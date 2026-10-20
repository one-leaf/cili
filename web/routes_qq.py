"""QQ 接入端路由：扫码绑定（onboard）、状态、断开连接（按账号寻址）。

**为什么单独一个路由域**（与微信同理）：扫码绑定是一套有状态的流程（建任务 →
轮询 → 解密落库），与「读写 setting.json」的配置 CRUD 是两回事。凭据虽然落在
setting.json 的 qq 段，但只由 onboard 合并与「断开连接」清除；/api/config 的
表单读写路径不携带明文（secret 只回掩码，app_id/secret 留空 = 继承已存值）。

**多账号**：全部端点按账号寻址（``/api/qq/accounts/{id}/...``），状态接口返回
账号列表。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from interfaces.qq.config import load_qq_accounts

router = APIRouter()
logger = logging.getLogger(__name__)


# ─── 接入端生命周期的小助手 ───────────────────────────────────────────
#
# 接入端本身由 app_context 启动（无参 = 全部账号）。这里全部防御式导入：
# 状态查询/断开连接不该因为「接入端没启用、没加载」而报 500。


def _interface_running(account_id: str) -> bool:
    try:
        from interfaces.qq import qq_interface_running

        return bool(qq_interface_running(account_id))
    except Exception:  # noqa: BLE001 — 未加载就是「没在跑」
        return False


async def _restart_interface(account_id: str) -> None:
    """凭据落库后重启该账号的宿主（旧客户端可能还攥着旧凭据）。

    未启用 / 凭据不齐时 start 内部静默跳过——「扫码成功」本身就能把一个
    enabled 但此前没绑定的账号带起来。
    """
    try:
        from interfaces.qq import start_qq_interface, stop_qq_interface
    except Exception:  # noqa: BLE001
        return
    await asyncio.to_thread(stop_qq_interface, account_id)
    await asyncio.to_thread(start_qq_interface, account_id)


# ─── 状态与断开连接 ───────────────────────────────────────────────────


@router.get("/api/qq/status")
async def qq_status() -> dict[str, Any]:
    """接入端状态（账号列表）。**不含任何凭据**（secret 永不出配置文件）。

    ``app_id`` 不是秘密（QQ 开放平台本就明文展示），用于卡片上识别绑的是哪个
    机器人；``secret`` 只以有无（``bound``）表达。
    """
    accounts: list[dict[str, Any]] = []
    for cfg in load_qq_accounts():
        accounts.append({
            "id": cfg.id,
            "label": cfg.label,
            "enabled": cfg.enabled,
            "running": _interface_running(cfg.id),
            "bound": cfg.is_ready(),
            "app_id": cfg.app_id,
        })
    return {"accounts": accounts}


@router.post("/api/qq/accounts/{account_id}/disconnect")
async def qq_disconnect(account_id: str) -> dict[str, Any]:
    """断开连接：停掉该账号的宿主，并清除已绑定的凭据（重新扫码可再连）。

    与微信「断开连接」（登出清凭证）同语义。启用开关不动——是否还留这个账号
    由用户的「启用」+ 保存决定。
    """
    try:
        from interfaces.qq import stop_qq_interface

        await asyncio.to_thread(stop_qq_interface, account_id)
    except Exception:  # noqa: BLE001 — 停不掉也照样清凭据
        logger.warning(f"[qq] 断开连接：停宿主失败（忽略）: {account_id}", exc_info=True)

    from interfaces.qq.config import save_qq_accounts

    accounts = load_qq_accounts()
    found = False
    for acc in accounts:
        if acc.id == account_id:
            acc.app_id = ""
            acc.secret = ""
            found = True
            break
    if found and not save_qq_accounts(accounts):
        raise HTTPException(status_code=500, detail="Failed to save config")
    return {"success": True, "message": "已断开连接" if found else "该账号尚未绑定"}


# ─── 扫码绑定机器人（凭据自动落库，形态对齐微信扫码登录） ─────────────


@router.post("/api/qq/accounts/{account_id}/onboard/start")
async def qq_onboard_start(account_id: str) -> dict[str, Any]:
    """建绑定任务、生成二维码。AppSecret 经服务端加密回传后只落配置文件。"""
    try:
        from interfaces.qq.onboard import get_onboard_store

        return await get_onboard_store().start(account_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/api/qq/accounts/{account_id}/onboard/status")
async def qq_onboard_status(
    account_id: str,
    session_id: str = Query(""),
) -> dict[str, Any]:
    """轮询扫码绑定状态（前端每 2s 调一次）。完成时凭据已落库并尝试拉起宿主。"""
    if not session_id:
        raise HTTPException(status_code=400, detail="缺少 session_id")
    try:
        from interfaces.qq.onboard import get_onboard_store

        result = await get_onboard_store().poll(session_id)
    except Exception as e:  # noqa: BLE001 — 平台侧失败回给前端在弹框里展示
        logger.warning(f"[qq] 扫码绑定轮询失败: {e}")
        return {"status": "failed", "message": str(e)[:200]}
    if result.get("status") == "succeeded":
        await _restart_interface(account_id)
        result["message"] = (
            "机器人已连接" if _interface_running(account_id)
            else "凭据已保存，勾选「启用」并保存后生效"
        )
    return result


@router.post("/api/qq/accounts/{account_id}/onboard/cancel")
async def qq_onboard_cancel(
    account_id: str,
    session_id: str = Query(""),
) -> dict[str, Any]:
    """取消扫码绑定（丢弃会话）。"""
    if not session_id:
        raise HTTPException(status_code=400, detail="缺少 session_id")
    from interfaces.qq.onboard import get_onboard_store

    return await get_onboard_store().cancel(session_id)
