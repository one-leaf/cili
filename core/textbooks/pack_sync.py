"""教材数据包后台同步：从 cili-data 仓库静默下载并展开。

数据分发在独立仓库 ``github.com/one-leaf/cili-data``。下载源优先级：
Release 资产 → **Release 源码归档**（GitHub 按 tag 自动生成，天然包含
``textbook/dist/`` 三件套，无需手工上传资产）→ raw dist 文件。

1. 取远端 manifest.json，比对本地 pack_meta.json 的 dataset_version；
2. 版本不一致 / 本地缺失 → 下载 chapters.zip + catalog.json（国内镜像链
   gh-proxy.com / ghfast.top / ghproxy.net 依次失败转移，直连兜底，
   Range 断点续传）；
3. 按 manifest 的 sha256 校验 → 临时目录解压安装 → 原子替换
   ``data/cili/textbooks/``；
4. 构建 catalog.db（FTS trigram，约 85 秒）。

全程只打印【教材数据】日志；失败静默重试，不影响任何功能。
配置：``setting.json → textbooks``（缺省 auto_sync=true）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from core.config import PROJECT_ROOT
from core.textbooks.pack_store import PACK_DIR, build_catalog_db

logger = logging.getLogger(__name__)

SETTING_PATH = PROJECT_ROOT / "data" / "cili" / "setting.json"
CFG_DEFAULTS: dict[str, Any] = {
    "auto_sync": True,
    "repo": "one-leaf/cili-data",
    "channel": "release",       # release | raw
    "mirror": "auto",           # auto（镜像链+直连）| direct（仅直连）；旧值 gh-proxy 等同 auto
    "check_interval_hours": 24,
}
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_BLOCK = 256 * 1024

# GitHub 代理前缀：按顺序失败转移，最后回落直连（与 core/updater.py 的镜像列表一致）
PROXY_PREFIXES = (
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://ghproxy.net/",
)

STATE: dict[str, Any] = {"status": "idle", "progress": 0.0, "message": ""}
_thread: threading.Thread | None = None
_lock = threading.Lock()


def state() -> dict[str, Any]:
    return dict(STATE)


def _set(status: str, message: str = "", progress: float | None = None) -> None:
    STATE["status"] = status
    STATE["message"] = message
    if progress is not None:
        STATE["progress"] = progress
    logger.info(f"[教材数据] {status}: {message}")


def _settings() -> dict[str, Any]:
    cfg = dict(CFG_DEFAULTS)
    try:
        data = json.loads(SETTING_PATH.read_text(encoding="utf-8"))
        cfg.update(data.get("textbooks") or {})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def _proxied(cfg: dict[str, Any], url: str) -> list[str]:
    """镜像全前缀 + 直连；mirror=direct 时仅直连。"""
    if cfg.get("mirror") == "direct":
        return [url]
    return [f"{p}{url}" for p in PROXY_PREFIXES] + [url]


def _urls(cfg: dict[str, Any], name: str) -> list[str]:
    repo = cfg["repo"]
    bases: list[str] = []
    if cfg.get("channel") == "release":
        bases.append(f"https://github.com/{repo}/releases/latest/download/{name}")
    bases.append(f"https://raw.githubusercontent.com/{repo}/main/textbook/dist/{name}")
    if cfg.get("mirror") == "direct":
        return bases
    # 先跑完所有镜像（release → raw 各自过一遍代理），直连最后兜底
    return ([f"{p}{base}" for base in bases for p in PROXY_PREFIXES]
            + bases)


def _fetch_json(cfg: dict[str, Any], name: str) -> dict[str, Any] | None:
    for url in _urls(cfg, name):
        try:
            r = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
            if r.status_code == 200:
                return r.json()
            logger.debug(f"[教材数据] {url} -> HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001 —— 换下一个源
            logger.debug(f"[教材数据] {url} 失败: {e}")
    return None


def _latest_tag(cfg: dict[str, Any]) -> str:
    """取最新 release 的 tag（用于回退到源码归档 zip）。"""
    api = f"https://api.github.com/repos/{cfg['repo']}/releases/latest"
    urls = _proxied(cfg, api)
    for url in urls:
        try:
            r = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True,
                          headers={"Accept": "application/vnd.github+json"})
            if r.status_code == 200:
                return str((r.json() or {}).get("tag_name") or "")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[教材数据] 获取 release tag 失败: {e}")
    return ""


def _reject_html(resp: httpx.Response) -> None:
    """镜像/网关错误时可能返回 200 + HTML 错误页，直接判为失败换下一个源。"""
    ctype = (resp.headers.get("content-type") or "").lower()
    if "text/html" in ctype:
        raise RuntimeError(f"源返回 HTML 页面（{ctype}）")


def _extract_from_archive(cfg: dict[str, Any], tag: str, name: str,
                          dest: Path, expect_sha: str = "") -> bool:
    """从 release 源码归档（GitHub 自动生成）抽取 textbook/dist/<name>。

    归档是仓库在该 tag 上的快照，天然包含 dist 三件套，无需手工上传资产。
    """
    import zipfile
    repo = cfg["repo"]
    base = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
    urls = _proxied(cfg, base)
    archive = dest.parent / f"src-{tag}.zip"
    part = archive.with_suffix(".zip.part")
    for attempt, url in enumerate(urls):
        try:
            offset = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
                with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code == 200 and offset:
                        offset = 0
                        part.unlink(missing_ok=True)
                    elif resp.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    _reject_html(resp)
                    with open(part, "ab" if resp.status_code == 206 else "wb") as f:
                        for block in resp.iter_bytes(_BLOCK):
                            f.write(block)
            part.replace(archive)
            break
        except Exception as e:  # noqa: BLE001 —— 换下一个源
            logger.warning(f"[教材数据] 下载源码归档第 {attempt + 1}/{len(urls)} 个源失败: {e}")
            time.sleep(min(2.0 * (attempt + 1), 6.0))
    else:
        return False
    try:
        with zipfile.ZipFile(archive) as z:
            member = next((m for m in z.namelist()
                           if m.endswith(f"/textbook/dist/{name}")), "")
            if not member:
                logger.warning(f"[教材数据] 源码归档缺少 {name}")
                return False
            data = z.read(member)
        dest.write_bytes(data)
        if expect_sha and _sha256(dest) != expect_sha:
            dest.unlink(missing_ok=True)
            logger.warning(f"[教材数据] 源码归档内 {name} SHA256 不符")
            return False
        # 解包成功后删除整仓归档，避免每轮回退都在 data/cili 留一份
        archive.unlink(missing_ok=True)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[教材数据] 解包源码归档失败: {e}")
        return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(cfg: dict[str, Any], name: str, dest: Path,
              expected: int = 0, expect_sha: str = "") -> bool:
    """Range 断点续传到 dest；校验大小与 sha256。"""
    part = dest.with_suffix(dest.suffix + ".part")
    urls = _urls(cfg, name)
    for attempt, url in enumerate(urls):
        try:
            offset = part.stat().st_size if part.exists() else 0
            if expected and offset > expected:
                part.unlink(missing_ok=True)
                offset = 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
                with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code == 200 and offset:
                        offset = 0
                        part.unlink(missing_ok=True)
                    elif resp.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    _reject_html(resp)
                    mode = "ab" if resp.status_code == 206 else "wb"
                    total = expected or int(resp.headers.get("content-length") or 0)
                    done = offset
                    with open(part, mode) as f:
                        for block in resp.iter_bytes(_BLOCK):
                            f.write(block)
                            done += len(block)
                            if total:
                                _set("downloading", f"{name} {done * 100 // total}%",
                                     min(0.99, done / total))
            if expected and part.stat().st_size != expected:
                raise RuntimeError(f"大小不符: {part.stat().st_size}/{expected}")
            if expect_sha and _sha256(part) != expect_sha:
                part.unlink(missing_ok=True)
                raise RuntimeError("SHA256 校验失败")
            part.replace(dest)
            return True
        except Exception as e:  # noqa: BLE001 —— 换下一个源
            logger.warning(f"[教材数据] 下载 {name} 第 {attempt + 1}/{len(urls)} 个源失败: {e}")
            time.sleep(min(2.0 * (attempt + 1), 6.0))
    return False


def _install(manifest: dict[str, Any]) -> bool:
    """把下载好的包原子安装到 PACK_DIR。"""
    staging = PACK_DIR.with_name("textbooks.staging")
    if staging.exists():
        import shutil
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    artifacts = manifest.get("artifacts") or {}
    for key, name in (("chapters", "chapters.zip"), ("catalog", "catalog.json")):
        item = artifacts.get(key)
        src = staging.parent / f"{name}.dl"
        if item is None or not src.is_file():
            logger.warning(f"[教材数据] 安装缺少 {name}")
            return False
        src.replace(staging / name)
    (staging / "pack_meta.json").write_text(
        json.dumps({**manifest, "installed_at": _now()},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    preserved_original = None
    if PACK_DIR.exists():
        import shutil
        # originals.zip 是用户本地安装的清洗版原文（分发包不含），同步不能删掉
        local_original = PACK_DIR / "originals.zip"
        if local_original.is_file():
            preserved_original = PACK_DIR.with_name("textbooks.originals.zip")
            try:
                local_original.replace(preserved_original)
            except OSError as e:
                logger.warning(f"[教材数据] 保留 originals.zip 失败，同步继续: {e}")
                preserved_original = None
        shutil.rmtree(PACK_DIR)
    staging.rename(PACK_DIR)
    if preserved_original is not None:
        try:
            preserved_original.replace(PACK_DIR / "originals.zip")
        except OSError as e:
            logger.warning(f"[教材数据] 恢复 originals.zip 失败: {e}")
    return True


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _local_version() -> str:
    try:
        meta = json.loads((PACK_DIR / "pack_meta.json").read_text(encoding="utf-8"))
        return str(meta.get("dataset_version") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def sync_once(cfg: dict[str, Any] | None = None) -> bool:
    """执行一次检查+同步。返回是否已就绪。"""
    cfg = cfg or _settings()
    _set("checking", "检查数据版本")
    remote = _fetch_json(cfg, "manifest.json")
    if not remote:
        _set("error", "无法获取远端 manifest（网络不可用）")
        return False
    version = str(remote.get("dataset_version") or "")
    local = _local_version()
    has_zip = (PACK_DIR / "chapters.zip").is_file()
    has_db = (PACK_DIR / "catalog.db").is_file()
    if version and version == local and has_zip and has_db:
        _set("ready", f"数据已是最新（{version}）", 1.0)
        return True
    if version and version == local and has_zip:
        _set("building", "构建检索索引")
        try:
            build_catalog_db(PACK_DIR)
        except Exception as e:  # noqa: BLE001
            _set("error", f"构建检索索引失败: {e}")
            return False
        _set("ready", f"检索索引完成（{version}）", 1.0)
        return True

    _set("downloading", "下载数据包", 0.0)
    artifacts = remote.get("artifacts") or {}
    dl_dir = PACK_DIR.parent
    dl_dir.mkdir(parents=True, exist_ok=True)
    tag = ""
    ok = True
    for key, name in (("chapters", "chapters.zip"), ("catalog", "catalog.json")):
        item = artifacts.get(key) or {}
        dest = dl_dir / f"{name}.dl"
        if _download(cfg, name, dest, int(item.get("bytes") or 0),
                     str(item.get("sha256") or "")):
            continue
        # 回退：release 源码归档（GitHub 自动生成，含 dist 三件套，无需手工资产）
        if not tag:
            tag = _latest_tag(cfg)
        if tag and _extract_from_archive(cfg, tag, name, dest,
                                         str(item.get("sha256") or "")):
            logger.info(f"[教材数据] {name} 来自源码归档 {tag}")
            continue
        _set("error", f"下载失败: {name}")
        ok = False
        break
    if not ok:
        return False

    _set("installing", "安装数据包", 0.99)
    if not _install(remote):
        _set("error", "安装失败")
        return False
    _set("building", "构建检索索引")
    try:
        build_catalog_db(PACK_DIR)
    except Exception as e:  # noqa: BLE001
        _set("error", f"构建检索索引失败: {e}")
        return False
    _set("ready", f"同步完成（{version or 'unknown'}）", 1.0)
    return True


def _worker() -> None:
    cfg = _settings()
    interval = max(1.0, float(cfg.get("check_interval_hours") or 24)) * 3600
    while True:
        try:
            sync_once(cfg)
        except Exception as e:  # noqa: BLE001 —— 后台线程不退出
            logger.warning(f"[教材数据] 同步异常: {e}")
            _set("error", str(e))
        time.sleep(interval)


def start_background() -> bool:
    """启动后台同步（auto_sync=false 时跳过）。返回是否已启动。"""
    global _thread
    cfg = _settings()
    if not cfg.get("auto_sync", True):
        _set("disabled", "自动同步已关闭（setting.json → textbooks.auto_sync）")
        return False
    with _lock:
        if _thread and _thread.is_alive():
            return True
        _thread = threading.Thread(target=_worker, daemon=True,
                                   name="textbook-pack-sync")
        _thread.start()
    logger.info(f"[教材数据] 后台同步已启动（repo={cfg['repo']}）")
    return True
