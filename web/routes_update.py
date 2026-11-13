"""系统升级 路由域：版本检查、升级包下载（只下载，启动循环统一应用）、
状态查询、手动重启。"""

from __future__ import annotations

import asyncio
import logging
import threading

from fastapi import APIRouter

from core import updater

router = APIRouter()
logger = logging.getLogger(__name__)

# 手动下载的「启动中」窗口：POST 返回后、线程真正开始下载前，is_downloading()
# 仍是 False。用同步标志位兜住，避免连点两次起两个线程（并发下结果覆盖问题
# 已由 core.updater 的结果归属解决——busy 拒绝不写 last_result）。
_start_guard = threading.Lock()
_starting = False


def _download_worker() -> None:
    """后台线程执行升级包下载；结果由 core.updater 统一记录，状态接口直接读。"""
    global _starting
    try:
        result = updater.download_upgrade_zip()
        logger.info(
            "[update] 升级包下载结束: %s", result.get("message") or result.get("error")
        )
        # 下载成功后安排「等空闲自动重启」（仅 start.ps1 托管且未关闭时生效）
        if result.get("success") and updater.schedule_idle_restart("升级包下载完成"):
            logger.info("[update] 已安排：等待会话空闲后自动重启应用升级包")
    except Exception as e:  # noqa: BLE001 —— 线程异常只记日志，状态接口仍会结束
        logger.exception("[update] 升级包下载线程异常: %s", e)
    finally:
        with _start_guard:
            _starting = False


@router.get("/api/update/check")
async def check_update():
    """检查 GitHub 远端版本（网络请求放线程池，避免阻塞事件循环）。"""
    has_update, local, remote = await asyncio.to_thread(updater.check_update)
    return {
        "has_update": has_update,
        "local_version": local,
        "remote_version": remote,
        "error": None if remote is not None else "版本检查失败（网络异常）",
    }


@router.get("/api/update/status")
async def update_status():
    """升级状态：本地/升级包版本、是否下载中、最近一次下载结果、启动方式。"""
    zip_path = updater.get_upgrade_zip_path()
    zip_exists = zip_path.exists()
    zip_version = updater.read_zip_version(str(zip_path)) if zip_exists else None
    return {
        "local_version": updater.get_local_version(),
        # _starting 也算「下载中」：POST 已返回但线程尚未置位 is_downloading 的窗口
        "downloading": updater.is_downloading() or _starting,
        "last_result": updater.get_last_download_result(),
        "zip_present": zip_exists,
        "zip_version": zip_version,
        # 前端据此展示「自动重启」提示与立即重启按钮
        "launcher_managed": updater.is_launcher_managed(),
    }


@router.post("/api/update/download")
async def download_update():
    """下载升级包到脚本目录（后台线程执行；start.ps1 托管时空闲后自动重启应用）。"""
    global _starting
    with _start_guard:
        if updater.is_downloading() or _starting:
            return {"success": False, "error": "已有下载任务进行中"}
        _starting = True
    updater.reset_last_download_result()
    try:
        threading.Thread(target=_download_worker, daemon=True, name="UpdateDownload").start()
    except Exception:
        with _start_guard:
            _starting = False
        raise
    return {"success": True, "message": "开始下载升级包，完成后将自动重启应用"}


@router.post("/api/update/restart")
async def restart_service():
    """立即重启服务以应用已就绪的升级包（有会话运行中时拒绝）。

    仅 start.ps1 托管下可用：request_restart 触发优雅停机，进程以退出码 42
    交回启动循环，由循环重新应用升级包并拉起新实例。
    """
    if not updater.is_launcher_managed():
        return {"success": False,
                "error": "当前为直接启动（python main.py），自动重启不可用；请手动重启服务"}
    if not updater.get_upgrade_zip_path().exists():
        return {"success": False, "error": "没有待应用的升级包"}
    if updater.is_downloading() or _starting:
        return {"success": False, "busy": True, "error": "升级包正在下载，请稍后再试"}
    if not updater.all_sessions_idle():
        return {"success": False, "busy": True, "error": "有会话正在运行，请等待结束后重试"}
    if not updater.request_restart("Web UI 手动触发重启"):
        return {"success": False, "error": "发起重启失败"}
    return {"success": True, "message": "服务重启中，新实例将自动应用升级包"}
