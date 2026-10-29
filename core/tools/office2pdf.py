"""Office document → PDF conversion tool.

Uses the locally installed Microsoft Office (Word/Excel/PowerPoint) or WPS
COM automation. Conversion runs in a short-lived worker subprocess so a hung
Office instance cannot block the agent loop; the parent enforces a timeout and
kills the recorded Office process on timeout.

Results are cached per workspace under ``{workspace}/.cili/cache/pdf/`` keyed
by source path + mtime_ns + size; unchanged sources reuse the cached PDF and
superseded versions of the same source are removed on re-conversion. Callers
without a workspace context fall back to the legacy ``data/cache/office2pdf``.

The same module doubles as the worker entry point::

    python -m core.tools.office2pdf --worker <source> <output> <kind> <pid-file>
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from core.config import DATA_DIR, PROJECT_ROOT
from core.fs_utils import atomic_write_json
from core.security.path_policy import OP_WRITE, PathTarget
from core.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120
_MAX_TIMEOUT = 600
_MIN_TIMEOUT = 10
_CACHE_TTL_SECONDS = 7 * 24 * 3600
_CACHE_MAX_BYTES = 500 * 1024 * 1024

CACHE_DIR = DATA_DIR / "cache" / "office2pdf"

_WORD_EXTS = {".doc", ".docx", ".docm", ".rtf"}
_EXCEL_EXTS = {".xls", ".xlsx", ".xlsm", ".xlsb"}
_SLIDE_EXTS = {".ppt", ".pptx", ".pptm", ".ppsx", ".ppsm", ".potx"}

SUPPORTED_EXTENSIONS = _WORD_EXTS | _EXCEL_EXTS | _SLIDE_EXTS

_ENGINE_PROGIDS: dict[str, list[tuple[str, str]]] = {
    "word": [
        ("Microsoft Office Word", "Word.Application"),
        ("WPS Writer", "KWPS.Application"),
    ],
    "excel": [
        ("Microsoft Office Excel", "Excel.Application"),
        ("WPS Spreadsheets", "KET.Application"),
    ],
    "slides": [
        ("Microsoft Office PowerPoint", "PowerPoint.Application"),
        ("WPS Presentation", "KWPP.Application"),
    ],
}

_cache_guard = threading.Lock()
_cache_locks: dict[str, threading.Lock] = {}
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


def _cache_key(source: str) -> tuple[str, os.stat_result]:
    stat = os.stat(source)
    raw = f"{os.path.realpath(source)}\0{stat.st_mtime_ns}\0{stat.st_size}".encode("utf-8", "replace")
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


def _remove_superseded(cache_dir: Path, source_real: str, keep_key: str) -> int:
    """删除同一源文件的过期缓存版本（源文件变化后旧 key 的 pdf/meta）。"""
    removed = 0
    for meta_path in cache_dir.glob("*.json"):
        if meta_path.stem == keep_key:
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if meta.get("source") == source_real:
            meta_path.unlink(missing_ok=True)
            meta_path.with_suffix(".pdf").unlink(missing_ok=True)
            removed += 1
    return removed


def _prune_cache(cache_dir: Path | None = None) -> None:
    with _prune_guard:
        cache_dir = cache_dir if cache_dir is not None else _get_cache_dir()
        now = time.time()
        entries: list[tuple[float, Path]] = []
        for pdf in cache_dir.glob("*.pdf"):
            try:
                age = now - pdf.stat().st_mtime
            except OSError:
                continue
            meta = pdf.with_suffix(".json")
            if age > _CACHE_TTL_SECONDS:
                pdf.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                continue
            entries.append((pdf.stat().st_mtime, pdf))
        total = sum(pdf.stat().st_size for _, pdf in entries if pdf.exists())
        for _, pdf in sorted(entries):
            if total <= _CACHE_MAX_BYTES:
                break
            try:
                size = pdf.stat().st_size
            except OSError:
                continue
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
    """Run the COM conversion in an isolated subprocess with a hard timeout."""
    cmd = [
        sys.executable,
        "-m",
        "core.tools.office2pdf",
        "--worker",
        source,
        output,
        kind,
        str(pid_file),
    ]
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(PROJECT_ROOT) + (os.pathsep + existing if existing else "")
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
    source: str, *, cache_dir: Path | None = None, timeout: int = _DEFAULT_TIMEOUT
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
                "source": os.path.realpath(source),
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "engine": engine,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            # 源文件变化会换 key：清掉同源旧版本，避免缓存目录膨胀
            _remove_superseded(cache_dir, os.path.realpath(source), key)
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_suffix(".officepid").unlink(missing_ok=True)
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


# ----- workspace sync (office-pdf-sync cron) -----

_SKIP_DIR_NAMES = {".cili", ".git", "node_modules", "__pycache__", ".svn", ".idea", ".vscode"}


def _iter_office_docs(workspace_dir: Path):
    """遍历工作区内的 Office 文档（跳过 .cili/.git/node_modules 等目录）。"""
    for dirpath, dirnames, filenames in os.walk(workspace_dir):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                yield p


def sync_workspace_pdf_cache(workspace_uuid: str, *, limit: int = 100,
                             timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """把一个工作区的全部 Office 文档增量同步到 ``.cili/cache/pdf/``。

    - 新增/变更的文档转换为 PDF（受 ``limit`` 限额，超出留给下一轮）；
    - 源文件已删除或 meta 损坏的缓存条目（过期 PDF）直接清理；
    - 同源旧版本由 ``ensure_pdf`` 转换时自动清理。

    写操作只落在 ``{workspace}/.cili/cache/pdf/``（工具内部缓存，不触碰
    用户文件），因此不需要路径策略门禁。
    """
    from core.config import load_workspace_config

    cfg = load_workspace_config(workspace_uuid)
    root = Path((cfg or {}).get("directory", ""))
    if not cfg or not root.exists():
        return {"workspace_uuid": workspace_uuid, "error": "workspace directory not found"}

    cache_dir = workspace_cache_dir(workspace_uuid)
    docs = sorted(_iter_office_docs(root))
    valid_sources = {os.path.realpath(str(d)) for d in docs}

    # 过期清理：meta 指向的源已不存在，或 meta 损坏
    removed = 0
    for meta_path in cache_dir.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            source_ok = str(meta.get("source", "")) in valid_sources
        except Exception:
            source_ok = False
        if not source_ok:
            meta_path.unlink(missing_ok=True)
            meta_path.with_suffix(".pdf").unlink(missing_ok=True)
            removed += 1

    converted = skipped = failed = 0
    errors: list[str] = []
    for doc in docs:
        try:
            key, stat = _cache_key(str(doc))
        except OSError:
            continue
        if _read_meta(cache_dir / f"{key}.json", cache_dir / f"{key}.pdf", stat) is not None:
            skipped += 1
            continue
        if converted >= limit:
            continue  # 超出本轮限额：留给下一轮（2 小时后）
        try:
            ensure_pdf(str(doc), cache_dir=cache_dir, timeout=timeout)
            converted += 1
        except Exception as e:
            failed += 1
            errors.append(f"{doc.name}: {e}")

    return {
        "workspace_uuid": workspace_uuid,
        "docs": len(docs),
        "converted": converted,
        "skipped": skipped,
        "failed": failed,
        "removed_stale": removed,
        "cache_dir": str(cache_dir),
        "errors": errors[:10],
    }


class Office2PDFTool(Tool):
    """Convert Word/Excel/PowerPoint documents to PDF via Office/WPS COM."""

    name = "office2pdf"
    description = """Convert a Word, Excel, or PowerPoint document to PDF using the locally installed Microsoft Office or WPS COM engine.

Supported inputs: .doc, .docx, .docm, .rtf, .xls, .xlsx, .xlsm, .xlsb, .ppt, .pptx, .pptm, .ppsx, .ppsm, .potx.

The source is opened read-only with macros and external link updates disabled. Conversion runs in an isolated worker process with a timeout; results are cached by source path + mtime + size, so repeated conversions of an unchanged file reuse the PDF.

By default the PDF is written to <filename>.pdf in the current directory; pass output_path to choose another path. Pass overwrite=false to refuse replacing an existing PDF.

action="sync" batch-syncs workspace Office documents into the per-workspace preview cache ({workspace}/.cili/cache/pdf/): converts new or changed files, and removes cached PDFs whose source no longer exists. Used by the office-pdf-sync cron task; sync writes only inside .cili/cache/pdf and never touches user files.

Use this tool instead of writing ad-hoc Word/Excel/PowerPoint COM scripts for PDF export.
"""
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["convert", "sync"],
                "description": "convert: convert a single document (default). sync: batch-sync workspaces into the preview cache and clean stale PDFs.",
                "default": "convert",
            },
            "file_path": {
                "type": "string",
                "description": "Workspace-relative path to the Office document (convert only).",
            },
            "workspace_uuid": {
                "type": "string",
                "description": "sync only: limit to this workspace UUID. Default: all workspaces (System excluded).",
            },
            "limit": {
                "type": "integer",
                "description": "sync only: max new conversions per workspace per run (default 100).",
                "minimum": 1,
                "maximum": 1000,
                "default": 100,
            },
            "output_path": {
                "type": "string",
                "description": "Optional output PDF path. Defaults to <filename>.pdf in the current directory.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Whether to replace an existing output PDF (default: true).",
                "default": True,
            },
            "timeout": {
                "type": "integer",
                "description": f"Conversion timeout in seconds ({_MIN_TIMEOUT}-{_MAX_TIMEOUT}, default {_DEFAULT_TIMEOUT}).",
                "minimum": _MIN_TIMEOUT,
                "maximum": _MAX_TIMEOUT,
                "default": _DEFAULT_TIMEOUT,
            },
        },
        "required": ["file_path"],
    }
    concurrency_safe = False

    def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action") or "convert").strip().lower()
        if action == "sync":
            return self._execute_sync(kwargs)

        file_path = str(kwargs.get("file_path") or "").strip()
        if not file_path:
            return ToolResult("Error: file_path is required", error=True)

        abs_path = self._resolve_path(file_path, read_only=True)
        if not os.path.isfile(abs_path):
            return ToolResult(f"Error: file not found: {abs_path}", error=True)

        ext = Path(abs_path).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            return ToolResult(
                f"Error: unsupported file type '{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
                error=True,
            )

        gate = self._read_gate([abs_path])
        if gate:
            return gate

        output_arg = str(kwargs.get("output_path") or "").strip()
        if output_arg:
            output_path = self._resolve_path(output_arg)
        else:
            output_path = os.path.join(self.cwd, f"{Path(abs_path).stem}.pdf")

        gate = self._path_gate([PathTarget(OP_WRITE, output_path, "output", resolved=output_path)])
        if gate:
            return gate

        overwrite = kwargs.get("overwrite", True)
        if isinstance(overwrite, str):
            overwrite = overwrite.strip().lower() not in ("false", "0", "no", "off")
        if os.path.exists(output_path) and not overwrite:
            return ToolResult(f"Error: output already exists: {output_path}", error=True)

        try:
            timeout = int(kwargs.get("timeout", _DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        timeout = max(_MIN_TIMEOUT, min(timeout, _MAX_TIMEOUT))

        try:
            ws_uuid = str(getattr(self, "workspace_uuid", "") or "")
            cache_dir = workspace_cache_dir(ws_uuid) if ws_uuid else None
            pdf_path, engine, cached = ensure_pdf(abs_path, cache_dir=cache_dir, timeout=timeout)
            copy_pdf(pdf_path, output_path)
        except TimeoutError as e:
            return ToolResult(f"Error: {e}", error=True)
        except Exception as e:
            logger.warning(f"[office2pdf] conversion failed for {abs_path}: {e}")
            return ToolResult(
                f"Error: Office PDF conversion failed: {e}\n"
                "Ensure Microsoft Office or WPS is installed and no dialogs are blocking automation.",
                error=True,
            )

        output = (
            f"PDF: {output_path}\n"
            f"Engine: {engine}\n"
            f"Source: {abs_path}\n"
            f"Cached: {'yes' if cached else 'no'}"
        )
        return ToolResult(output, meta={
            "output_path": output_path,
            "cached": cached,
            "engine": engine,
        })

    def _execute_sync(self, kwargs: dict) -> ToolResult:
        """批量同步工作区 Office 文档到预览缓存。

        写操作限定在各工作区 ``.cili/cache/pdf/``（工具内部缓存，不触碰
        用户文件），因此不走路径策略门禁；跨工作区文档读取按「区外读取
        默认允许」策略放行。
        """
        try:
            limit = int(kwargs.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 1000))

        ws_arg = str(kwargs.get("workspace_uuid") or "").strip()
        if ws_arg:
            targets = [ws_arg]
        else:
            from core.config import load_workspaces_index

            targets = [
                str(e.get("uuid", "")) for e in load_workspaces_index()
                if e.get("uuid") and not e.get("system") and str(e.get("uuid")) != "system"
            ]

        results: list[dict] = []
        totals = {"converted": 0, "skipped": 0, "failed": 0, "removed_stale": 0}
        for uuid in targets:
            try:
                r = sync_workspace_pdf_cache(uuid, limit=limit)
            except Exception as e:
                logger.warning(f"[office2pdf] sync {uuid} failed: {e}")
                results.append({"workspace_uuid": uuid, "error": str(e)})
                continue
            results.append(r)
            for k in totals:
                totals[k] += int(r.get(k, 0))

        lines = [
            f"工作区数: {len(results)}",
            f"新转换: {totals['converted']} · 复用缓存: {totals['skipped']} · "
            f"清理过期: {totals['removed_stale']} · 失败: {totals['failed']}",
        ]
        for r in results:
            if r.get("error"):
                lines.append(f"- {r['workspace_uuid']}: 错误 {r['error']}")
                continue
            if r.get("converted") or r.get("removed_stale") or r.get("failed"):
                lines.append(
                    f"- {r['workspace_uuid']}: 转换 {r['converted']} / 复用 {r['skipped']} / "
                    f"清理 {r['removed_stale']} / 失败 {r['failed']}"
                )
                for err in r.get("errors", []):
                    lines.append(f"  · {err}")
        return ToolResult("\n".join(lines), meta={"totals": totals, "results": results})


# ----- worker entry point (COM runs here, never in the agent process) -----


def _write_office_pid(app: Any, pid_file: str) -> None:
    try:
        import ctypes

        hwnd = int(getattr(app, "Hwnd", 0) or 0)
        if not hwnd:
            return
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            Path(pid_file).write_text(str(pid.value), encoding="utf-8")
    except Exception:
        pass


def _configure_app(app: Any, kind: str) -> None:
    try:
        app.Visible = False
    except Exception:
        pass
    try:
        app.DisplayAlerts = False if kind == "excel" else 0
    except Exception:
        pass
    try:
        app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
    except Exception:
        pass


def _convert_with_com(source: str, output: str, kind: str, pid_file: str) -> str:
    import win32com.client as win32

    errors: list[str] = []
    for label, progid in _ENGINE_PROGIDS[kind]:
        app = None
        try:
            app = win32.DispatchEx(progid)
            _configure_app(app, kind)
            _write_office_pid(app, pid_file)

            if kind == "word":
                try:
                    app.Options.UpdateLinksAtOpen = False
                except Exception:
                    pass
                doc = app.Documents.Open(source, False, True, False)
                try:
                    doc.SaveAs2(output, 17)  # wdFormatPDF
                except Exception:
                    doc.SaveAs(output, 17)
                doc.Close(0)
            elif kind == "excel":
                try:
                    app.AskToUpdateLinks = False
                except Exception:
                    pass
                wb = app.Workbooks.Open(source, 0, True)
                wb.ExportAsFixedFormat(0, output)  # xlTypePDF
                wb.Close(False)
            else:
                pres = app.Presentations.Open(source, True, False, False)
                pres.ExportAsFixedFormat(output, 2)  # ppFixedFormatTypePDF
                pres.Close()

            if not Path(output).exists() or Path(output).stat().st_size == 0:
                raise RuntimeError("conversion produced no PDF")
            return label
        except Exception as e:
            errors.append(f"{label}: {e}")
            Path(output).unlink(missing_ok=True)
        finally:
            try:
                if app is not None:
                    app.Quit()
            except Exception:
                pass
            try:
                del app
            except Exception:
                pass
    raise RuntimeError("; ".join(errors) or "no Office/WPS COM engine available")


def _worker_main(argv: list[str]) -> int:
    import pythoncom

    if len(argv) != 4:
        print("usage: --worker <source> <output> <kind> <pid-file>", file=sys.stderr)
        return 2
    source, output, kind, pid_file = argv
    pythoncom.CoInitialize()
    try:
        engine = _convert_with_com(source, output, kind, pid_file)
        print(json.dumps({"engine": engine}, ensure_ascii=False))
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        raise SystemExit(_worker_main(sys.argv[2:]))
    raise SystemExit("Use the office2pdf tool from the agent, or --worker for internal use.")
