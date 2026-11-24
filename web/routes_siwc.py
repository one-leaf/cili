"""SIWC（ChatGPT 套餐额度）登录与凭证管理路由。"""

from __future__ import annotations

import asyncio
import logging
import threading

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.llm.siwc import (
    cancel,
    complete_manual,
    poll,
    set_authorized_hook,
    start_login,
)
from core.llm.siwc import (
    logout as siwc_logout,
)
from core.llm.siwc import (
    status as siwc_status,
)
from core.llm.siwc.store import (
    clear_provider_credential,
    provider_credential,
)
from web.deps import csrf_protect, reload_all_runners, teen_guard

router = APIRouter()
logger = logging.getLogger(__name__)

_GUARDS = [Depends(teen_guard("config_update")), Depends(csrf_protect)]


class LoginRequest(BaseModel):
    provider: str = ""
    credential: str | None = None
    provider_config: dict | None = None  # 当前卡片的表单快照（登录成功即保存用）


class CompleteRequest(BaseModel):
    callback_url: str


class LogoutRequest(BaseModel):
    provider: str = ""
    credential: str | None = None


def _on_authorized(
    provider_key: str, credential: str, provider_config: dict | None = None
) -> str:
    """换码成功后直接保存 provider，并后台重载 runner（不依赖前端轮询）。"""
    from core.llm.siwc.store import upsert_oauth_provider

    status = upsert_oauth_provider(provider_key, credential, provider_config)
    if status == "written":
        threading.Thread(
            target=reload_all_runners, daemon=True, name="cili-siwc-reload"
        ).start()
    return status


def register_hooks() -> None:
    """装配点显式注册换码成功回调（不再依赖 import 副作用）。"""
    set_authorized_hook(_on_authorized)


@router.post("/api/config/siwc/login", dependencies=_GUARDS)
async def login_start(request: LoginRequest):
    provider_key = (request.provider or "").strip()
    if not provider_key:
        return {"success": False, "error": "缺少 provider"}
    credential = request.credential or provider_credential(provider_key)
    attempt = await asyncio.to_thread(
        start_login, provider_key, credential, request.provider_config
    )
    return {
        "success": True,
        "attempt_id": attempt.attempt_id,
        "auth_url": attempt.auth_url,
        "redirect_uri": attempt.redirect_uri,
        "expires_at": attempt.expires_at,
    }


@router.get("/api/config/siwc/login/{attempt_id}")
async def login_poll(attempt_id: str):
    return poll(attempt_id)


@router.post("/api/config/siwc/login/{attempt_id}/complete", dependencies=_GUARDS)
async def login_complete(attempt_id: str, request: CompleteRequest):
    return await asyncio.to_thread(complete_manual, attempt_id, request.callback_url)


@router.post("/api/config/siwc/login/{attempt_id}/cancel", dependencies=_GUARDS)
async def login_cancel(attempt_id: str):
    await asyncio.to_thread(cancel, attempt_id)
    return {"success": True}


@router.post("/api/config/siwc/logout", dependencies=_GUARDS)
async def logout(request: LogoutRequest):
    provider_key = (request.provider or "").strip()
    credential = request.credential or (provider_credential(provider_key) if provider_key else "")
    result = await asyncio.to_thread(siwc_logout, credential)
    if provider_key:
        await asyncio.to_thread(clear_provider_credential, provider_key)
        await asyncio.to_thread(reload_all_runners)
    return {"success": True, **result}


@router.get("/api/config/siwc/status")
async def get_status(provider: str = "", credential: str = ""):
    cred = credential or (provider_credential(provider) if provider else "")
    return await asyncio.to_thread(siwc_status, cred)
