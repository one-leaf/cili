"""SIWC（ChatGPT 套餐额度）登录与凭证管理路由。"""

from __future__ import annotations

import asyncio
import logging
import threading

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.config import load_global_config, migrate_legacy_config, save_global_config
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
from web.deps import _csrf_protect, reload_all_runners, teen_guard

router = APIRouter()
logger = logging.getLogger(__name__)

_GUARDS = [Depends(teen_guard("config_update")), Depends(_csrf_protect)]


class LoginRequest(BaseModel):
    provider: str = ""
    credential: str | None = None
    provider_config: dict | None = None  # 当前卡片的表单快照（登录成功即保存用）


class CompleteRequest(BaseModel):
    callback_url: str


class LogoutRequest(BaseModel):
    provider: str = ""
    credential: str | None = None


def _provider_entry(provider_key: str) -> dict:
    raw = migrate_legacy_config(load_global_config())
    entry = (raw.get("providers") or {}).get(provider_key)
    return entry if isinstance(entry, dict) else {}


def _provider_credential(provider_key: str) -> str:
    return str(_provider_entry(provider_key).get("credential") or "")


def _upsert_oauth_provider(
    provider_key: str, credential: str, provider_config: dict | None = None
) -> str:
    """登录成功后直接创建/更新 provider（含 credential），返回写回状态。

    前端把当前卡片配置随登录请求带上；provider 尚未保存时也能一次落盘，
    避免“登录成功后卡片消失、还要再点一次保存”。
    """
    raw = migrate_legacy_config(load_global_config())
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        raw["providers"] = providers

    entry = providers.get(provider_key)
    if not isinstance(entry, dict):
        entry = {}
        providers[provider_key] = entry

    if isinstance(provider_config, dict):
        backend = provider_config.get("backend")
        if isinstance(backend, str) and backend:
            entry["backend"] = backend
        headers = provider_config.get("headers")
        if isinstance(headers, dict):
            entry["headers"] = {str(k): str(v) for k, v in headers.items()}
        models = provider_config.get("models")
        if isinstance(models, dict) and models:
            entry["models"] = models
    if not entry.get("models"):
        entry["models"] = {"default": {}}

    entry["auth_type"] = "oauth"
    entry["interface_type"] = "openai-responses"
    entry["base_url"] = "https://api.openai.com/v1"
    entry["api_path"] = ""
    entry["credential"] = credential
    entry.pop("api_key", None)

    return "written" if save_global_config(raw) else "failed"


def _clear_credential(provider_key: str) -> None:
    raw = migrate_legacy_config(load_global_config())
    entry = (raw.get("providers") or {}).get(provider_key)
    if not isinstance(entry, dict):
        return
    entry["credential"] = ""
    save_global_config(raw)


def _on_authorized(
    provider_key: str, credential: str, provider_config: dict | None = None
) -> str:
    """换码成功后直接保存 provider，并后台重载 runner（不依赖前端轮询）。"""
    status = _upsert_oauth_provider(provider_key, credential, provider_config)
    if status == "written":
        threading.Thread(
            target=reload_all_runners, daemon=True, name="cili-siwc-reload"
        ).start()
    return status


set_authorized_hook(_on_authorized)


@router.post("/api/config/siwc/login", dependencies=_GUARDS)
async def login_start(request: LoginRequest):
    provider_key = (request.provider or "").strip()
    if not provider_key:
        return {"success": False, "error": "缺少 provider"}
    credential = request.credential or _provider_credential(provider_key)
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
    credential = request.credential or (_provider_credential(provider_key) if provider_key else "")
    result = await asyncio.to_thread(siwc_logout, credential)
    if provider_key:
        await asyncio.to_thread(_clear_credential, provider_key)
        await asyncio.to_thread(reload_all_runners)
    return {"success": True, **result}


@router.get("/api/config/siwc/status")
async def get_status(provider: str = "", credential: str = ""):
    cred = credential or (_provider_credential(provider) if provider else "")
    return await asyncio.to_thread(siwc_status, cred)
