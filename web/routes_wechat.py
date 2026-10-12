"""微信接入端路由：扫码登录、状态、登出。

**为什么单独一个路由域**（而不是塞进 ``routes_config``）：扫码登录是一套有状态的
流程（取码 → 轮询 → 拿凭证），与「读写 setting.json」是两回事；凭证也不进
setting.json（见 ``interfaces/wechat/account.py``），所以这里与 ``/api/config``
没有共享的写入路径。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from interfaces.wechat.account import clear_account, load_account
from interfaces.wechat.config import load_wechat_config
from interfaces.wechat.login import WechatLoginStore
from interfaces.wechat.protocol import WechatApiError

router = APIRouter()
logger = logging.getLogger(__name__)

# 扫码会话表：进程级单例（每个会话自带 600s TTL）。测试 monkeypatch 本模块的 _store。
_store: WechatLoginStore | None = None


def get_login_store() -> WechatLoginStore:
    """取（惰性建）扫码会话表。"""
    global _store
    if _store is None:
        _store = WechatLoginStore()
    return _store


# ─── 接入端生命周期的小助手 ───────────────────────────────────────────
#
# 接入端本身由 app_context 启动（Stage 3 落地）。这里全部防御式导入：登录/状态
# 查询不该因为「接入端没启用、没加载」而报 500。


def _interface_running() -> bool:
    try:
        from interfaces.wechat import wechat_interface_running
        return bool(wechat_interface_running())
    except Exception:  # noqa: BLE001 — 未加载就是「没在跑」
        return False


async def _stop_interface_if_running() -> bool:
    """停掉接入端。返回是否真的停了一个在跑的实例。"""
    try:
        from interfaces.wechat import stop_wechat_interface, wechat_interface_running
    except Exception:  # noqa: BLE001
        return False
    if not wechat_interface_running():
        return False
    # 启停会等宿主线程就绪/退出（数秒），阻塞操作 → 丢线程池
    await asyncio.to_thread(stop_wechat_interface)
    return True


async def _restart_interface_if_running() -> None:
    """登录态变了 → 重启轮询（旧 client 还攥着旧 token）。"""
    if not await _stop_interface_if_running():
        return
    try:
        from interfaces.wechat import start_wechat_interface
    except Exception:  # noqa: BLE001
        return
    await asyncio.to_thread(start_wechat_interface)


# ─── 扫码登录 ─────────────────────────────────────────────────────────


@router.post("/api/wechat/login/start")
async def wechat_login_start(force: bool = False) -> dict[str, Any]:
    """开始扫码登录。``force=true`` 时忽略本地已有凭证，强制重新扫码。"""
    try:
        return await get_login_store().start(force=force)
    except WechatApiError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/api/wechat/login/status")
async def wechat_login_status(
    session_id: str = Query(""),
    verify_code: str = Query(""),
) -> dict[str, Any]:
    """轮询扫码状态（前端每 2s 调一次）。拿到凭证后顺带重启轮询。"""
    if not session_id:
        raise HTTPException(status_code=400, detail="缺少 session_id")
    result = await get_login_store().poll(session_id, verify_code=verify_code)
    if result.get("status") == "succeeded":
        await _restart_interface_if_running()
    return result


@router.post("/api/wechat/login/cancel")
async def wechat_login_cancel(session_id: str = Query("")) -> dict[str, Any]:
    """取消扫码登录（关掉该会话的 HTTP 客户端）。"""
    if not session_id:
        raise HTTPException(status_code=400, detail="缺少 session_id")
    return await get_login_store().cancel(session_id)


# ─── 状态与登出 ───────────────────────────────────────────────────────


@router.get("/api/wechat/status")
async def wechat_status() -> dict[str, Any]:
    """接入端状态。**不含任何凭证**（token 只存在于 account.json）。"""
    cfg = load_wechat_config()
    account = load_account()
    return {
        "enabled": cfg.enabled,
        "running": _interface_running(),
        "logged_in": account.has_token(),
        "ilink_user_id": account.ilink_user_id,
        "ilink_bot_id": account.ilink_bot_id,
        "base_url": account.base_url or cfg.base_url,
    }


@router.post("/api/wechat/logout")
async def wechat_logout() -> dict[str, Any]:
    """登出：先停接入端再清凭证。

    顺序不能反——轮询还攥着 token 的时候把文件删了，它会一直拿着失效凭证转。
    """
    stopped = await _stop_interface_if_running()
    cleared = clear_account()
    return {
        "success": cleared,
        "stopped": stopped,
        "message": "已登出，请重新扫码登录。" if cleared else "清除本地凭证失败。",
    }
