"""AgentRun records and lifecycle helpers.

An AgentRun is the durable identity of a worker/lite execution. It can span
multiple epochs when a worker exhausts its iteration budget: the same exec_id
continues with a new epoch and a fresh handoff context. Master session forks
are session-level relations and are stored in SessionStore metadata, not here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.fs_utils import atomic_write_json

logger = logging.getLogger(__name__)

RUN_FILE = "run.json"
EPOCHS_DIR = "epochs"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def root_session_dir(workspace_uuid: str, session: Any) -> Path | None:
    """Resolve the root master session directory for an AgentTool session.

    Master SessionStore: ``session.session_dir`` is already the root.
    Worker/lite _SessionRef: only has ``master_session_id`` and its own exec
    directory; derive the root from workspace data.
    """
    if session is None:
        return None
    if hasattr(session, "agent_logs"):
        return Path(session.session_dir)
    master_sid = (
        getattr(session, "master_session_id", "")
        or getattr(session, "session_id", "")
    )
    if not master_sid:
        return None
    from core.config import get_workspace_data_dir
    return get_workspace_data_dir(workspace_uuid) / "sessions" / master_sid


@dataclass
class AgentEpoch:
    epoch: int
    status: str = "running"
    exit_reason: str = ""
    truncated: bool = False
    iterations: int = 0
    max_iterations: int = 0
    summary: str = ""
    started_at: str = ""
    ended_at: str = ""
    context_ref: dict[str, Any] = field(default_factory=dict)
    messages_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentRunRecord:
    exec_id: str
    root_session_id: str
    role: str
    task: str
    parent_exec_id: str | None = None
    owner_task_id: str | None = None
    relation: str = "delegate"
    source_exec_id: str | None = None
    owns: list[str] = field(default_factory=list)
    context_ref: dict[str, Any] = field(default_factory=dict)
    max_epochs: int = 1
    status: str = "created"
    delivery_state: str = "pending"
    exit_reason: str = ""
    truncated: bool = False
    started_at: str = ""
    ended_at: str = ""
    summary: str = ""
    epochs: list[AgentEpoch] = field(default_factory=list)
    legacy: bool = False
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["epochs"] = [e.to_dict() if isinstance(e, AgentEpoch) else e for e in self.epochs]
        return data


@dataclass
class ContinuationRequest:
    """Worker -> parent request to continue the same AgentRun in a new epoch."""

    exec_id: str
    epoch: int
    exit_reason: str
    truncated: bool
    document: str
    next_steps: str
    summary: str
    iterations: int
    max_iterations: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentRunStore:
    """Persist AgentRun metadata under ``{root_session}/exec_{id}/run.json``."""

    def __init__(self, root_dir: Path | str):
        self.root_dir = Path(root_dir)

    def run_dir(self, exec_id: str) -> Path:
        return self.root_dir / exec_id

    def run_path(self, exec_id: str) -> Path:
        return self.run_dir(exec_id) / RUN_FILE

    def epoch_dir(self, exec_id: str) -> Path:
        return self.run_dir(exec_id) / EPOCHS_DIR

    def epoch_path(self, exec_id: str, epoch: int) -> Path:
        return self.epoch_dir(exec_id) / f"{epoch}.json"

    def create(self, record: AgentRunRecord) -> None:
        self.run_dir(record.exec_id).mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.run_path(record.exec_id), record.to_dict())

    def get(self, exec_id: str) -> AgentRunRecord | None:
        path = self.run_path(exec_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[agent_run] load run.json failed: {exec_id}: {e}")
            return None
        return self._from_dict(data)

    @staticmethod
    def _from_dict(data: dict[str, Any]) -> AgentRunRecord:
        epochs = [AgentEpoch(**e) if isinstance(e, dict) else e for e in data.pop("epochs", [])]
        return AgentRunRecord(**data, epochs=epochs)

    def update(self, exec_id: str, **fields: Any) -> AgentRunRecord | None:
        record = self.get(exec_id)
        if record is None:
            return None
        for key, value in fields.items():
            if hasattr(record, key):
                setattr(record, key, value)
        atomic_write_json(self.run_path(exec_id), record.to_dict())
        return record

    def archive_epoch(
        self,
        exec_id: str,
        *,
        epoch: int,
        status: str,
        exit_reason: str,
        truncated: bool,
        iterations: int,
        max_iterations: int,
        summary: str,
        messages: list[dict],
        context_ref: dict[str, Any] | None = None,
        started_at: str = "",
    ) -> AgentEpoch:
        epoch_dir = self.epoch_dir(exec_id)
        epoch_dir.mkdir(parents=True, exist_ok=True)
        record = AgentEpoch(
            epoch=epoch,
            status=status,
            exit_reason=exit_reason,
            truncated=truncated,
            iterations=iterations,
            max_iterations=max_iterations,
            summary=summary,
            started_at=started_at,
            ended_at=now_str(),
            context_ref=context_ref or {},
            messages_path=f"epochs/{epoch}.json",
        )
        atomic_write_json(
            self.epoch_path(exec_id, epoch),
            {
                "epoch": epoch,
                "status": status,
                "exit_reason": exit_reason,
                "truncated": truncated,
                "iterations": iterations,
                "max_iterations": max_iterations,
                "summary": summary,
                "context_ref": context_ref or {},
                "messages": messages,
                "started_at": started_at,
                "ended_at": record.ended_at,
            },
        )
        run = self.get(exec_id)
        if run is not None:
            run.epochs = [e for e in run.epochs if e.epoch != epoch] + [record]
            run.epochs.sort(key=lambda e: e.epoch)
            run.status = "running" if status == "handoff" else status
            run.exit_reason = exit_reason
            run.truncated = truncated
            run.summary = summary
            if status != "handoff":
                run.ended_at = record.ended_at
            atomic_write_json(self.run_path(exec_id), run.to_dict())
        return epoch

    def list(self) -> list[AgentRunRecord]:
        runs: list[AgentRunRecord] = []
        if not self.root_dir.exists():
            return runs
        for d in sorted(self.root_dir.glob("exec_*")):
            if not d.is_dir():
                continue
            record = self.get(d.name)
            if record is not None:
                runs.append(record)
        return runs

    def derive_legacy(self, exec_id: str) -> AgentRunRecord | None:
        """Best-effort record for old exec logs without run.json."""
        log_path = self.run_dir(exec_id) / "index.json"
        if not log_path.exists():
            return None
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        metadata = data.get("metadata") or {}
        return AgentRunRecord(
            exec_id=exec_id,
            root_session_id=str(metadata.get("session_id") or ""),
            role=str(metadata.get("role") or "worker"),
            task=str(data.get("task") or ""),
            status=str(metadata.get("status") or "completed"),
            exit_reason=str(metadata.get("exit_reason") or metadata.get("status") or ""),
            truncated=bool(metadata.get("truncated", False)),
            iterations=int(metadata.get("iterations") or 0),
            started_at=str(metadata.get("started_at") or ""),
            ended_at=str(metadata.get("ended_at") or ""),
            summary=str(data.get("summary") or ""),
            legacy=True,
        )


class AgentRunManager:
    """Small façade used by AgentTool/SessionRunner.

    The manager owns record creation/epoch archiving/delivery updates; runner
    execution itself remains in SessionRunner so the existing LLM loop is
    unchanged.
    """

    def __init__(self, root_dir: Path | str):
        self.store = AgentRunStore(root_dir)

    def create(
        self,
        *,
        exec_id: str,
        root_session_id: str,
        role: str,
        task: str,
        parent_exec_id: str | None = None,
        owner_task_id: str | None = None,
        relation: str = "delegate",
        source_exec_id: str | None = None,
        max_epochs: int = 1,
        context_ref: dict[str, Any] | None = None,
    ) -> AgentRunRecord:
        record = AgentRunRecord(
            exec_id=exec_id,
            root_session_id=root_session_id,
            role=role,
            task=task,
            parent_exec_id=parent_exec_id,
            owner_task_id=owner_task_id,
            relation=relation,
            source_exec_id=source_exec_id,
            max_epochs=max_epochs,
            status="created",
            started_at=now_str(),
            context_ref=context_ref or {"kind": "task"},
        )
        self.store.create(record)
        return record

    def start(self, exec_id: str, *, epoch: int = 1) -> None:
        self.store.update(exec_id, status="running", started_at=now_str())
        run = self.store.get(exec_id)
        if run is not None:
            run.epochs = [e for e in run.epochs if e.epoch != epoch] + [
                AgentEpoch(epoch=epoch, status="running", started_at=now_str())
            ]
            run.epochs.sort(key=lambda e: e.epoch)
            atomic_write_json(self.store.run_path(exec_id), run.to_dict())

    def archive_epoch(self, exec_id: str, **kwargs: Any) -> AgentEpoch:
        return self.store.archive_epoch(exec_id, **kwargs)

    def complete(
        self,
        exec_id: str,
        *,
        status: str,
        exit_reason: str,
        truncated: bool,
        summary: str,
        delivery_state: str = "pending",
    ) -> None:
        self.store.update(
            exec_id,
            status=status,
            exit_reason=exit_reason,
            truncated=truncated,
            summary=summary,
            delivery_state=delivery_state,
            ended_at=now_str(),
        )

    def delivered(self, exec_id: str) -> None:
        self.store.update(exec_id, delivery_state="delivered")

    def dropped(self, exec_id: str) -> None:
        self.store.update(exec_id, delivery_state="dropped")
