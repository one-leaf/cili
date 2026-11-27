"""组织托管路由：状态、发现、短信登录、同步与退出。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.org.service import get_org_service
from web.deps import csrf_protect

router = APIRouter()
logger = logging.getLogger(__name__)


class DiscoverRequest(BaseModel):
    server_url: str = Field(min_length=1, max_length=500)


class SmsSendRequest(BaseModel):
    server_url: str = Field(min_length=1, max_length=500)
    org_id: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=1, max_length=32)


class OrgLoginRequest(BaseModel):
    server_url: str = Field(min_length=1, max_length=500)
    org_id: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=1, max_length=32)
    code: str = Field(min_length=1, max_length=16)
    device_name: str = Field(default="cili", max_length=120)
    bind_roles: list[str] | None = None


def _http_error(exc: Exception) -> HTTPException:
    from core.org.client import (
        OrgAuthError,
        OrgError,
        OrgNetworkError,
        OrgProtocolError,
        OrgRateLimited,
    )

    if isinstance(exc, OrgAuthError):
        return HTTPException(status_code=401, detail="login_invalid")
    if isinstance(exc, OrgRateLimited):
        return HTTPException(
            status_code=429,
            detail={"error": "rate_limited", "retry_after": exc.retry_after},
        )
    if isinstance(exc, OrgNetworkError):
        return HTTPException(status_code=502, detail="org_server_unreachable")
    if isinstance(exc, OrgProtocolError):
        return HTTPException(status_code=400, detail=exc.code or "org_protocol_error")
    if isinstance(exc, OrgError):
        return HTTPException(status_code=500, detail=str(exc) or "org_error")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("[org] request failed")
    return HTTPException(status_code=500, detail="org_error")


@router.get("/api/org/status")
async def org_status():
    return await asyncio.to_thread(get_org_service().status)


@router.post("/api/org/discover", dependencies=[Depends(csrf_protect)])
async def org_discover(payload: DiscoverRequest):
    try:
        orgs = await asyncio.to_thread(
            get_org_service().discover, payload.server_url
        )
        return {"orgs": orgs}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.post("/api/org/sms/send", dependencies=[Depends(csrf_protect)])
async def org_send_sms(payload: SmsSendRequest):
    try:
        data = await asyncio.to_thread(
            get_org_service().send_sms,
            payload.server_url,
            payload.org_id,
            payload.phone,
        )
        return {"ok": True, "cooldown_seconds": int(data.get("cooldown_seconds") or 60)}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.post("/api/org/login", dependencies=[Depends(csrf_protect)])
async def org_login(payload: OrgLoginRequest):
    try:
        status = await asyncio.to_thread(
            get_org_service().login,
            server_url=payload.server_url,
            org_id=payload.org_id,
            phone=payload.phone,
            code=payload.code,
            device_name=payload.device_name,
            bind_roles=payload.bind_roles,
        )
        return status
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.post("/api/org/sync", dependencies=[Depends(csrf_protect)])
async def org_sync():
    return await asyncio.to_thread(get_org_service().sync, force=True, wait=True)


@router.post("/api/org/logout", dependencies=[Depends(csrf_protect)])
async def org_logout():
    return await asyncio.to_thread(get_org_service().logout)
