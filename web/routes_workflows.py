"""工作区工作流管理路由（侧栏工作流分区 / 工作流面板使用）。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from core.workflows import service, store
from core.workflows.model import WorkflowValidationError
from core.workflows.sessions import (
    ensure_new_workflow_panel_session,
    ensure_panel_session,
    ensure_results_session,
    find_workflow_by_panel_session,
    workspace_sessions_dir,
    workspace_cwd,
)
from core.outbox import OutboxFile, build_web_file_card
from core.event_bus import get_event_bus
from core.session import SessionStore
from web.deps import _require_workspace, _validate_session_id

router = APIRouter()
logger = logging.getLogger(__name__)


def _handle_error(exc: Exception) -> HTTPException:
    if isinstance(exc, service.WorkflowNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, service.WorkflowBusyError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, WorkflowValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("[workflows-api] request failed")
    return HTTPException(status_code=500, detail=str(exc))


def _teen_guard() -> None:
    from core.teen_mode import get_teen_mode

    teen = get_teen_mode()
    if (teen.is_enabled()
            and (teen.policy().get("workflows") or "deny") == "deny"
            and not teen.is_unlocked()):
        raise HTTPException(status_code=403,
                            detail="青少年模式已开启：工作流修改与运行需要家长授权")


@router.get("/api/workspaces/{workspace_uuid}/workflows")
async def list_workflows(workspace_uuid: str, ws_dir: Path = Depends(_require_workspace)):
    return {"workflows": service.list_workflows(workspace_uuid)}


@router.post("/api/workspaces/{workspace_uuid}/workflows")
async def create_workflow(workspace_uuid: str,
                      payload: dict[str, Any] = Body(...),
                      ws_dir: Path = Depends(_require_workspace)):
    _teen_guard()
    try:
        workflow = service.create_workflow(workspace_uuid, payload)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"workflow": workflow.to_dict()}


@router.get("/api/workspaces/{workspace_uuid}/workflows/{workflow_id}")
async def get_workflow(workspace_uuid: str, workflow_id: str,
                   ws_dir: Path = Depends(_require_workspace)):
    try:
        workflow = service.get_workflow(workspace_uuid, workflow_id)
        state = service.get_state(workspace_uuid, workflow_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"workflow": workflow.to_dict(), "state": state.to_dict()}


@router.put("/api/workspaces/{workspace_uuid}/workflows/{workflow_id}")
async def update_workflow(workspace_uuid: str, workflow_id: str,
                      payload: dict[str, Any] = Body(...),
                      ws_dir: Path = Depends(_require_workspace)):
    _teen_guard()
    try:
        workflow = service.update_workflow(workspace_uuid, workflow_id, payload)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"workflow": workflow.to_dict()}


@router.post("/api/workspaces/{workspace_uuid}/workflows/{workflow_id}/hidden")
async def set_workflow_hidden(workspace_uuid: str, workflow_id: str,
                              payload: dict[str, Any] = Body(default={}),
                              ws_dir: Path = Depends(_require_workspace)):
    _teen_guard()
    try:
        workflow = service.set_hidden(
            workspace_uuid, workflow_id, bool((payload or {}).get("hidden")))
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"ok": True, "hidden": workflow.hidden}


@router.delete("/api/workspaces/{workspace_uuid}/workflows/{workflow_id}")
async def delete_workflow(workspace_uuid: str, workflow_id: str,
                      ws_dir: Path = Depends(_require_workspace)):
    _teen_guard()
    try:
        service.delete_workflow(workspace_uuid, workflow_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"ok": True}


@router.post("/api/workspaces/{workspace_uuid}/workflows/{workflow_id}/run")
async def run_workflow(workspace_uuid: str, workflow_id: str,
                   payload: dict | None = None,
                   ws_dir: Path = Depends(_require_workspace)):
    _teen_guard()
    body = payload if isinstance(payload, dict) else {}
    try:
        state = service.run_workflow(
            workspace_uuid, workflow_id,
            inputs=body.get("inputs") if isinstance(body.get("inputs"), dict) else None,
            origin_session_id=str(body.get("origin_session_id") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"ok": True, "status": state.status, "run_id": state.run_id}


@router.post("/api/workspaces/{workspace_uuid}/workflows/{workflow_id}/stop")
async def stop_workflow(workspace_uuid: str, workflow_id: str,
                    ws_dir: Path = Depends(_require_workspace)):
    _teen_guard()
    try:
        stopped = service.stop_workflow(workspace_uuid, workflow_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"ok": True, "stopped": stopped}


@router.get("/api/workspaces/{workspace_uuid}/workflows/{workflow_id}/state")
async def get_state(workspace_uuid: str, workflow_id: str,
                    ws_dir: Path = Depends(_require_workspace)):
    try:
        state = service.get_state(workspace_uuid, workflow_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"state": state.to_dict()}


@router.post("/api/workspaces/{workspace_uuid}/workflows/panel-sessions")
async def panel_session(workspace_uuid: str,
                        payload: dict[str, Any] = Body(default={}),
                        ws_dir: Path = Depends(_require_workspace)):
    _teen_guard()
    workflow_id = str((payload or {}).get("workflow_id") or "")
    try:
        if workflow_id:
            workflow = service.get_workflow(workspace_uuid, workflow_id)
            state = store.load_state(workspace_uuid, workflow_id)
            session = ensure_panel_session(workspace_uuid, workflow, state)
        else:
            session = ensure_new_workflow_panel_session(workspace_uuid)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"session_id": session.session_id, "workflow_id": workflow_id}


@router.get("/api/workspaces/{workspace_uuid}/workflows/panel-sessions/by-session/{session_id}")
async def panel_session_binding(workspace_uuid: str, session_id: str,
                                ws_dir: Path = Depends(_require_workspace)):
    _validate_session_id(session_id)
    session = SessionStore.load_session(session_id, workspace_sessions_dir(workspace_uuid))
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"workflow_id": find_workflow_by_panel_session(workspace_uuid, session)}


@router.post("/api/workspaces/{workspace_uuid}/workflows/{workflow_id}/results-session")
async def results_session(workspace_uuid: str, workflow_id: str,
                          ws_dir: Path = Depends(_require_workspace)):
    _teen_guard()
    try:
        workflow = service.get_workflow(workspace_uuid, workflow_id)
        state = store.load_state(workspace_uuid, workflow_id)
        session = ensure_results_session(workspace_uuid, workflow, state)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"session_id": session.session_id}


@router.post("/api/workspaces/{workspace_uuid}/workflows/{workflow_id}/send-results")
async def send_results(workspace_uuid: str, workflow_id: str,
                       payload: dict | None = Body(default=None),
                       ws_dir: Path = Depends(_require_workspace)):
    """把一次运行的步骤结果文件直接发到指定会话（Web file card）。"""
    body = payload if isinstance(payload, dict) else {}
    try:
        state = service.get_state(workspace_uuid, workflow_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    session_id = str(body.get("session_id") or state.origin_session_id
                     or state.panel_session_id or state.results_session_id or "")
    if not session_id:
        raise HTTPException(status_code=400, detail="没有可发送到的会话")
    run_id = str(body.get("run_id") or "") or None
    files = service.collect_result_files(workspace_uuid, workflow_id, run_id)
    if not files:
        raise HTTPException(status_code=400, detail="这次运行没有可发送的结果文件")
    sessions_dir = ws_dir / "sessions"
    session = await asyncio.to_thread(SessionStore.load_session, session_id, sessions_dir)
    if session is None:
        raise HTTPException(status_code=404, detail="目标会话不存在")
    fake_runner = SimpleNamespace(
        cwd=str(workspace_cwd(workspace_uuid)),
        workspace_uuid=workspace_uuid,
        current_session_id=session_id,
    )
    bus = get_event_bus()
    for item in files:
        content, event, meta = build_web_file_card(
            fake_runner, OutboxFile(path=item["path"], note=item["note"]))
        session.add_message("assistant", content, meta=meta, flush=True)
        bus.publish({**event, "workspace_uuid": workspace_uuid,
                     "session_id": session_id})
    return {"ok": True, "count": len(files)}


@router.get("/api/workspaces/{workspace_uuid}/workflows/{workflow_id}/runs")
async def list_runs(workspace_uuid: str, workflow_id: str,
                    ws_dir: Path = Depends(_require_workspace)):
    try:
        service.get_workflow(workspace_uuid, workflow_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle_error(exc) from exc
    return {"runs": store.list_runs(workspace_uuid, workflow_id)}


@router.get("/api/workspaces/{workspace_uuid}/workflows/{workflow_id}/runs/{run_id}")
async def get_run(workspace_uuid: str, workflow_id: str, run_id: str,
                  ws_dir: Path = Depends(_require_workspace)):
    data = store.read_run_json(workspace_uuid, workflow_id, run_id)
    if data is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return {"run": data}


@router.get("/api/workspaces/{workspace_uuid}/workflows/{workflow_id}/runs/{run_id}/events")
async def get_run_events(workspace_uuid: str, workflow_id: str, run_id: str,
                         offset: int = 0,
                         ws_dir: Path = Depends(_require_workspace)):
    return {"events": store.read_events(workspace_uuid, workflow_id, run_id, offset=offset)}
