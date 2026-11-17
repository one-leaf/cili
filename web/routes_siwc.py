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
    logout as siwc_logout,
    poll,
    set_authorized_hook,
    start_login,
    status as siwc_status,
)
from web.deps import _csrf_protect, _sessions_lock, registry, teen_guard

router = APIRouter()
logger = logging.getLogger(__name__)

_GUARDS = [Depends(teen_guard("config_update")), Depends(_csrf_protect)]


class LoginRequest(BaseModel):
    provider: str = ""
    credential: str | None = None


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


def _write_credential(provider_key: str, credential: str) -> bool:
    """登录成功后把 credential 写回 provider（配置保存仍是显式动作，但凭证由服务端创建）。"""
    raw = migrate_legacy_config(load_global_config())
    entry = (raw.get("providers") or {}).get(provider_key)
    if not isinstance(entry, dict) or entry.get("auth_type") != "oauth":
        return False
    if str(entry.get("credential") or "") == credential:
        return False
    entry["credential"] = credential
    return save_global_config(raw)


def _clear_credential(provider_key: str) -> None:
    raw = migrate_legacy_config(load_global_config())
    entry = (raw.get("providers") or {}).get(provider_key)
    if not isinstance(entry, dict):
        return
    entry["credential"] = ""
    save_global_config(raw)


_reload_lock = threading.Lock()


def _reload_runners_sync() -> None:
    with _reload_lock:
        with _sessions_lock:
            cached = list(registry.items())
        for _key, runner in cached:
            try:
                runner.reload_config()
            except Exception as e:  # noqa: BLE001 —— 单个 runner 失败不影响其余
                logger.warning(f"[SIWC] 重载 runner 失败: {e}")


async def _reload_runners() -> None:
    await asyncio.to_thread(_reload_runners_sync)


def _on_authorized(provider_key: str, credential: str) -> None:
    """换码成功后立即写回 provider 并后台重载 runner（不依赖前端轮询）。"""
    if not _write_credential(provider_key, credential):
        return
    threading.Thread(
        target=_reload_runners_sync, daemon=True, name="cili-siwc-reload"
    ).start()


set_authorized_hook(_on_authorized)


@router.post("/api/config/siwc/login", dependencies=_GUARDS)
async def login_start(request: LoginRequest):
    provider_key = (request.provider or "").strip()
    if not provider_key:
        return {"success": False, "error": "缺少 provider"}
    credential = request.credential or _provider_credential(provider_key)
    attempt = await asyncio.to_thread(start_login, provider_key, credential)
    return {
        "success": True,
        "attempt_id": attempt.attempt_id,
        "auth_url": attempt.auth_url,
        "redirect_uri": attempt.redirect_uri,
        "expires_at": attempt.expires_at,
    }


@router.get("/api/config/siwc/login/{attempt_id}")
async def login_poll(attempt_id: str):
    result = poll(attempt_id)
    if result.get("status") == "authorized":
        credential = str(result.get("credential") or "")
        provider_key = str(result.get("provider") or "")
        if credential and provider_key:
            if await asyncio.to_thread(_write_credential, provider_key, credential):
                await _reload_runners()
    return result


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
        await _reload_runners()
    return {"success": True, **result}


@router.get("/api/config/siwc/status")
async def get_status(provider: str = "", credential: str = ""):
    cred = credential or (_provider_credential(provider) if provider else "")
    return await asyncio.to_thread(siwc_status, cred)
