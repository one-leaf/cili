"""自动升级模块：启动时检查 GitHub 新版本，有更新则自动下载代码包覆盖本地。

版本比对来源是 web/static/footer.json 的 version 字段（格式 vYYYYMMDD），
由 .githooks pre-commit hook 每次提交时自动更新为当天日期：
- 本地 footer.json 缺失或不可读时按 0.0.0 处理
- 远端 footer.json 拉取失败视为"检查失败"，跳过升级（不影响服务启动）

升级由启动时的后台线程自动完成，进程级 _upgrade_lock 保证同一时间只有一个
升级任务在写代码文件。

用法:
    from core.updater import start_auto_upgrade
    start_auto_upgrade()      # 后台线程：延迟后检查 + 自动升级
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path

from core.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

# 本地版本文件（version 字段，与 GitHub 仓库 web/static/footer.json 对应）
VERSION_FILE = PROJECT_ROOT / "web" / "static" / "footer.json"

# 代码包镜像 URL（与 scripts/upgrade.ps1 一致）
MIRROR_URLS = {
    "github": "https://github.com/one-leaf/cili/archive/refs/heads/main.zip",
    "ghproxy": "https://ghproxy.net/https://github.com/one-leaf/cili/archive/refs/heads/main.zip",
    "ghfast": "https://ghfast.top/https://github.com/one-leaf/cili/archive/refs/heads/main.zip",
    "gh-proxy": "https://gh-proxy.com/https://github.com/one-leaf/cili/archive/refs/heads/main.zip",
}

# 版本文件 URL（raw 直连 + 国内镜像回退）
VERSION_URLS = [
    "https://raw.githubusercontent.com/one-leaf/cili/main/web/static/footer.json",
    "https://ghproxy.net/https://raw.githubusercontent.com/one-leaf/cili/main/web/static/footer.json",
    "https://ghfast.top/https://raw.githubusercontent.com/one-leaf/cili/main/web/static/footer.json",
    "https://gh-proxy.com/https://raw.githubusercontent.com/one-leaf/cili/main/web/static/footer.json",
]

# 升级时排除的顶层目录（用户数据与版本控制目录不覆盖）
_EXCLUDE_DIRS = {"data", "workspace", ".git"}

# 进程级升级锁：web 手动升级与启动自动升级互斥，防止并发覆盖运行代码（W7）
_upgrade_lock = threading.Lock()


def get_local_version() -> str:
    """读取本地版本号（footer.json 的 version 字段）；缺失或不可读时返回 0.0.0。"""
    try:
        data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
        return str(data.get("version", "")).strip() or "0.0.0"
    except Exception:
        return "0.0.0"


def parse_version(version: str) -> tuple[int, ...]:
    """解析版本号（如 '1.2.3'）为可比较的整数元组；非数字段按 0 处理。"""
    parts = []
    for seg in str(version).strip().split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer_version(remote: str, local: str) -> bool:
    """remote > local 返回 True（长度不足的段按 0 对齐比较）。"""
    r, l = parse_version(remote), parse_version(local)
    n = max(len(r), len(l))
    return r + (0,) * (n - len(r)) > l + (0,) * (n - len(l))


def _download_text(urls: list[str], timeout: float = 30) -> str | None:
    """依次尝试 URL 下载文本内容（版本文件），全部失败返回 None。"""
    try:
        import httpx

        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Cili-Agent-Updater"},
        ) as client:
            for url in urls:
                try:
                    resp = client.get(url)
                    if resp.status_code == 200 and resp.text.strip():
                        return resp.text.strip()
                except Exception as e:
                    logger.warning(f"[updater] 下载失败: {url}: {e}")
    except Exception as e:
        logger.warning(f"[updater] 网络请求失败: {e}")
    return None


def fetch_remote_version() -> str | None:
    """获取 GitHub 仓库 footer.json 的 version 字段。"""
    text = _download_text(VERSION_URLS)
    if not text:
        return None
    try:
        data = json.loads(text)
        return str(data.get("version", "")).strip() or None
    except Exception as e:
        logger.warning(f"[updater] 解析远端版本失败: {e}")
        return None


def check_update() -> tuple[bool, str, str | None]:
    """检查 GitHub 是否有新版本。

    Returns:
        (has_update, local_version, remote_version)；remote_version 为 None 表示检查失败。
    """
    local = get_local_version()
    remote = fetch_remote_version()
    if remote is None:
        return False, local, None
    return is_newer_version(remote, local), local, remote


def _download_zip(urls: list[str], dest: str, timeout: float = 120) -> tuple[bool, str]:
    """依次尝试镜像下载代码包 zip 到 dest。返回 (是否成功, 最后错误信息)。"""
    last_error = ""
    try:
        import httpx

        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for url in urls:
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    if resp.content:
                        with open(dest, "wb") as f:
                            f.write(resp.content)
                        return True, ""
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"[updater] 下载失败: {url}: {e}")
    except Exception as e:
        return False, str(e)
    return False, last_error or "所有镜像均下载失败"


def _safe_extract(zip_path: str, dest_dir: str) -> str | None:
    """安全解压代码包：逐条目校验路径，拒绝绝对路径与 .. 穿越（zip-slip）。

    Returns:
        str | None: 解压出的仓库根目录（如 cili-main/），失败返回 None
    """
    try:
        os.makedirs(dest_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in Path(name).parts:
                    logger.error(f"[updater] 压缩包包含非法路径条目: {info.filename}")
                    return None
            zf.extractall(dest_dir)
    except Exception as e:
        logger.error(f"[updater] 解压失败: {e}")
        return None

    for name in os.listdir(dest_dir):
        if name.startswith("cili-main"):
            return os.path.join(dest_dir, name)
    logger.error("[updater] 解压后未找到 cili-main 目录")
    return None


def _copy_tree(src: str, dst: str, exclude_dirs: set[str]) -> None:
    """递归复制文件树，跳过 exclude_dirs 中的顶层目录。"""
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if item in exclude_dirs:
            continue
        if os.path.isdir(s):
            os.makedirs(d, exist_ok=True)
            _copy_tree(s, d, exclude_dirs)
        else:
            shutil.copy2(s, d)


def do_upgrade() -> dict:
    """执行升级：下载代码包 → 安全解压 → 覆盖本地代码文件。

    Returns:
        成功: {"success": True, "message": str, "needs_restart": True}
        失败: {"success": False, "error": str}
    """
    if not _upgrade_lock.acquire(blocking=False):
        return {"success": False, "error": "已有升级任务进行中，请稍后重试"}

    try:
        import tempfile

        # 按插入顺序依次尝试各镜像（GitHub 直连优先，国内镜像回退）
        download_urls = list(MIRROR_URLS.values())

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_zip = os.path.join(temp_dir, "cili-main.zip")
            temp_extract = os.path.join(temp_dir, "extract")

            downloaded, last_error = _download_zip(download_urls, temp_zip)
            if not downloaded:
                return {"success": False, "error": f"所有镜像均下载失败：{last_error}"}

            extracted_dir = _safe_extract(temp_zip, temp_extract)
            if not extracted_dir:
                return {"success": False, "error": "解压失败或解压目录未找到"}

            try:
                _copy_tree(extracted_dir, str(PROJECT_ROOT), _EXCLUDE_DIRS)
            except Exception as e:
                return {"success": False, "error": f"复制文件失败：{str(e)}"}
    finally:
        _upgrade_lock.release()

    return {
        "success": True,
        "message": "升级完成，请重启服务以应用更新",
        "needs_restart": True,
    }


def _auto_upgrade_worker(delay: float) -> None:
    try:
        time.sleep(delay)
        has_update, local, remote = check_update()
        if remote is None:
            logger.warning("[updater] 版本检查失败（网络异常），跳过自动升级")
            return
        if not has_update:
            logger.info(f"[updater] 已是最新版本（{local}）")
            return
        logger.info(f"[updater] 发现新版本 {remote}（本地 {local}），开始自动升级...")
        result = do_upgrade()
        if result.get("success"):
            logger.info(f"[updater] {result.get('message')}")
        else:
            logger.warning(f"[updater] 自动升级失败: {result.get('error')}")
    except Exception as e:
        logger.error(f"[updater] 自动升级异常: {e}")


def start_auto_upgrade(delay: float = 5.0) -> threading.Thread:
    """后台线程启动自动升级：延迟 delay 秒后检查版本，有新版本则自动升级。

    升级完成后仅提示重启（不强制重启，避免打断正在运行的会话）。
    """
    thread = threading.Thread(
        target=_auto_upgrade_worker, args=(delay,), daemon=True, name="AutoUpgrade"
    )
    thread.start()
    return thread
