"""工作流文件存储：每个工作流一个目录。

布局：
    {workspace}/.cili/workflows/
      {workflow_id}/workflow.json
      {workflow_id}/state.json
      {workflow_id}/runs/{run_id}/run.json
      {workflow_id}/runs/{run_id}/events.jsonl
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from core.config import get_workspace_data_dir
from core.fs_utils import atomic_write_json, load_json_or_backup
from core.workflows.model import (
    Workflow,
    WorkflowState,
    WorkflowValidationError,
    is_short_id,
    new_short_id,
)

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[0-9a-f]{8}$")
MAX_RUNS_KEPT = 20

_global_lock = threading.RLock()
_workflow_locks: dict[tuple[str, str], threading.RLock] = {}
_run_locks: dict[tuple[str, str, str], threading.RLock] = {}
_locks_guard = threading.Lock()


def store_lock() -> threading.RLock:
    """工作流配置/状态的进程级可重入锁。"""
    return _global_lock


def workflow_lock(workspace_uuid: str, workflow_id: str) -> threading.RLock:
    with _locks_guard:
        key = (workspace_uuid or "", workflow_id)
        lock = _workflow_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _workflow_locks[key] = lock
        return lock


def _run_lock(workspace_uuid: str, workflow_id: str, run_id: str) -> threading.RLock:
    with _locks_guard:
        key = (workspace_uuid or "", workflow_id, run_id)
        lock = _run_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _run_locks[key] = lock
        return lock


_migrated_workspaces: set[str] = set()


def _migrate_legacy(workspace_uuid: str) -> None:
    """把旧版 `.cili/tasks`（task.json / subtasks）迁移到 workflows。

    只执行一次，用 `.legacy-migrated` 标记；旧的状态文件里 panel/results
    会话 id 属于旧 metadata，清空后按需重建（步骤会话保留）。
    """
    key = workspace_uuid or ""
    if key in _migrated_workspaces:
        return
    data_dir = get_workspace_data_dir(workspace_uuid)
    old_root = data_dir / "tasks"
    new_root = data_dir / "workflows"
    if old_root.is_dir() and not new_root.exists():
        try:
            old_root.rename(new_root)
        except OSError:
            try:
                shutil.move(str(old_root), str(new_root))
            except OSError as exc:
                logger.warning("[workflows] 旧目录迁移失败: %s", exc)
    if new_root.is_dir() and not (new_root / ".legacy-migrated").exists():
        for directory in new_root.iterdir():
            if not directory.is_dir() or not _ID_RE.match(directory.name):
                continue
            legacy = directory / "task.json"
            current = directory / "workflow.json"
            if legacy.is_file() and not current.exists():
                data = load_json_or_backup(legacy, {})
                if isinstance(data, dict):
                    if "subtasks" in data and "steps" not in data:
                        data["steps"] = data.pop("subtasks")
                    try:
                        atomic_write_json(current, data)
                        legacy.unlink()
                    except OSError as exc:
                        logger.warning("[workflows] 迁移 %s 失败: %s", legacy, exc)
            state_file = directory / "state.json"
            if state_file.is_file():
                state = load_json_or_backup(state_file, {})
                if isinstance(state, dict) and (
                        state.get("panel_session_id") or state.get("results_session_id")):
                    state["panel_session_id"] = ""
                    state["results_session_id"] = ""
                    try:
                        atomic_write_json(state_file, state)
                    except OSError as exc:
                        logger.warning("[workflows] 迁移 %s 失败: %s", state_file, exc)
        try:
            (new_root / ".legacy-migrated").write_text("1", encoding="utf-8")
        except OSError:
            pass
    _migrated_workspaces.add(key)


def root(workspace_uuid: str = "") -> Path:
    _migrate_legacy(workspace_uuid)
    return get_workspace_data_dir(workspace_uuid) / "workflows"


def workflow_dir(workspace_uuid: str, workflow_id: str) -> Path:
    return root(workspace_uuid) / workflow_id


def workflow_path(workspace_uuid: str, workflow_id: str) -> Path:
    return workflow_dir(workspace_uuid, workflow_id) / "workflow.json"


def state_path(workspace_uuid: str, workflow_id: str) -> Path:
    return workflow_dir(workspace_uuid, workflow_id) / "state.json"


def runs_root(workspace_uuid: str, workflow_id: str) -> Path:
    return workflow_dir(workspace_uuid, workflow_id) / "runs"


def run_dir(workspace_uuid: str, workflow_id: str, run_id: str) -> Path:
    return runs_root(workspace_uuid, workflow_id) / run_id


def run_json_path(workspace_uuid: str, workflow_id: str, run_id: str) -> Path:
    return run_dir(workspace_uuid, workflow_id, run_id) / "run.json"


def events_path(workspace_uuid: str, workflow_id: str, run_id: str) -> Path:
    return run_dir(workspace_uuid, workflow_id, run_id) / "events.jsonl"


# ─── 工作流与状态 ────────────────────────────────────────────


def new_workflow_id(workspace_uuid: str = "") -> str:
    for _ in range(100):
        workflow_id = new_short_id()
        if not workflow_dir(workspace_uuid, workflow_id).exists():
            return workflow_id
    raise RuntimeError("无法生成唯一的工作流 id")


def new_run_id(workspace_uuid: str, workflow_id: str) -> str:
    for _ in range(100):
        run_id = new_short_id()
        if not run_dir(workspace_uuid, workflow_id, run_id).exists():
            return run_id
    raise RuntimeError("无法生成唯一的运行 id")


def load_workflow(workspace_uuid: str, workflow_id: str) -> Workflow | None:
    if not is_short_id(workflow_id):
        return None
    path = workflow_path(workspace_uuid, workflow_id)
    if not path.exists():
        legacy = workflow_dir(workspace_uuid, workflow_id) / "task.json"
        if legacy.is_file():
            path = legacy
    data = load_json_or_backup(path, {})
    if isinstance(data, dict) and "subtasks" in data and "steps" not in data:
        data["steps"] = data.pop("subtasks")
    if not isinstance(data, dict) or not data:
        return None
    try:
        return Workflow.from_dict(data, workflow_id=workflow_id)
    except WorkflowValidationError as exc:
        logger.warning("[workflows] 工作流 %s 定义损坏: %s", workflow_id, exc)
        return None


def save_workflow(workspace_uuid: str, workflow: Workflow) -> None:
    atomic_write_json(workflow_path(workspace_uuid, workflow.id), workflow.to_dict())


def create_workflow(workspace_uuid: str, payload: dict[str, Any]) -> Workflow:
    """从 payload 创建工作流（id 冲突时重新生成）。"""
    workflow = Workflow.from_dict(payload)
    workflow.id = new_workflow_id(workspace_uuid) if workflow_dir(workspace_uuid, workflow.id).exists() else workflow.id
    workflow_dir(workspace_uuid, workflow.id).mkdir(parents=True, exist_ok=True)
    save_workflow(workspace_uuid, workflow)
    save_state(workspace_uuid, workflow.id, WorkflowState())
    return workflow


def update_workflow(workspace_uuid: str, workflow_id: str, payload: dict[str, Any]) -> Workflow:
    existing = load_workflow(workspace_uuid, workflow_id)
    if existing is None:
        raise WorkflowValidationError("工作流不存在")
    workflow = Workflow.from_dict(payload, workflow_id=workflow_id)
    # 面板编辑只提交定义字段；未携带 hidden 时保留侧栏隐藏状态
    if "hidden" not in payload:
        workflow.hidden = existing.hidden
    workflow.created_at = existing.created_at
    workflow.updated_at = existing.updated_at  # from_dict 已设为 now? 这里保持原值再刷新
    from core.time_utils import now_str
    workflow.updated_at = now_str()
    save_workflow(workspace_uuid, workflow)
    return workflow


def set_hidden(workspace_uuid: str, workflow_id: str, hidden: bool) -> Workflow:
    """设置侧栏隐藏状态；不刷新 updated_at，避免隐藏/取消隐藏打乱列表顺序。"""
    workflow = load_workflow(workspace_uuid, workflow_id)
    if workflow is None:
        raise WorkflowValidationError("工作流不存在")
    workflow.hidden = bool(hidden)
    save_workflow(workspace_uuid, workflow)
    return workflow


def load_state(workspace_uuid: str, workflow_id: str) -> WorkflowState:
    data = load_json_or_backup(state_path(workspace_uuid, workflow_id), {})
    return WorkflowState.from_dict(data)


def save_state(workspace_uuid: str, workflow_id: str, state: WorkflowState) -> None:
    atomic_write_json(state_path(workspace_uuid, workflow_id), state.to_dict())


def delete_workflow(workspace_uuid: str, workflow_id: str) -> bool:
    directory = workflow_dir(workspace_uuid, workflow_id)
    if not directory.is_dir():
        return False
    shutil.rmtree(directory)
    return True


def list_workflows(workspace_uuid: str = "") -> list[dict[str, Any]]:
    base = root(workspace_uuid)
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for directory in base.iterdir():
        if not directory.is_dir() or not _ID_RE.match(directory.name):
            continue
        workflow = load_workflow(workspace_uuid, directory.name)
        if workflow is None:
            continue
        state = load_state(workspace_uuid, directory.name)
        out.append({
            "workflow": workflow.to_dict(),
            "state": {
                "status": state.status,
                "run_id": state.run_id,
                "current_index": state.current_index,
                "panel_session_id": state.panel_session_id,
                "results_session_id": state.results_session_id,
                "run_inputs": dict(state.run_inputs),
                "items": {k: v.to_dict() for k, v in state.items.items()},
                "history": state.history[:5],
            },
        })
    out.sort(key=lambda item: item["workflow"].get("updated_at", ""), reverse=True)
    return out


# ─── 运行日志 ──────────────────────────────────────────────


def ensure_run_dir(workspace_uuid: str, workflow_id: str, run_id: str) -> Path:
    directory = run_dir(workspace_uuid, workflow_id, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def append_event(workspace_uuid: str, workflow_id: str, run_id: str,
                 event: dict[str, Any], *, fsync: bool = False) -> None:
    """追加一条运行事件（一行 JSON）。"""
    path = events_path(workspace_uuid, workflow_id, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False)
    with _run_lock(workspace_uuid, workflow_id, run_id):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            if fsync:
                import os
                os.fsync(handle.fileno())


def write_run_json(workspace_uuid: str, workflow_id: str, run_id: str,
                   data: dict[str, Any]) -> None:
    atomic_write_json(run_json_path(workspace_uuid, workflow_id, run_id), data)


def read_run_json(workspace_uuid: str, workflow_id: str, run_id: str) -> dict[str, Any] | None:
    data = load_json_or_backup(run_json_path(workspace_uuid, workflow_id, run_id), {})
    return data if isinstance(data, dict) and data else None


def read_events(workspace_uuid: str, workflow_id: str, run_id: str,
                offset: int = 0) -> list[dict[str, Any]]:
    path = events_path(workspace_uuid, workflow_id, run_id)
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    with _run_lock(workspace_uuid, workflow_id, run_id):
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    for index, line in enumerate(lines):
        if index < max(0, int(offset)):
            continue
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _run_started_at(directory: Path) -> str:
    data = load_json_or_backup(directory / "run.json", {})
    if isinstance(data, dict):
        return str(data.get("started_at") or "")
    return ""


def list_runs(workspace_uuid: str, workflow_id: str) -> list[dict[str, Any]]:
    base = runs_root(workspace_uuid, workflow_id)
    if not base.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for directory in base.iterdir():
        if not directory.is_dir() or not _ID_RE.match(directory.name):
            continue
        data = load_json_or_backup(directory / "run.json", {})
        if not isinstance(data, dict) or not data:
            continue
        runs.append({
            "run_id": directory.name,
            "status": data.get("status", ""),
            "started_at": data.get("started_at", ""),
            "finished_at": data.get("finished_at", ""),
            "reason": data.get("reason", ""),
            "step_count": len(data.get("steps") or []),
        })
    runs.sort(key=lambda item: (item.get("started_at", ""), item.get("run_id", "")),
              reverse=True)
    return runs


def prune_runs(workspace_uuid: str, workflow_id: str, keep: int = MAX_RUNS_KEPT) -> None:
    base = runs_root(workspace_uuid, workflow_id)
    if not base.is_dir():
        return
    dirs = [
        d for d in base.iterdir()
        if d.is_dir() and _ID_RE.match(d.name)
    ]
    if len(dirs) <= keep:
        return
    dirs.sort(key=lambda d: (_run_started_at(d), d.name))
    for directory in dirs[:len(dirs) - keep]:
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            logger.warning("[workflows] 清理旧运行目录失败 %s: %s", directory, exc)
