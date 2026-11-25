"""office2pdf 缓存与转换进程编排。

- 缓存命中判断 / 过期清理 / 旧版本删除；
- ensure_pdf：缓存未命中时拉起 worker 子进程转换；
- copy_pdf：把缓存 PDF 复制到目标路径。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from core.config import DATA_DIR, PROJECT_ROOT
from core.fs_utils import atomic_write_json
from core.paths import resolve_storage_path, to_storage_path

_DEFAULT_TIMEOUT = 120
_CACHE_TTL_SECONDS = 7 * 24 * 3600
_CACHE_MAX_BYTES = 500 * 1024 * 1024
_PRUNE_MIN_AGE_S = 300  # 新转换产物在宽限期内不参与容量淘汰，避免与 copy_pdf 竞态

CACHE_DIR = DATA_DIR / "cache" / "office2pdf"

_WORD_EXTS = {".doc", ".docx", ".docm", ".rtf"}
_EXCEL_EXTS = {".xls", ".xlsx", ".xlsm", ".xlsb"}
_SLIDE_EXTS = {".ppt", ".pptx", ".pptm", ".ppsx", ".ppsm", ".potx"}

SUPPORTED_EXTENSIONS = _WORD_EXTS | _EXCEL_EXTS | _SLIDE_EXTS

_cache_guard = threading.Lock()
_cache_locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
_prune_guard = threading.Lock()


def _kind_for_ext(ext: str) -> str | None:
    ext = ext.lower()
    if ext in _WORD_EXTS:
        return "word"
    if ext in _EXCEL_EXTS:
        return "excel"
    if ext in _SLIDE_EXTS:
        return "slides"
    return None


def _source_key(source: str | Path) -> str:
    """源路径的持久化键：项目内相对、项目外绝对（cili 整体搬动后缓存仍可命中）。"""
    try:
        path = resolve_storage_path(str(source))
    except ValueError:
        path = Path(str(source))
    real = os.path.realpath(str(path))
    try:
        return to_storage_path(real)
    except ValueError:
        return real


def _cache_key(source: str) -> tuple[str, os.stat_result]:
    stat = os.stat(source)
    raw = f"{_source_key(source)}\0{stat.st_mtime_ns}\0{stat.st_size}".encode("utf-8", "replace")
    return hashlib.md5(raw).hexdigest(), stat


def _key_lock(key: str) -> threading.Lock:
    with _cache_guard:
        lock = _cache_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _cache_locks[key] = lock
        return lock


def _get_cache_dir() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def workspace_cache_dir(workspace_uuid: str) -> Path:
    """工作区级 PDF 预览缓存目录：{workspace}/.cili/cache/pdf/。"""
    from core.config import get_workspace_data_dir

    d = get_workspace_data_dir(workspace_uuid) / "cache" / "pdf"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_meta(meta_path: Path, pdf_path: Path, stat: os.stat_result) -> dict[str, Any] | None:
    if not pdf_path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if meta.get("mtime_ns") != stat.st_mtime_ns or meta.get("size") != stat.st_size:
        return None
    try:
        with open(pdf_path, "rb") as f:
            if f.read(5) != b"%PDF-":
                return None
    except OSError:
        return None
    return meta


def _remove_superseded(cache_dir: Path, source_key: str, keep_key: str) -> int:
    """删除同一源文件的过期缓存版本（源文件变化后旧 key 的 pdf/meta）。"""
    removed = 0
    for meta_path in cache_dir.glob("*.json"):
        if meta_path.stem == keep_key:
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        stored = str(meta.get("source") or "")
        if stored and _source_key(stored) == source_key:
            meta_path.unlink(missing_ok=True)
            meta_path.with_suffix(".pdf").unlink(missing_ok=True)
            removed += 1
    return removed


def _prune_cache(cache_dir: Path | None = None) -> None:
    with _prune_guard:
        cache_dir = cache_dir if cache_dir is not None else _get_cache_dir()
        now = time.time()
        entries: list[tuple[float, int, Path]] = []
        for pdf in cache_dir.glob("*.pdf"):
            try:
                st = pdf.stat()
            except OSError:
                continue
            age = now - st.st_mtime
            if age > _CACHE_TTL_SECONDS:
                pdf.unlink(missing_ok=True)
                pdf.with_suffix(".json").unlink(missing_ok=True)
                continue
            entries.append((st.st_mtime, st.st_size, pdf))
        total = sum(size for _, size, _ in entries)
        for mtime, size, pdf in sorted(entries, key=lambda e: e[0]):
            if total <= _CACHE_MAX_BYTES:
                break
            if now - mtime < _PRUNE_MIN_AGE_S:
                continue  # 刚生成的产物可能正被 copy_pdf 使用
            pdf.unlink(missing_ok=True)
            pdf.with_suffix(".json").unlink(missing_ok=True)
            total -= size


def _kill_office_pid(pid_file: Path) -> None:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return
    if pid <= 0:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    else:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def _run_worker(source: str, output: str, kind: str, pid_file: Path, timeout: int) -> str:
    """在隔离子进程中执行 COM 转换，并施加硬超时。"""
    # 用 -c 而不是 -m：core.tools.__init__ 会先导入本模块，runpy 再执行 -m
    # 会触发 "found in sys.modules" RuntimeWarning，并混进失败时的错误详情。
    cmd = [
        sys.executable,
        "-c",
        "import sys; from core.office_pdf.convert import _worker_main;"
        " raise SystemExit(_worker_main(sys.argv[1:]))",
        source,
        output,
        kind,
        str(pid_file),
    ]
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(PROJECT_ROOT) + (os.pathsep + existing if existing else "")
    # 失败详情里有中文诊断（ProgID 注册状态），固定 UTF-8 避免 GBK 控制台
    # 编码失败把真实错误盖掉。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        _kill_office_pid(pid_file)
        Path(output).unlink(missing_ok=True)
        pid_file.unlink(missing_ok=True)
        raise TimeoutError(f"Office conversion timed out after {timeout}s")
    except Exception:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        Path(output).unlink(missing_ok=True)
        pid_file.unlink(missing_ok=True)
        raise
    finally:
        pass

    pid_file.unlink(missing_ok=True)

    if proc.returncode != 0:
        Path(output).unlink(missing_ok=True)
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(detail or f"office2pdf worker exited with code {proc.returncode}")

    if not Path(output).exists() or Path(output).stat().st_size == 0:
        raise RuntimeError("office2pdf worker produced no PDF")
    engine = "Office/WPS COM"
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("engine"):
            engine = str(payload["engine"])
        break
    return engine


def ensure_pdf(
    source: str, *, cache_dir: Path | None = None, timeout: int = _DEFAULT_TIMEOUT,
    prune: bool = True,
) -> tuple[Path, str, bool]:
    """Return a cached PDF for *source*, converting once if needed.

    ``cache_dir`` 缺省时回退到全局缓存（测试 / 无工作区上下文的调用方）；
    生产调用方（预览路由 / 工具）传 ``workspace_cache_dir()``。

    Returns ``(pdf_path, engine, cached)``. Raises on unsupported input,
    missing Office/WPS, or conversion failure.
    """
    ext = Path(source).suffix.lower()
    kind = _kind_for_ext(ext)
    if kind is None:
        raise ValueError(
            f"unsupported Office extension '{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    if not os.path.isfile(source):
        raise FileNotFoundError(source)

    source_key = _source_key(source)
    key, stat = _cache_key(source)
    cache_dir = cache_dir if cache_dir is not None else _get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = cache_dir / f"{key}.pdf"
    meta_path = cache_dir / f"{key}.json"

    with _key_lock(key):
        cached = _read_meta(meta_path, pdf_path, stat)
        if cached is not None:
            return pdf_path, str(cached.get("engine") or "cached"), True

        tmp = cache_dir / f".{key}.{os.getpid()}.{threading.get_ident()}.tmp.pdf"
        try:
            tmp.unlink(missing_ok=True)
            engine = _run_worker(source, str(tmp), kind, tmp.with_suffix(".officepid"), timeout)
            os.replace(tmp, pdf_path)
            atomic_write_json(meta_path, {
                "source": source_key,
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "engine": engine,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            # 源文件变化会换 key：清掉同源旧版本，避免缓存目录膨胀
            _remove_superseded(cache_dir, source_key, key)
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_suffix(".officepid").unlink(missing_ok=True)
        if prune:
            _prune_cache(cache_dir)
        return pdf_path, engine, False


def copy_pdf(pdf_path: Path, output_path: str) -> None:
    """Atomically copy a cached PDF to the requested output path."""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        shutil.copyfile(pdf_path, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)

