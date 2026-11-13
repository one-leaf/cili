"""自动升级模块：新版本代码包 zip 的下载与应用，三段式分工。

- 下载（download_upgrade_zip，由启动后台线程与 Web UI 升级按钮触发；
  scripts/upgrade.ps1 手动下载也落到同一位置）：检查 GitHub 远端版本，
  有更新则把代码包 zip 下载到脚本目录（项目根，cili-update.zip），不动本地代码。
- 应用（apply_pending_upgrade，两条路径都收敛到它）：
  a) start.ps1 启动循环：每次拉起服务前先跑 ``main.py --apply-upgrade-only``
     在独立短进程中应用升级包（应用后即退出），服务进程从磁盘全新代码启动，
     不存在新旧代码混跑；
  b) main.py 服务进程内兜底（直接 python main.py 启动等场景）：启动早期检测
     到升级包，包版本不低于本地才解压覆盖（版本一致也应用，用于修复本地文件
     损坏；备份 + 残余清理 + 失败回滚）。应用成功且由 start.ps1 托管时以退出
     码 RESTART_EXIT_CODE 退出，交回启动循环以全新进程重新加载代码。
- 运行中重启（运行期间下载完升级包后）：schedule_idle_restart 等待会话空闲
  （registry / 排队回合 / 后台子代理 / cron 均无运行），request_restart 通知
  uvicorn 优雅停机，main.py 在 uvicorn.run 返回后以 RESTART_EXIT_CODE 退出
  交回启动循环——重启后走路径 a 应用升级包并加载新代码。

重启协议只在 start.ps1 托管下生效（启动前设 CILI_LAUNCHER=ps1，重启迭代前设
CILI_LAUNCH_RESTART=1 抑制浏览器自动打开）。直接 python main.py 启动时无循环
接管，进程退出后无人拉起，因此自动重启与手动重启端点均拒绝执行，保持旧行为：
提示「重启服务后自动应用」。

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
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT
from core.memory_store import cleanup_memory_git

logger = logging.getLogger(__name__)

# 本地版本文件（version 字段，与 GitHub 仓库 web/static/version.json 对应）
VERSION_FILE = PROJECT_ROOT / "web" / "static" / "version.json"

# 升级包文件名（下载到脚本目录，即项目根目录，启动时从这里检测）
UPGRADE_ZIP_NAME = "cili-update.zip"

# ── 启动脚本托管的重启协议 ──────────────────────────────────────────────
# start.ps1 启动服务前设 CILI_LAUNCHER=ps1；重启迭代前设 CILI_LAUNCH_RESTART=1
#（抑制浏览器自动打开）。服务请求重启时以 RESTART_EXIT_CODE 退出，start.ps1
# 据此重新走「应用升级包 → 启动」循环。直接 python main.py 启动时无循环托管，
# 自动/手动重启不可用（退出后无人拉起，服务会直接停掉）。
LAUNCHER_ENV = "CILI_LAUNCHER"
LAUNCH_RESTART_ENV = "CILI_LAUNCH_RESTART"
RESTART_EXIT_CODE = 42


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


def is_launcher_managed() -> bool:
    """当前服务是否由 start.ps1 启动循环托管（决定能否自动重启）。"""
    return os.environ.get(LAUNCHER_ENV, "") == "ps1"


def _post_download_hint() -> str:
    """下载完成后的提示语（是否自动重启取决于启动方式）。"""
    return "将在空闲时自动重启应用" if is_launcher_managed() else "重启服务后自动应用"


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


# 最近一次升级包下载的结果（自动 worker 与 Web UI 手动下载**共用同一份**，
# UI 轮询 /api/update/status 读取）。busy 拒绝不写这里——否则会覆盖真正持有
# 下载的线程的最终结果（并发 POST 时第二个线程抢锁失败就会发生）。
_last_download_result: dict | None = None


def get_last_download_result() -> dict | None:
    """最近一次升级包下载的结果（自动/手动共用）；尚未下载过为 None。"""
    return _last_download_result


def reset_last_download_result() -> None:
    """清空结果（新的手动下载开始时调用，让 UI 区分「本次」与「上次」）。"""
    global _last_download_result
    _last_download_result = None


def download_upgrade_zip(remote_version: str | None = None) -> dict:
    """下载新版本代码包到脚本目录（只下载不应用，重启后由 apply_pending_upgrade 应用）。

    已有相同版本的升级包时跳过重复下载；下载先落临时文件，校验通过后原子替换，
    避免半成品包被误认为有效升级包。

    无论自动 worker 还是 Web 手动下载调用，**结果都记录在模块级**
    ``_last_download_result``——UI 轮询状态接口时据此判断结束并展示成败。
    busy 拒绝不记录（它不属于本次下载）。

    Args:
        remote_version: 已知的远端版本号；缺省时现场检查 GitHub。

    Returns:
        成功: {"success": True, "message": str, "skipped": bool}
        失败: {"success": False, "error": str}
        忙:   {"success": False, "error": str, "busy": True}
    """
    global _download_in_progress, _last_download_result
    if not _download_lock.acquire(blocking=False):
        return {"success": False, "error": "已有下载任务进行中", "busy": True}
    _download_in_progress = True
    try:
        result = _do_download(remote_version)
    except Exception as e:  # noqa: BLE001 —— 任何异常都要变成可展示的结果
        logger.exception("[updater] 下载升级包异常")
        result = {"success": False, "error": str(e)}
    finally:
        _download_in_progress = False
        _download_lock.release()
    _last_download_result = result
    return result


def _do_download(remote_version: str | None) -> dict:
    """实际下载流程（调用方持有下载锁并负责记录结果）。"""
    if remote_version is None:
        remote_version = fetch_remote_version()
    if not remote_version:
        return {"success": False, "error": "版本检查失败（网络异常）"}

    zip_path = get_upgrade_zip_path()
    if zip_path.exists():
        existing = read_zip_version(str(zip_path))
        if existing == remote_version:
            return {"success": True, "skipped": True,
                    "message": f"升级包 {remote_version} 已下载，{_post_download_hint()}"}

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
            "message": f"升级包 {remote_version} 已下载，{_post_download_hint()}"}


def _download_zip(urls: list[str], dest: str, timeout: float = 120) -> tuple[bool, str]:
    """依次尝试镜像下载代码包 zip 到 dest。返回 (是否成功, 最后错误信息)。"""
    last_error = ""
    try:
        import httpx

        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for url in urls:
                try:
                    wrote = False
                    with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        with open(dest, "wb") as f:
                            for chunk in resp.iter_bytes():
                                if chunk:
                                    f.write(chunk)
                                    wrote = True
                    if not wrote:
                        raise ValueError("empty response body")
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


def _copy_tree(src: str, dst: str, exclude_dirs: set[str],
               changes: dict | None = None, root: str | None = None) -> None:
    """递归复制文件树，跳过 exclude_dirs 中的顶层目录。

    内容未变的文件跳过写入（与 scripts/upgrade.ps1 的 Copy-WithLog 检查逻辑一致），
    避免改写未变更文件的 mtime，也减少对运行中文件的无谓写入。

    changes 传入 {"added": [], "updated": []} 时收集实际写入的文件（相对 root
    的路径，root 缺省取首次调用的 dst）；None 时不收集（备份/回滚路径）。
    """
    if root is None:
        root = dst
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if item in exclude_dirs:
            continue
        if os.path.isdir(s):
            os.makedirs(d, exist_ok=True)
            _copy_tree(s, d, exclude_dirs, changes, root)
        elif os.path.exists(d) and _files_equal(s, d):
            continue
        else:
            if changes is not None:
                key = "updated" if os.path.exists(d) else "added"
                changes[key].append(os.path.relpath(d, root).replace("\\", "/"))
            shutil.copy2(s, d)


def _prune_dir(dst_dir: str, src_dir: str, removed: list | None = None,
               root: str | None = None) -> None:
    """递归删除 dst_dir 中 src_dir 不存在的内容（共享代码目录内部的清理）。

    removed 传入列表时收集被删除的条目（相对 root 的路径，root 缺省取首次
    调用的 dst_dir）；None 时不收集。
    """
    if root is None:
        root = dst_dir
    for item in os.listdir(dst_dir):
        s = os.path.join(dst_dir, item)
        if not os.path.exists(os.path.join(src_dir, item)):
            if os.path.isdir(s) and not os.path.islink(s):
                shutil.rmtree(s)
            else:
                os.remove(s)
            if removed is not None:
                removed.append(os.path.relpath(s, root).replace("\\", "/"))
        elif os.path.isdir(s) and not os.path.islink(s):
            _prune_dir(s, os.path.join(src_dir, item), removed, root)


def _prune_stale_files(dst: str, src: str, exclude_dirs: set[str],
                       removed: list | None = None) -> None:
    """删除升级前存在、但新版本代码包已移除的残余文件/目录。

    覆盖语义只处理"新增/覆盖"，不处理"删除"，导致旧版曾发布、新版已移除的
    文件（以及 __pycache__ 等生成物）成为残余；本函数补齐删除语义。

    仅清理新版本仍保留的代码目录内部（如 core/cron.d/ 下被移除的配置）；
    顶层本地目录（.claude/、reference/、.pytest_cache/、__pycache__/ 等不在
    代码包中的目录）与本地顶层文件整体保留，避免误删用户本地资源。

    removed 传入列表时收集被删除文件的相对路径（相对 dst）。
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
            _prune_dir(abs_item, os.path.join(src, item), removed, dst)
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
        成功: {"success": True, "message": str,
               "changes": {"updated": [...], "added": [...], "deleted": [...]}}
               changes 为实际写入/删除文件的相对路径清单（内容未变的文件不列入）
        失败: {"success": False, "error": str}
    """
    if not _upgrade_lock.acquire(blocking=False):
        return {"success": False, "error": "已有升级任务进行中，请稍后重试"}

    old_version = get_local_version()
    changes = {"updated": [], "added": [], "deleted": []}
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
                _copy_tree(extracted_dir, str(PROJECT_ROOT), _EXCLUDE_DIRS, changes)
                _prune_stale_files(str(PROJECT_ROOT), extracted_dir, _EXCLUDE_DIRS,
                                   changes["deleted"])
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

    for files in changes.values():
        files.sort()
    # __pycache__/.pyc 属运行产物回收而非版本变更，每次升级都会成批删除，不进报告
    changes["deleted"] = [p for p in changes["deleted"]
                          if "__pycache__" not in p and not p.endswith((".pyc", ".pyo"))]

    new_version = get_local_version()
    version_part = (f"{old_version} → {new_version}"
                    if new_version != old_version else f"{new_version}，版本一致")
    parts = []
    if changes["updated"]:
        parts.append(f"更新 {len(changes['updated'])} 个")
    if changes["added"]:
        parts.append(f"新增 {len(changes['added'])} 个")
    if changes["deleted"]:
        parts.append(f"删除 {len(changes['deleted'])} 个")
    detail = "，".join(parts) if parts else "无文件变更"
    return {"success": True, "message": f"升级完成（{version_part}）：{detail}",
            "changes": changes}


def _safe_unlink(path: Path) -> bool:
    """尽力删除文件，失败仅记录日志（如被杀毒软件短暂占用）。返回是否删除成功。"""
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as e:
        logger.warning(f"[updater] 删除 {path.name} 失败: {e}")
        return False


def _log_changes(changes: dict | None) -> None:
    """逐条列出升级变更的文件（启动日志中可见哪些文件被更新/新增/删除）。"""
    if not changes:
        return
    for label, key in (("更新", "updated"), ("新增", "added"), ("删除", "deleted")):
        for rel in changes.get(key, ()):
            logger.info(f"[updater] {label}: {rel}")


def apply_pending_upgrade() -> dict | None:
    """启动时应用脚本目录下的升级包（main.py 启动早期调用）。

    检查脚本目录下是否存在升级包，存在时：
    - 包内版本不低于本地 → 解压覆盖（apply_zip；版本一致也应用，用于修复
      本地文件损坏），成功后删除升级包，继续启动并逐条列出变更文件
    - 本地版本高于包内（防降级）/ 包无效 → 删除升级包，按原版本启动
    - 应用失败 → 保留升级包，下次启动重试

    Returns:
        None 表示无升级包；否则返回处理结果 dict（success/message/error，
        应用成功且升级包删除成功时额外带 restart=True——调用方据此决定是否
        交回启动循环以全新进程加载新代码）。
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

    if is_newer_version(local, zip_version):
        logger.info(f"[updater] 本地版本 {local} 高于升级包 {zip_version}，跳过（防降级），删除升级包")
        _safe_unlink(zip_path)
        return {"success": True, "message": f"本地版本 {local} 高于升级包 {zip_version}，已跳过"}

    logger.info(f"[updater] 发现待应用升级包 {zip_version}（当前 {local}），开始解压覆盖...")
    result = apply_zip(str(zip_path))
    if result.get("success"):
        # 删除失败时不置 restart 标记：升级包残留会让下次启动重复应用，
        # 若配合启动循环的退出码协议会形成重启循环
        if _safe_unlink(zip_path):
            result["restart"] = True
            logger.info(f"[updater] {result.get('message')}，继续启动")
        else:
            logger.warning("[updater] 升级包删除失败，本次不请求重启（下次启动重试删除）")
        _log_changes(result.get("changes"))
    else:
        logger.warning(f"[updater] 应用升级包失败: {result.get('error')}（保留升级包，下次启动重试）")
    return result


# ── 运行中重启（升级包下载完成后的自动重启 / Web UI 手动重启） ─────────────
# 协议：request_restart 记录原因并通知 uvicorn 优雅停机 → uvicorn.run 返回后
# main.py 调 shutdown_and_restart_if_requested，以 RESTART_EXIT_CODE 退出 →
# start.ps1 检测退出码重新走「应用升级包 → 启动」循环，新进程加载全新代码。

_uvicorn_server: Any = None
_restart_requested: str | None = None
_restart_lock = threading.Lock()


def set_uvicorn_server(server: Any) -> None:
    """main.py 启动 uvicorn 后注入 Server 句柄（request_restart 需要它触发优雅停机）。"""
    global _uvicorn_server
    _uvicorn_server = server


def request_restart(reason: str) -> bool:
    """请求优雅重启：记录原因并通知 uvicorn 停机。

    仅在 start.ps1 托管下可用（退出码协议需要启动循环接住）。重启流程已在
    进行中时幂等返回 True。
    """
    global _restart_requested
    if not is_launcher_managed():
        logger.warning("[updater] 非 start.ps1 托管启动，无法自动重启（请改用 start.cmd/start.ps1）")
        return False
    with _restart_lock:
        if _restart_requested:
            return True
        _restart_requested = reason
    logger.info(f"[updater] 请求重启: {reason}")
    if _uvicorn_server is not None:
        _uvicorn_server.should_exit = True
    return True


def shutdown_and_restart_if_requested() -> None:
    """main.py 在 uvicorn.run 返回后调用：有未决重启请求时以退出码交回启动循环。

    此刻进程已完成优雅停机（lifespan shutdown / app_context.shutdown 均已
    执行），sys.exit 即可安全退出。
    """
    global _restart_requested
    with _restart_lock:
        reason = _restart_requested
        _restart_requested = None
    if reason:
        logger.info(
            f"[updater] {reason}，以退出码 {RESTART_EXIT_CODE} 交回启动脚本"
            "（应用升级包后以新代码重启）"
        )
        sys.exit(RESTART_EXIT_CODE)


def all_sessions_idle() -> bool:
    """升级自动重启的空闲判定：无运行中会话、无排队回合、无后台子代理与 cron 任务。

    各子系统逐一探测，任一探测器不可用（导入失败等）按该子系统空闲处理——
    判定目标是「别打断正在跑的工作」，失败只意味着没探测到活动。
    """
    try:
        from core.session_registry import registry
        if registry.any_running():
            return False
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.turn_queue import queue as turn_queue
        if turn_queue.any_pending():
            return False
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.tools.background import any_background_runner_running
        if any_background_runner_running():
            return False
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.cron import any_cron_runner_running
        if any_cron_runner_running():
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def _auto_restart_enabled() -> bool:
    """读取 setting.json 的 system.auto_restart_after_upgrade，未配置时默认开启。"""
    try:
        from core.config import load_global_config
        cfg = load_global_config()
        return bool((cfg.get("system") or {}).get("auto_restart_after_upgrade", True))
    except Exception:  # noqa: BLE001
        return True


_idle_restart_scheduled = False
_idle_restart_lock = threading.Lock()


def is_idle_restart_waiting() -> bool:
    """是否已有「等空闲自动重启」任务在等待中。"""
    return _idle_restart_scheduled


def schedule_idle_restart(reason: str, poll_interval: float = 30.0,
                          max_wait: float = 1800.0,
                          confirm_delay: float = 3.0) -> bool:
    """升级包下载完成后安排「等空闲即优雅重启」。

    空闲判定见 all_sessions_idle；确认空闲后由 request_restart 触发优雅停机，
    进程以 RESTART_EXIT_CODE 退出、由 start.ps1 循环重新拉起（先应用升级包）。
    超过 max_wait 仍未空闲则放弃（升级包保留，下次启动或手动重启时应用）。
    同一时刻只允许一个等待任务；仅在 start.ps1 托管且未关闭自动重启时生效。

    Args:
        reason: 重启原因（日志用）。
        poll_interval: 空闲轮询间隔（秒）。
        max_wait: 最长等待空闲时间（秒），超时放弃。
        confirm_delay: 空闲确认后的二次确认前等待（秒），避免恰好踩中
            「刚认领运行权但尚未标记运行」的会话窗口。
    """
    global _idle_restart_scheduled
    if not is_launcher_managed():
        return False
    if not _auto_restart_enabled():
        logger.info("[updater] 自动重启已关闭（system.auto_restart_after_upgrade=false），跳过")
        return False
    with _idle_restart_lock:
        if _idle_restart_scheduled:
            return False
        _idle_restart_scheduled = True

    def _worker() -> None:
        global _idle_restart_scheduled
        deadline = time.monotonic() + max_wait
        try:
            while time.monotonic() < deadline:
                if all_sessions_idle():
                    time.sleep(confirm_delay)
                    if all_sessions_idle():
                        if request_restart(reason):
                            return
                        logger.warning("[updater] 发起重启失败，放弃自动重启（升级包保留）")
                        return
                time.sleep(poll_interval)
            logger.warning("[updater] 等待空闲超时，取消自动重启（升级包保留，重启服务后应用）")
        finally:
            with _idle_restart_lock:
                _idle_restart_scheduled = False

    threading.Thread(target=_worker, daemon=True, name="UpgradeIdleRestart").start()
    return True


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
            # 下载完成后安排「等空闲自动重启」（仅 start.ps1 托管且未关闭时生效）
            if schedule_idle_restart(f"自动下载的升级包 {remote} 已就绪"):
                logger.info("[updater] 已安排：等待会话空闲后自动重启应用升级包")
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
