"""系统升级 路由域：版本检查、升级包下载（只下载，重启时统一应用）、状态查询。"""

from __future__ import annotations

import asyncio
import logging
import threading

from fastapi import APIRouter

from core import updater

router = APIRouter()
logger = logging.getLogger(__name__)

# 最近一次升级包下载结果（下载在后台线程执行，UI 轮询 /api/update/status 读取）
_last_download_result: dict | None = None


def _download_worker() -> None:
    """后台线程执行升级包下载，结果写入 _last_download_result 供状态接口读取。"""
    global _last_download_result
    try:
        result = updater.download_upgrade_zip()
    except Exception as e:
        result = {"success": False, "error": str(e)}
    _last_download_result = result
    logger.info("[update] 升级包下载结束: %s", result.get("message") or result.get("error"))


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
    """升级状态：本地/升级包版本、是否下载中、最近一次下载结果。"""
    zip_path = updater.get_upgrade_zip_path()
    zip_exists = zip_path.exists()
    zip_version = updater.read_zip_version(str(zip_path)) if zip_exists else None
    return {
        "local_version": updater.get_local_version(),
        "downloading": updater.is_downloading(),
        "last_result": _last_download_result,
        "zip_present": zip_exists,
        "zip_version": zip_version,
    }


@router.post("/api/update/download")
async def download_update():
    """下载升级包到脚本目录（后台线程执行，完成后重启服务自动应用）。"""
    global _last_download_result
    if updater.is_downloading():
        return {"success": False, "error": "已有下载任务进行中"}
    _last_download_result = None
    threading.Thread(target=_download_worker, daemon=True, name="UpdateDownload").start()
    return {"success": True, "message": "开始下载升级包，完成后重启服务自动应用"}
