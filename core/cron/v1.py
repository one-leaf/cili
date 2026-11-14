"""v1 cron 数据解析（仅供 migration.py 使用，运行时不再读取）。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.config import get_workspace_data_dir
from core.cron import rules
from core.cron.model import Job, State, now_str
from core.fs_utils import load_json_or_backup

logger = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    pass


def legacy_dir(workspace_uuid: str = "") -> Path:
    return get_workspace_data_dir(workspace_uuid) / "cron.d"


def load_v1_tasks(workspace_uuid: str = "") -> list[dict[str, Any]] | None:
    path = legacy_dir(workspace_uuid) / "user_tasks.json"
    if not path.exists():
        return None
    data = load_json_or_backup(path, None)
    if not isinstance(data, list):
        raise MigrationError(f"invalid v1 tasks file: {path}")
    return [item for item in data if isinstance(item, dict)]


def convert_job(config: dict[str, Any], default_workspace: str = "") -> Job:
    name = str(config.get("name") or config.get("task_id") or "").strip()
    if not name:
        raise MigrationError("task has no name")
    job_id = str(config.get("task_id") or name).strip()
    schedule = dict(config.get("schedule") or {})
    if schedule.get("type") == "cron":
        schedule.setdefault("timezone", None)
    content = config.get("content")
    if content in (None, "", {}, []):
        content = {
            "task": config.get("task", ""),
            "plan": config.get("plan") or [],
        }
    runtime = config.get("config") or {}
    max_executions = runtime.get("max_executions", 9999)
    job = Job(
        id=job_id,
        name=name,
        description=str(config.get("description") or ""),
        enabled=bool(config.get("enabled", True)),
        scope="workspace",
        workspace_uuid=str(config.get("workspace_uuid") or default_workspace or ""),
        schedule=schedule,
        misfire={"policy": "once", "grace_minutes": 120},
        content=content,
        execution={
            "timeout_seconds": 900,
            "max_executions": max_executions,
            "max_runs_kept": 20,
            "context": "fresh",
        },
        delivery={"mode": "none"},
        origin=None,
        one_time=bool(config.get("one_time", False)),
        managed=False,
        created_at=now_str(),
        updated_at=now_str(),
    )
    job.validate()
    rules.validate_schedule(job)
    return job


def convert_state(workspace_uuid: str, job: Job) -> State:
    path = legacy_dir(workspace_uuid) / "state" / f"{job.id}.json"
    if not path.exists():
        path = legacy_dir(workspace_uuid) / "state" / f"{job.name}.json"
    data = load_json_or_backup(path, {}) if path.exists() else {}
    if not isinstance(data, dict):
        data = {}
    max_executions = int(job.execution.get("max_executions", 9999))
    remaining = data.get("remaining")
    try:
        remaining = int(remaining) if remaining is not None else max_executions
    except (TypeError, ValueError):
        remaining = max_executions
    return State(
        next_run=None,
        last_run=data.get("last_run"),
        last_status=data.get("last_status"),
        last_error=data.get("last_error"),
        last_duration_ms=data.get("last_duration_ms"),
        run_count=int(data.get("run_count", 0) or 0),
        remaining=remaining,
        last_slot=data.get("last_slot"),
        missed_count=int(data.get("missed_count", 0) or 0),
        cron_session_id=str(data.get("cron_session_id") or data.get("session_id") or ""),
        history=[],
    )


def load_legacy_state_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = load_json_or_backup(path, None)
    return data if isinstance(data, dict) else None
