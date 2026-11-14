"""Cron v2 领域模型：Job / State / RunResult。

设计见 workspace/default/cron-redesign-plan-2026-11-14.md。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SCHEDULE_TYPES = {"at", "interval", "cron"}
MISFIRE_POLICIES = {"once", "skip", "none"}
DELIVERY_MODES = {"none", "origin", "session"}
SCOPES = {"workspace", "global"}


class CronValidationError(ValueError):
    """任务定义非法。"""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _int_field(value: Any, default: int, name: str, minimum: int | None = None) -> int:
    if value is None:
        return default
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise CronValidationError(f"{name} must be an integer") from exc
    if minimum is not None and result < minimum:
        raise CronValidationError(f"{name} must be >= {minimum}")
    return result


@dataclass
class Job:
    id: str
    name: str
    description: str = ""
    enabled: bool = True
    scope: str = "workspace"
    workspace_uuid: str = ""
    schedule: dict[str, Any] = field(default_factory=dict)
    misfire: dict[str, Any] = field(
        default_factory=lambda: {"policy": "once", "grace_minutes": 120}
    )
    content: Any = field(default_factory=dict)
    execution: dict[str, Any] = field(
        default_factory=lambda: {
            "timeout_seconds": 900,
            "max_executions": 9999,
            "max_runs_kept": 20,
            "context": "fresh",
        }
    )
    delivery: dict[str, Any] = field(default_factory=lambda: {"mode": "none"})
    origin: dict[str, Any] | None = None
    one_time: bool = False
    managed: bool = False
    seed_revision: int = 0
    seed_checksum: str = ""
    created_at: str = ""
    updated_at: str = ""
    version: int = 2

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, workspace_uuid: str = "") -> "Job":
        if not isinstance(data, dict):
            raise CronValidationError("job must be an object")
        merged_schedule = dict(data.get("schedule") or {})
        misfire = {"policy": "once", "grace_minutes": 120}
        misfire.update(data.get("misfire") or {})
        execution = {
            "timeout_seconds": 900,
            "max_executions": 9999,
            "max_runs_kept": 20,
            "context": "fresh",
        }
        execution.update(data.get("execution") or {})
        delivery = {"mode": "none"}
        delivery.update(data.get("delivery") or {})
        job = cls(
            id=str(data.get("id") or data.get("name") or "").strip(),
            name=str(data.get("name") or data.get("id") or "").strip(),
            description=str(data.get("description") or ""),
            enabled=bool(data.get("enabled", True)),
            scope=str(data.get("scope") or "workspace"),
            workspace_uuid=str(data.get("workspace_uuid") or workspace_uuid or ""),
            schedule=merged_schedule,
            misfire=misfire,
            content=copy.deepcopy(data.get("content")),
            execution=execution,
            delivery=delivery,
            origin=copy.deepcopy(data.get("origin")),
            one_time=bool(data.get("one_time", False)),
            managed=bool(data.get("managed", False)),
            seed_revision=_int_field(data.get("seed_revision"), 0, "seed_revision", 0),
            seed_checksum=str(data.get("seed_checksum") or ""),
            created_at=str(data.get("created_at") or now_str()),
            updated_at=str(data.get("updated_at") or data.get("created_at") or now_str()),
            version=_int_field(data.get("version"), 2, "version", 1),
        )
        job.validate()
        return job

    def validate(self) -> None:
        if not JOB_ID_RE.fullmatch(self.id):
            raise CronValidationError(
                "id may only contain letters, digits, hyphens, and underscores (max 64 chars)"
            )
        if not self.name:
            raise CronValidationError("name is required")
        if self.scope not in SCOPES:
            raise CronValidationError(f"unknown scope: {self.scope!r}")
        schedule_type = str(self.schedule.get("type") or "")
        if schedule_type not in SCHEDULE_TYPES:
            raise CronValidationError("schedule.type must be 'at', 'interval' or 'cron'")
        if schedule_type == "interval":
            _int_field(self.schedule.get("minutes"), 0, "schedule.minutes", 1)
            if self.schedule.get("initial_delay_minutes") is not None:
                _int_field(
                    self.schedule.get("initial_delay_minutes"),
                    0,
                    "schedule.initial_delay_minutes",
                    1,
                )
        elif schedule_type == "cron":
            expr = str(self.schedule.get("expr") or "").strip()
            if len(expr.split()) != 5:
                raise CronValidationError(
                    "cron expression must have 5 fields (minute hour day month weekday)"
                )
        else:  # at
            if not self.schedule.get("time"):
                raise CronValidationError("schedule.time is required for type='at'")
        if self.misfire.get("policy") not in MISFIRE_POLICIES:
            raise CronValidationError(
                f"misfire.policy must be one of {sorted(MISFIRE_POLICIES)}"
            )
        _int_field(self.misfire.get("grace_minutes"), 120, "misfire.grace_minutes", 0)
        _int_field(
            self.execution.get("timeout_seconds"),
            900,
            "execution.timeout_seconds",
            0,
        )
        _int_field(
            self.execution.get("max_executions"),
            9999,
            "execution.max_executions",
            1,
        )
        _int_field(
            self.execution.get("max_runs_kept"),
            20,
            "execution.max_runs_kept",
            1,
        )
        if self.delivery.get("mode", "none") not in DELIVERY_MODES:
            raise CronValidationError(
                f"delivery.mode must be one of {sorted(DELIVERY_MODES)}"
            )
        if self.content in (None, "", {}, []):
            raise CronValidationError("content is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 2,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "scope": self.scope,
            "workspace_uuid": self.workspace_uuid,
            "schedule": copy.deepcopy(self.schedule),
            "misfire": copy.deepcopy(self.misfire),
            "content": copy.deepcopy(self.content),
            "execution": copy.deepcopy(self.execution),
            "delivery": copy.deepcopy(self.delivery),
            "origin": copy.deepcopy(self.origin),
            "one_time": self.one_time,
            "managed": self.managed,
            "seed_revision": self.seed_revision,
            "seed_checksum": self.seed_checksum,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def managed_payload(self) -> dict[str, Any]:
        """种子受管字段（用于 checksum 与升级同步）。"""
        return {
            "name": self.name,
            "description": self.description,
            "schedule": copy.deepcopy(self.schedule),
            "content": copy.deepcopy(self.content),
            "misfire": copy.deepcopy(self.misfire),
            "one_time": self.one_time,
            "scope": self.scope,
            "enabled": self.enabled,
            "execution": copy.deepcopy(self.execution),
        }


@dataclass
class State:
    version: int = 2
    next_run: str | None = None
    last_run: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_duration_ms: int | None = None
    run_count: int = 0
    remaining: int | None = None
    last_slot: str | None = None
    running: dict[str, Any] | None = None
    missed_count: int = 0
    cron_session_id: str = ""
    last_delivery: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "State":
        data = data or {}
        if not isinstance(data, dict):
            raise CronValidationError("state must be an object")
        history = data.get("history") or []
        if not isinstance(history, list):
            history = []
        return cls(
            version=_int_field(data.get("version"), 2, "state.version", 1),
            next_run=data.get("next_run"),
            last_run=data.get("last_run"),
            last_status=data.get("last_status"),
            last_error=data.get("last_error"),
            last_duration_ms=data.get("last_duration_ms"),
            run_count=_int_field(data.get("run_count"), 0, "state.run_count", 0),
            remaining=data.get("remaining"),
            last_slot=data.get("last_slot"),
            running=data.get("running"),
            missed_count=_int_field(data.get("missed_count"), 0, "state.missed_count", 0),
            cron_session_id=str(data.get("cron_session_id") or ""),
            last_delivery=data.get("last_delivery"),
            history=[dict(item) for item in history if isinstance(item, dict)],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 2,
            "next_run": self.next_run,
            "last_run": self.last_run,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_duration_ms": self.last_duration_ms,
            "run_count": self.run_count,
            "remaining": self.remaining,
            "last_slot": self.last_slot,
            "running": copy.deepcopy(self.running),
            "missed_count": self.missed_count,
            "cron_session_id": self.cron_session_id,
            "last_delivery": copy.deepcopy(self.last_delivery),
            "history": copy.deepcopy(self.history),
        }

    def push_history(self, record: dict[str, Any], max_kept: int) -> None:
        self.history.insert(0, record)
        if max_kept > 0:
            self.history = self.history[:max_kept]


@dataclass(frozen=True)
class RunResult:
    job_id: str
    slot: str
    status: Literal["ok", "error", "timeout", "skipped", "interrupted"]
    final_text: str = ""
    error: str | None = None
    duration_ms: int = 0
    session_id: str = ""
    delivery: dict[str, Any] | None = None
