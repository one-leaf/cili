"""工作区 Cron 管理路由（工作区设置 → 定时任务页签使用）。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from core.cron import service
from core.cron.model import CronValidationError
from web.deps import _require_workspace

router = APIRouter()
logger = logging.getLogger(__name__)


def _handle_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if "只读" in message:
        return HTTPException(status_code=403, detail=message)
    if isinstance(exc, (CronValidationError, service.CronError)):
        return HTTPException(status_code=400, detail=message)
    logger.exception("[cron-api] request failed")
    return HTTPException(status_code=500, detail=message)


def _teen_guard() -> None:
    from core.teen_mode import get_teen_mode

    tm = get_teen_mode()
    if tm.is_enabled() and (tm.policy().get("cron") or "deny") == "deny" and not tm.is_unlocked():
        raise HTTPException(
            status_code=403, detail="青少年模式已开启：定时任务的增删改需要家长授权"
        )


@router.get("/api/workspaces/{workspace_uuid}/cron/jobs")
async def list_cron_jobs(
    workspace_uuid: str, ws_dir: Path = Depends(_require_workspace)
):
    return {"jobs": service.list_jobs([workspace_uuid])}


@router.post("/api/workspaces/{workspace_uuid}/cron/jobs")
async def create_cron_job(
    workspace_uuid: str,
    payload: dict[str, Any] = Body(...),
    ws_dir: Path = Depends(_require_workspace),
):
    _teen_guard()
    data = dict(payload)
    data["workspace_uuid"] = workspace_uuid
    try:
        job = service.create_job(data)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"job": job.to_dict()}


@router.put("/api/workspaces/{workspace_uuid}/cron/jobs/{job_id}")
async def update_cron_job(
    workspace_uuid: str,
    job_id: str,
    payload: dict[str, Any] = Body(...),
    ws_dir: Path = Depends(_require_workspace),
):
    _teen_guard()
    try:
        job = service.update_job(workspace_uuid, job_id, payload)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"job": job.to_dict()}


@router.delete("/api/workspaces/{workspace_uuid}/cron/jobs/{job_id}")
async def delete_cron_job(
    workspace_uuid: str, job_id: str, ws_dir: Path = Depends(_require_workspace)
):
    _teen_guard()
    try:
        service.delete_job(workspace_uuid, job_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"ok": True}


@router.post("/api/workspaces/{workspace_uuid}/cron/jobs/{job_id}/run")
async def run_cron_job(
    workspace_uuid: str, job_id: str, ws_dir: Path = Depends(_require_workspace)
):
    _teen_guard()
    try:
        service.run_now(workspace_uuid, job_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"ok": True, "status": "triggered"}


@router.post("/api/workspaces/{workspace_uuid}/cron/jobs/{job_id}/enable")
async def enable_cron_job(
    workspace_uuid: str, job_id: str, ws_dir: Path = Depends(_require_workspace)
):
    _teen_guard()
    try:
        service.set_enabled(workspace_uuid, job_id, True)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"ok": True}


@router.post("/api/workspaces/{workspace_uuid}/cron/jobs/{job_id}/disable")
async def disable_cron_job(
    workspace_uuid: str, job_id: str, ws_dir: Path = Depends(_require_workspace)
):
    _teen_guard()
    try:
        service.set_enabled(workspace_uuid, job_id, False)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"ok": True}


@router.get("/api/workspaces/{workspace_uuid}/cron/jobs/{job_id}/history")
async def cron_job_history(
    workspace_uuid: str,
    job_id: str,
    limit: int = 20,
    ws_dir: Path = Depends(_require_workspace),
):
    try:
        return {"history": service.history(workspace_uuid, job_id, limit)}
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
