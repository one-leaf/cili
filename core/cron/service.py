"""Cron 任务管理 API：工具与 REST 共用同一套校验和锁。"""

from __future__ import annotations

import logging
import re
from typing import Any

from core.config import get_all_workspace_uuids
from core.cron import rules, store
from core.cron.model import CronValidationError, Job, State, now_str

logger = logging.getLogger(__name__)


class CronError(RuntimeError):
    """业务错误（可直接展示给用户）。"""


def _engine():
    """延迟导入 engine（engine 依赖 SessionRunner → tools，避免循环导入）。"""
    from core.cron.engine import get_engine

    return get_engine()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-").lower()
    return slug[:64] or "task"


def _default_workspace_uuid() -> str:
    from core.config import load_workspaces_index

    for entry in load_workspaces_index():
        if entry.get("workspace_name") in ("Default", "default") and entry.get("uuid"):
            return str(entry["uuid"])
    return ""


def _unique_id(workspace_uuid: str, base_id: str) -> str:
    candidate = base_id
    index = 2
    while store.job_path(workspace_uuid, candidate).exists():
        candidate = f"{base_id}-{index}"
        index += 1
    return candidate


def _ensure_not_managed(job: Job) -> None:
    if job.managed:
        raise CronError("受管任务只读：只能查看列表与历史")


def _enrich(workspace_uuid: str, job: Job, state: State) -> dict[str, Any]:
    item = job.to_dict()
    item["workspace_uuid"] = workspace_uuid
    item["schedule_summary"] = rules.schedule_summary(job)
    item["state"] = {
        "next_run": state.next_run,
        "last_run": state.last_run,
        "last_status": state.last_status,
        "last_error": state.last_error,
        "last_duration_ms": state.last_duration_ms,
        "run_count": state.run_count,
        "remaining": state.remaining,
        "running": state.running,
        "missed_count": state.missed_count,
        "last_delivery": state.last_delivery,
    }
    return item


def list_jobs(workspaces: list[str] | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for workspace_uuid in workspaces or get_all_workspace_uuids():
        for job in store.list_jobs(workspace_uuid):
            result.append(
                _enrich(workspace_uuid, job, store.load_state(workspace_uuid, job.id))
            )
    return result


def get_job(workspace_uuid: str, job_id: str) -> Job:
    job = store.load_job(workspace_uuid, job_id)
    if job is None:
        raise CronError(f"task '{job_id}' not found in workspace '{workspace_uuid or 'default'}'")
    return job


def create_job(
    data: dict[str, Any],
    *,
    origin: dict[str, Any] | None = None,
) -> Job:
    workspace_uuid = str(
        data.get("workspace_uuid") or (origin or {}).get("workspace_uuid") or ""
    )
    if not workspace_uuid:
        workspace_uuid = _default_workspace_uuid()
    name = str(data.get("name") or "").strip()
    if not name:
        raise CronValidationError("'name' is required")
    job_id = _unique_id(workspace_uuid, _slugify(str(data.get("id") or name)))
    data = dict(data)
    data["id"] = job_id
    data["workspace_uuid"] = workspace_uuid
    data["scope"] = "workspace"
    data["managed"] = False
    if origin and not data.get("delivery"):
        data["delivery"] = {
            "mode": "origin",
            "workspace_uuid": origin.get("workspace_uuid"),
            "session_id": origin.get("session_id"),
            "interface": origin.get("interface"),
        }
    job = Job.from_dict(data, workspace_uuid=workspace_uuid)
    if job.schedule.get("type") == "at":
        job.one_time = True
    if job.delivery.get("mode") == "origin" and origin:
        job.origin = dict(origin)
    rules.validate_schedule(job)
    job.created_at = job.created_at or now_str()
    job.updated_at = now_str()

    with store.config_lock():
        if store.job_path(workspace_uuid, job.id).exists():
            raise CronError(f"task '{job.id}' already exists")
        state = State(remaining=int(job.execution.get("max_executions", 9999)))
        next_dt = rules.compute_next_run(job, None)
        state.next_run = next_dt.isoformat()
        store.save_job(job)
        store.save_state(workspace_uuid, job.id, state)
    _engine().reload()
    return job


def update_job(workspace_uuid: str, job_id: str, updates: dict[str, Any]) -> Job:
    with store.config_lock(), store.job_lock(workspace_uuid, job_id):
        job = get_job(workspace_uuid, job_id)
        _ensure_not_managed(job)
        allowed = {
            "name",
            "description",
            "schedule",
            "misfire",
            "content",
            "execution",
            "delivery",
            "one_time",
        }
        changed: list[str] = []
        schedule_changed = False
        for key, value in updates.items():
            if key not in allowed or value is None:
                continue
            if key in ("schedule", "misfire", "execution", "delivery"):
                current = dict(getattr(job, key) or {})
                current.update(value)
                setattr(job, key, current)
            else:
                setattr(job, key, value)
            changed.append(key)
            if key == "schedule":
                schedule_changed = True
        if not changed:
            raise CronError("no fields to update")
        if job.schedule.get("type") == "at":
            job.one_time = True
        job.updated_at = now_str()
        job.validate()
        rules.validate_schedule(job)
        store.save_job(job)
        if schedule_changed:
            state = store.load_state(workspace_uuid, job_id)
            state.next_run = rules.compute_next_run(job, None).isoformat()
            store.save_state(workspace_uuid, job_id, state)
    _engine().reload()
    return job


def delete_job(workspace_uuid: str, job_id: str) -> None:
    with store.config_lock(), store.job_lock(workspace_uuid, job_id):
        job = get_job(workspace_uuid, job_id)
        _ensure_not_managed(job)
        store.delete_job(workspace_uuid, job_id)
    _engine().reload()


def set_enabled(workspace_uuid: str, job_id: str, enabled: bool) -> Job:
    with store.config_lock(), store.job_lock(workspace_uuid, job_id):
        job = get_job(workspace_uuid, job_id)
        _ensure_not_managed(job)
        job.enabled = bool(enabled)
        job.updated_at = now_str()
        store.save_job(job)
        state = store.load_state(workspace_uuid, job_id)
        if enabled:
            state.remaining = int(job.execution.get("max_executions", 9999))
            state.next_run = rules.compute_next_run(job, None).isoformat()
        store.save_state(workspace_uuid, job_id, state)
    _engine().reload()
    return job


def run_now(workspace_uuid: str, job_id: str) -> None:
    job = get_job(workspace_uuid, job_id)
    _ensure_not_managed(job)
    if not _engine().run_now(workspace_uuid, job_id):
        raise CronError("任务正在运行或调度器未就绪，无法立即执行")


def history(workspace_uuid: str, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
    get_job(workspace_uuid, job_id)
    return _engine().history(workspace_uuid, job_id, limit)
