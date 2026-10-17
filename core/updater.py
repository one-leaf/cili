"""自动升级模块：新版本代码包 zip 的下载与应用，两段式分工。

- 下载（download_upgrade_zip，由启动后台线程与 Web UI 升级按钮触发；
  scripts/upgrade.ps1 手动下载也落到同一位置）：检查 GitHub 远端版本，
  有更新则把代码包 zip 下载到脚本目录（项目根，cili-update.zip），不动本地代码。
- 应用（main.py 启动时调用 apply_pending_upgrade）：启动时检测到脚本目录有
  升级包，先比对包内 version.json 与本地版本，包版本更高才解压覆盖
  （备份 + 残余清理 + 失败回滚），然后继续启动；不更高则删除升级包。

覆盖逻辑只在 apply_zip 一处实现（下载方不直接改代码，统一收敛到启动时应用）。

版本比对来源是 web/static/version.json 的 version 字段（格式 vYYYYMMDD），
由 .githooks pre-commit hook 每次提交时自动更新为当天日期：
- 本地 version.json 缺失或不可读时按 0.0.0 处理
- 远端 version.json 拉取失败视为"检查失败"，跳过升级（不影响服务启动）

用法:
    from core.updater import start_auto_upgrade
    start_auto_upgrade()      # 后台线程：延迟后检查版本，有更新则下载升级包
    apply_pending_upgrade()   # 启动早期调用：应用脚本目录下已下载的升级包
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from core.config import PROJECT_ROOT
from core.memory_store import cleanup_memory_git

logger = logging.getLogger(__name__)

# 本地版本文件（version 字段，与 GitHub 仓库 web/static/version.json 对应）
VERSION_FILE = PROJECT_ROOT / "web" / "static" / "version.json"

# 升级包文件名（下载到脚本目录，即项目根目录，启动时从这里检测）
UPGRADE_ZIP_NAME = "cili-update.zip"


# 代码包镜像 URL（与 scripts/upgrade.ps1 一致）
MIRROR_URLS = {
    "github": "https://github.com/one-leaf/cili/archive/refs/heads/main.zip",
    "ghproxy": "https://ghproxy.net/https://github.com/one-leaf/cili/archive/refs/heads/main.zip",
    "ghfast": "https://ghfast.top/https://github.com/one-leaf/cili/archive/refs/heads/main.zip",
    "gh-proxy": "https://gh-proxy.com/https://github.com/one-leaf/cili/archive/refs/heads/main.zip",
}

# 版本文件 URL（raw 直连 + 国内镜像回退）
VERSION_URLS = [
    "https://raw.githubusercontent.com/one-leaf/cili/main/web/static/version.json",
    "https://ghproxy.net/https://raw.githubusercontent.com/one-leaf/cili/main/web/static/version.json",
    "https://ghfast.top/https://raw.githubusercontent.com/one-leaf/cili/main/web/static/version.json",
    "https://gh-proxy.com/https://raw.githubusercontent.com/one-leaf/cili/main/web/static/version.json",
]

# 升级时排除的顶层目录（用户数据与版本控制目录不覆盖）
_EXCLUDE_DIRS = {"data", "workspace", ".git"}

# GitHub 仓库归档解压出的根目录前缀（{repo}-{branch}，one-leaf/cili@main → cili-main）
_ARCHIVE_ROOT_PREFIX = "cili-main"

# 应用升级锁：启动应用升级时互斥，防止并发覆盖运行代码
_upgrade_lock = threading.Lock()

# 下载锁 + 进行中标记：后台自动下载与 Web UI 手动下载互斥（W7）
_download_lock = threading.Lock()
_download_in_progress = False


def get_upgrade_zip_path() -> Path:
    """升级包存放路径（脚本目录，即项目根目录）。"""
    return PROJECT_ROOT / UPGRADE_ZIP_NAME


def get_local_version() -> str:
    """读取本地版本号（version.json 的 version 字段）；缺失或不可读时返回 0.0.0。"""
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
    rv, lv = parse_version(remote), parse_version(local)
    n = max(len(rv), len(lv))
    return rv + (0,) * (n - len(rv)) > lv + (0,) * (n - len(lv))


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
    """获取 GitHub 仓库 version.json 的 version 字段。"""
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


def read_zip_version(zip_path: str) -> str | None:
    """读取升级包内 web/static/version.json 的 version 字段；缺失或损坏返回 None。"""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                # 兼容带仓库根目录（cili-main/...）与无根目录两种包结构
                if name == "web/static/version.json" or name.endswith("/web/static/version.json"):
                    data = json.loads(zf.read(info).decode("utf-8"))
                    return str(data.get("version", "")).strip() or None
    except Exception as e:
        logger.warning(f"[updater] 读取升级包版本失败: {e}")
    return None


def is_downloading() -> bool:
    """是否有升级包下载任务进行中。"""
    return _download_in_progress


def download_upgrade_zip(remote_version: str | None = None) -> dict:
    """下载新版本代码包到脚本目录（只下载不应用，重启后由 apply_pending_upgrade 应用）。

    已有相同版本的升级包时跳过重复下载；下载先落临时文件，校验通过后原子替换，
    避免半成品包被误认为有效升级包。

    Args:
        remote_version: 已知的远端版本号；缺省时现场检查 GitHub。

    Returns:
        成功: {"success": True, "message": str, "skipped": bool}
        失败: {"success": False, "error": str}
    """
    global _download_in_progress
    if not _download_lock.acquire(blocking=False):
        return {"success": False, "error": "已有下载任务进行中"}
    _download_in_progress = True
    try:
        if remote_version is None:
            remote_version = fetch_remote_version()
        if not remote_version:
            return {"success": False, "error": "版本检查失败（网络异常）"}

        zip_path = get_upgrade_zip_path()
        if zip_path.exists():
            existing = read_zip_version(str(zip_path))
            if existing == remote_version:
                return {"success": True, "skipped": True,
                        "message": f"升级包 {remote_version} 已下载，重启服务后自动应用"}

        tmp_path = zip_path.parent / (zip_path.name + ".downloading")
        try:
            downloaded, last_error = _download_zip(list(MIRROR_URLS.values()), str(tmp_path))
            if not downloaded:
                return {"success": False, "error": f"所有镜像均下载失败：{last_error}"}
            # 落盘前校验：包损坏或缺版本信息直接判失败，不占用升级包路径
            if read_zip_version(str(tmp_path)) is None:
                return {"success": False, "error": "下载的升级包无效（缺少版本信息或已损坏）"}
            os.replace(str(tmp_path), str(zip_path))
        finally:
            tmp_path.unlink(missing_ok=True)

        return {"success": True, "skipped": False,
                "message": f"升级包 {remote_version} 已下载，重启服务后自动应用"}
    finally:
        _download_in_progress = False
        _download_lock.release()


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
        if name.startswith(_ARCHIVE_ROOT_PREFIX):
            return os.path.join(dest_dir, name)
    logger.error(f"[updater] 解压后未找到 {_ARCHIVE_ROOT_PREFIX} 目录")
    return None


def _files_equal(a: str, b: str) -> bool:
    """两文件内容是否完全一致（先比大小，再逐块比对；读取失败视为不同）。"""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca = fa.read(1024 * 1024)
                cb = fb.read(1024 * 1024)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


def _copy_tree(src: str, dst: str, exclude_dirs: set[str]) -> None:
    """递归复制文件树，跳过 exclude_dirs 中的顶层目录。

    内容未变的文件跳过写入（与 scripts/upgrade.ps1 的 Copy-WithLog 检查逻辑一致），
    避免改写未变更文件的 mtime，也减少对运行中文件的无谓写入。
    """
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if item in exclude_dirs:
            continue
        if os.path.isdir(s):
            os.makedirs(d, exist_ok=True)
            _copy_tree(s, d, exclude_dirs)
        elif os.path.exists(d) and _files_equal(s, d):
            continue
        else:
            shutil.copy2(s, d)


def _prune_dir(dst_dir: str, src_dir: str) -> None:
    """递归删除 dst_dir 中 src_dir 不存在的内容（共享代码目录内部的清理）。"""
    for item in os.listdir(dst_dir):
        s = os.path.join(dst_dir, item)
        if not os.path.exists(os.path.join(src_dir, item)):
            if os.path.isdir(s) and not os.path.islink(s):
                shutil.rmtree(s)
            else:
                os.remove(s)
        elif os.path.isdir(s) and not os.path.islink(s):
            _prune_dir(s, os.path.join(src_dir, item))


def _prune_stale_files(dst: str, src: str, exclude_dirs: set[str]) -> None:
    """删除升级前存在、但新版本代码包已移除的残余文件/目录。

    覆盖语义只处理"新增/覆盖"，不处理"删除"，导致旧版曾发布、新版已移除的
    文件（以及 __pycache__ 等生成物）成为残余；本函数补齐删除语义。

    仅清理新版本仍保留的代码目录内部（如 core/cron.d/ 下被移除的配置）；
    顶层本地目录（.claude/、reference/、.pytest_cache/、__pycache__/ 等不在
    代码包中的目录）与本地顶层文件整体保留，避免误删用户本地资源。
    """
    top_src_dirs = set()
    if os.path.isdir(src):
        for name in os.listdir(src):
            if os.path.isdir(os.path.join(src, name)):
                top_src_dirs.add(name)
    for item in os.listdir(dst):
        if item in exclude_dirs:
            continue
        abs_item = os.path.join(dst, item)
        if os.path.isdir(abs_item) and not os.path.islink(abs_item):
            # 本地目录（新代码包中不存在）整体保留，不进入清理
            if item not in top_src_dirs:
                continue
            _prune_dir(abs_item, os.path.join(src, item))
        # 顶层文件：新版本仍在的文件已由覆盖阶段处理；本地独有文件一律保留


def _restore_tree(backup: str, dst: str, exclude_dirs: set[str]) -> str | None:
    """回滚：用备份恢复被覆盖文件，再删除升级新增的文件/目录。

    Returns:
        str | None: None 表示成功，否则返回错误信息。
    """
    try:
        _copy_tree(backup, dst, exclude_dirs)
        # 删除升级引入、备份中不存在的文件与空目录，使代码树回到升级前状态
        for root, dirs, files in os.walk(dst):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for f in files:
                abs_path = os.path.join(root, f)
                rel = os.path.relpath(abs_path, dst)
                if not os.path.exists(os.path.join(backup, rel)):
                    os.remove(abs_path)
        for root, dirs, files in os.walk(dst, topdown=False):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            rel = os.path.relpath(root, dst)
            if rel == "." or os.path.exists(os.path.join(backup, rel)):
                continue
            try:
                os.rmdir(root)
            except OSError:
                pass  # 非空目录（还有备份中也存在的子文件）忽略
        return None
    except Exception as e:
        logger.error(f"[updater] 回滚失败: {e}")
        return str(e)


def _cleanup_memory_git_dirs() -> int:
    """清理所有工作区 memory 目录下的残留 .git 目录（历史版本曾为每个工作区建立独立 git 仓库）。

    共享工作区场景下 memory 不再使用 git 做版本控制，残留的 .git 目录浪费空间，
    且可能在多用户并发写入时引发锁冲突。本函数扫描所有已知工作区的 memory 目录，
    删除其中的 .git 和 .git.bak.* 目录。

    Returns:
        int: 删除的目录总数
    """
    removed = 0
    try:
        from core.config import (
            SYSTEM_DATA_DIR,
            get_workspace_data_dir,
            load_workspaces_index,
        )

        # 系统工作区
        system_memory = SYSTEM_DATA_DIR / "memory"
        if system_memory.is_dir():
            removed += cleanup_memory_git(system_memory)

        # 所有注册用户工作区
        for entry in load_workspaces_index():
            uuid = entry.get("uuid", "")
            if uuid == "system":
                continue
            try:
                mem_dir = get_workspace_data_dir(uuid) / "memory"
                if mem_dir.is_dir():
                    removed += cleanup_memory_git(mem_dir)
            except Exception:
                pass
    except Exception as e:
        logger.warning("[updater] 清理 memory git 目录失败: %s", e)
    return removed


def apply_zip(zip_path: str) -> dict:
    """将升级包 zip 解压覆盖到项目目录（不含下载）。

    覆盖前先备份当前代码；覆盖后删除新版本已移除的残余文件/目录
    （_prune_stale_files），避免旧版文件残留。任一阶段失败自动回滚，
    避免代码树不一致导致无法启动。

    Returns:
        成功: {"success": True, "message": str}
        失败: {"success": False, "error": str}
    """
    if not _upgrade_lock.acquire(blocking=False):
        return {"success": False, "error": "已有升级任务进行中，请稍后重试"}

    old_version = get_local_version()
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_extract = os.path.join(temp_dir, "extract")
            backup_dir = os.path.join(temp_dir, "backup")

            extracted_dir = _safe_extract(zip_path, temp_extract)
            if not extracted_dir:
                return {"success": False, "error": "解压失败或解压目录未找到"}

            # 覆盖前备份当前代码（data/workspace/.git 不受升级影响，无需备份）
            try:
                os.makedirs(backup_dir, exist_ok=True)
                _copy_tree(str(PROJECT_ROOT), backup_dir, _EXCLUDE_DIRS)
            except Exception as e:
                return {"success": False, "error": f"备份当前代码失败：{str(e)}"}

            try:
                _copy_tree(extracted_dir, str(PROJECT_ROOT), _EXCLUDE_DIRS)
                _prune_stale_files(str(PROJECT_ROOT), extracted_dir, _EXCLUDE_DIRS)
            except Exception as e:
                rollback_error = _restore_tree(backup_dir, str(PROJECT_ROOT), _EXCLUDE_DIRS)
                if rollback_error:
                    return {"success": False, "error": f"更新文件失败：{e}；回滚也失败：{rollback_error}"}
                return {"success": False, "error": f"更新文件失败，已回滚到升级前版本：{str(e)}"}
    finally:
        _upgrade_lock.release()

        # 升级后清理历史遗留的 memory/.git 目录（旧版本曾为每个工作区建立独立 git 仓库）
        cleaned = _cleanup_memory_git_dirs()
        if cleaned:
            logger.info(f"[updater] 已清理 {cleaned} 个残留 memory git 目录")

    new_version = get_local_version()
    change = f"（{old_version} → {new_version}）" if new_version != old_version else ""
    return {"success": True, "message": f"升级完成{change}"}


def _safe_unlink(path: Path) -> None:
    """尽力删除文件，失败仅记录日志（如被杀毒软件短暂占用）。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"[updater] 删除 {path.name} 失败: {e}")


def apply_pending_upgrade() -> dict | None:
    """启动时应用脚本目录下的升级包（main.py 启动早期调用）。

    检查脚本目录下是否存在升级包，存在时：
    - 包内版本高于本地 → 解压覆盖（apply_zip），成功后删除升级包，继续启动
    - 包内版本不高于本地 / 包无效 → 删除升级包，按原版本启动
    - 应用失败 → 保留升级包，下次启动重试

    Returns:
        None 表示无升级包；否则返回处理结果 dict（success/message/error）。
    """
    zip_path = get_upgrade_zip_path()
    # 清理上次下载中断残留的临时文件（正常由 download_upgrade_zip 的 finally 清理）
    _safe_unlink(zip_path.parent / (zip_path.name + ".downloading"))
    if not zip_path.exists():
        return None

    local = get_local_version()
    zip_version = read_zip_version(str(zip_path))
    if zip_version is None:
        logger.warning("[updater] 升级包无效（缺少版本信息或已损坏），删除后按原版本启动")
        _safe_unlink(zip_path)
        return {"success": False, "error": "升级包无效，已删除"}

    if not is_newer_version(zip_version, local):
        logger.info(f"[updater] 升级包版本 {zip_version} 不高于当前版本 {local}，删除升级包")
        _safe_unlink(zip_path)
        return {"success": True, "message": f"升级包版本 {zip_version} 不高于当前版本，已跳过"}

    logger.info(f"[updater] 发现待应用升级包 {zip_version}（当前 {local}），开始解压覆盖...")
    result = apply_zip(str(zip_path))
    if result.get("success"):
        _safe_unlink(zip_path)
        logger.info(f"[updater] {result.get('message')}，继续启动")
    else:
        logger.warning(f"[updater] 应用升级包失败: {result.get('error')}（保留升级包，下次启动重试）")
    return result


def _auto_upgrade_worker(delay: float) -> None:
    try:
        time.sleep(delay)
        # 每次启动都清理一次残留的 memory git 目录（与版本无关，共享工作区准备）
        cleaned = _cleanup_memory_git_dirs()
        if cleaned:
            logger.info(f"[updater] 已清理 {cleaned} 个残留 memory git 目录")
        has_update, local, remote = check_update()
        if remote is None:
            logger.warning("[updater] 版本检查失败（网络异常），跳过自动升级")
            return
        if not has_update:
            logger.info(f"[updater] 已是最新版本（{local}）")
            return
        logger.info(f"[updater] 发现新版本 {remote}（本地 {local}），下载升级包...")
        result = download_upgrade_zip(remote)
        if result.get("success"):
            logger.info(f"[updater] {result.get('message')}")
        else:
            logger.warning(f"[updater] 下载升级包失败: {result.get('error')}")
    except Exception as e:
        logger.error(f"[updater] 自动升级异常: {e}")


def start_auto_upgrade(delay: float = 5.0) -> threading.Thread:
    """后台线程启动自动升级：延迟 delay 秒后检查版本，有新版本则下载升级包到脚本目录。

    只下载不应用——解压覆盖统一在下一次启动时由 apply_pending_upgrade 完成。
    """
    thread = threading.Thread(
        target=_auto_upgrade_worker, args=(delay,), daemon=True, name="AutoUpgrade"
    )
    thread.start()
    return thread
