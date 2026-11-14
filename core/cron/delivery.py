"""Cron 投递层：把结果写入目标会话，目标缺失时回落到该任务的 cron 会话。"""

from __future__ import annotations

import logging
from typing import Any

from core.cron import sessions
from core.cron.model import Job, RunResult, State
from core.event_bus import get_event_bus
from core.session import SessionStore

logger = logging.getLogger(__name__)


def _failure_text(job: Job, result: RunResult) -> str:
    reason = result.error or ("执行超时" if result.status == "timeout" else "未知错误")
    return f"定时任务「{job.name}」执行失败：{reason}"


def _target(job: Job) -> tuple[str, str, str] | None:
    mode = str(job.delivery.get("mode") or "none")
    if mode == "origin":
        origin = job.origin if isinstance(job.origin, dict) else {}
        session_id = str(origin.get("session_id") or "")
        workspace_uuid = str(origin.get("workspace_uuid") or job.workspace_uuid)
        return (workspace_uuid, session_id, "origin") if session_id else None
    if mode == "session":
        session_id = str(job.delivery.get("session_id") or "")
        workspace_uuid = str(job.delivery.get("workspace_uuid") or job.workspace_uuid)
        return (workspace_uuid, session_id, "session") if session_id else None
    return None


def deliver(job: Job, state: State, result: RunResult) -> dict[str, Any]:
    """执行一次投递，返回回执字段（写入 state.last_delivery）。"""
    mode = str(job.delivery.get("mode") or "none")

    text = result.final_text.strip()
    if result.status != "ok" and not text:
        text = _failure_text(job, result)
    if not text:
        return {"status": "empty", "mode": mode}

    if mode == "none":
        return {"status": "local", "mode": mode}

    target = _target(job)
    if target is None:
        return _fallback(job, state, mode)

    workspace_uuid, session_id, kind = target
    if workspace_uuid == (job.workspace_uuid or "system") and session_id == state.cron_session_id:
        return {"status": "local", "mode": mode}

    sessions_dir = sessions.workspace_sessions_dir(workspace_uuid)
    target_session = SessionStore.load_session(session_id, sessions_dir)
    if target_session is None:
        logger.warning(
            "[cron-delivery] target session missing: ws=%s sid=%s (job %s)",
            workspace_uuid,
            session_id,
            job.id,
        )
        return _fallback(job, state, mode)

    meta = {
        "cron_delivery": {
            "job_id": job.id,
            "job_name": job.name,
            "slot": result.slot,
            "scheduled_ts": state.last_run,
        }
    }
    try:
        target_session.add_message("assistant", text, meta=meta, flush=True)
        target_session.save()
    except Exception as exc:  # noqa: BLE001 —— 重试一次
        logger.warning("[cron-delivery] write failed, retrying once: %s", exc)
        try:
            target_session.add_message("assistant", text, meta=meta, flush=True)
            target_session.save()
        except Exception as exc2:  # noqa: BLE001
            logger.exception("[cron-delivery] write failed: %s", exc2)
            return {
                "status": "failed",
                "mode": mode,
                "workspace_uuid": workspace_uuid,
                "session_id": session_id,
            }

    bus = get_event_bus()
    bus.publish(
        {
            "type": "cron_delivery",
            "workspace_uuid": workspace_uuid,
            "session_id": session_id,
            "job_id": job.id,
            "job_name": job.name,
            "slot": result.slot,
        }
    )
    bus.publish(
        {
            "type": "turn_complete",
            "workspace_uuid": workspace_uuid,
            "session_id": session_id,
        }
    )
    return {
        "status": "delivered",
        "mode": kind,
        "workspace_uuid": workspace_uuid,
        "session_id": session_id,
    }


def _fallback(job: Job, state: State, mode: str) -> dict[str, Any]:
    """目标缺失：确保该任务自己的 cron 会话存在（结果已在其中）。"""
    try:
        session = sessions.ensure_cron_session(
            job.workspace_uuid or "system", job, state
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[cron-delivery] failed to ensure cron session: %s", exc)
        return {"status": "failed", "mode": mode}
    return {
        "status": "fallback",
        "mode": mode,
        "workspace_uuid": job.workspace_uuid or "system",
        "session_id": session.session_id,
    }
