"""工作流服务：CRUD、运行/停止、状态读取。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from core.config import get_workspace_cwd
from core.event_bus import get_event_bus
from core.workflows import runner as runner_mod
from core.workflows import store
from core.workflows.model import (
    Workflow,
    WorkflowState,
    WorkflowValidationError,
    resolve_step_index,
    validate_run_inputs,
)

logger = logging.getLogger(__name__)


class WorkflowBusyError(RuntimeError):
    """工作流正在运行或调度未就绪。"""


class WorkflowNotFoundError(LookupError):
    """工作流不存在。"""


def _publish_changed(workspace_uuid: str, workflow_id: str, action: str) -> None:
    """广播工作流变更。只在写盘成功后调用，前端据此刷新而不是推断 LLM 是否成功。"""
    get_event_bus().publish({
        "type": "workflow_changed",
        "workspace_uuid": workspace_uuid,
        "workflow_id": workflow_id,
        "action": action,
    })


def list_workflows(workspace_uuid: str = "") -> list[dict[str, Any]]:
    items = store.list_workflows(workspace_uuid)
    for item in items:
        workflow_id = item["workflow"]["id"]
        if item["state"].get("status") == "running" and not runner_mod.is_workflow_running(
                workspace_uuid, workflow_id):
            _mark_interrupted(workspace_uuid, workflow_id,
                              store.load_state(workspace_uuid, workflow_id))
            state = store.load_state(workspace_uuid, workflow_id)
            item["state"].update({
                "status": state.status,
                "current_index": state.current_index,
                "items": {k: v.to_dict() for k, v in state.items.items()},
            })
    return items


def get_workflow(workspace_uuid: str, workflow_id: str) -> Workflow:
    workflow = store.load_workflow(workspace_uuid, workflow_id)
    if workflow is None:
        raise WorkflowNotFoundError("工作流不存在")
    return workflow


def get_state(workspace_uuid: str, workflow_id: str) -> WorkflowState:
    get_workflow(workspace_uuid, workflow_id)
    state = store.load_state(workspace_uuid, workflow_id)
    if state.status == "running" and not runner_mod.is_workflow_running(workspace_uuid, workflow_id):
        _mark_interrupted(workspace_uuid, workflow_id, state)
    return state


def collect_result_files(workspace_uuid: str, workflow_id: str,
                         run_id: str | None = None) -> list[dict[str, str]]:
    """收集一次运行的步骤结果文件：默认 result.md + 声明的 outputs。"""
    workflow = get_workflow(workspace_uuid, workflow_id)
    state = store.load_state(workspace_uuid, workflow_id)
    run_id = run_id or state.run_id
    if not run_id:
        return []
    data = store.read_run_json(workspace_uuid, workflow_id, run_id) or {}
    items = data.get("items")
    if not isinstance(items, dict):
        items = {sid: st.to_dict() for sid, st in state.items.items()}
    cwd = Path(get_workspace_cwd(workspace_uuid))
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, step in enumerate(workflow.steps, 1):
        item = items.get(step.id) if isinstance(items.get(step.id), dict) else {}
        candidates: list[str] = []
        artifact = str(item.get("artifact") or "")
        if artifact:
            candidates.append(artifact)
        candidates.extend(step.outputs)
        for raw in candidates:
            path = Path(raw)
            if not path.is_absolute():
                path = cwd / path
            try:
                resolved = path.resolve()
                inside = os.path.commonpath([str(resolved), str(cwd.resolve())]) == str(cwd.resolve())
            except (OSError, ValueError):
                continue
            if not inside or not resolved.is_file():
                continue
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            out.append({"path": str(resolved), "note": f"第 {index} 步：{step.name}"})
    return out


def _mark_interrupted(workspace_uuid: str, workflow_id: str, state: WorkflowState) -> None:
    """运行中但进程内没有 runner → 标记中断并补运行日志。"""
    from core.time_utils import now_str
    state.status = "interrupted"
    state.finished_at = now_str()
    for step in state.items.values():
        if step.status == "running":
            step.status = "interrupted"
            step.phase = ""
            step.current_tool = ""
    store.save_state(workspace_uuid, workflow_id, state)
    if state.run_id:
        try:
            store.append_event(workspace_uuid, workflow_id, state.run_id,
                               {"ts": state.finished_at, "type": "interrupted",
                                "reason": "host restarted"}, fsync=True)
            run = store.read_run_json(workspace_uuid, workflow_id, state.run_id) or {}
            run.update({"status": state.status, "finished_at": state.finished_at,
                        "reason": "host restarted",
                        "steps": {k: v.to_dict() for k, v in state.items.items()}})
            store.write_run_json(workspace_uuid, workflow_id, state.run_id, run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] 标记中断失败: %s", exc)
    _publish_changed(workspace_uuid, workflow_id, "interrupted")

    # 进程重启导致的中断也要进收件箱（与 runner 正常终态通知同一模型）。
    try:
        from core.config import get_workspace_meta
        from core.inbox import MAX_SYSTEM_BODY_CHARS, get_inbox_store

        workflow = store.load_workflow(workspace_uuid, workflow_id)
        name = workflow.name if workflow else workflow_id
        workspace_name = str(
            get_workspace_meta(workspace_uuid).get("workspace_name") or ""
        )
        run_id = state.run_id or state.finished_at or "unknown"
        get_inbox_store().add(
            f"工作流「{name}」已中断",
            "进程重启，运行被中断。",
            level="warning",
            source_type="workflow",
            workspace_uuid=workspace_uuid,
            workspace_name=workspace_name,
            session_id=str(state.origin_session_id or state.results_session_id or ""),
            dedupe_key=f"workflow:{workspace_uuid}:{workflow_id}:{run_id}",
            payload={
                "workflow_id": workflow_id,
                "run_id": run_id,
                "status": "interrupted",
                "results_dir": "",
            },
            max_body_chars=MAX_SYSTEM_BODY_CHARS,
        )
    except Exception:  # noqa: BLE001 —— 通知失败不影响中断标记
        logger.warning("[workflow] 写中断收件箱失败: %s", workflow_id, exc_info=True)


def create_workflow(workspace_uuid: str, payload: dict[str, Any]) -> Workflow:
    with store.store_lock():
        workflow = store.create_workflow(workspace_uuid, payload)
    _publish_changed(workspace_uuid, workflow.id, "created")
    return workflow


def update_workflow(workspace_uuid: str, workflow_id: str, payload: dict[str, Any]) -> Workflow:
    if runner_mod.is_workflow_running(workspace_uuid, workflow_id):
        raise WorkflowBusyError("工作流正在运行，无法修改")
    with store.store_lock():
        workflow = store.update_workflow(workspace_uuid, workflow_id, payload)
        state = store.load_state(workspace_uuid, workflow_id)
        state.status = "idle"
        state.run_id = ""
        state.started_at = ""
        state.finished_at = ""
        state.current_index = 0
        state.run_inputs = {}
        state.items = {}
        store.save_state(workspace_uuid, workflow_id, state)
    _publish_changed(workspace_uuid, workflow_id, "updated")
    return workflow


def set_hidden(workspace_uuid: str, workflow_id: str, hidden: bool) -> Workflow:
    """设置侧栏隐藏状态（允许运行中操作，不影响执行）。"""
    with store.store_lock():
        workflow = store.set_hidden(workspace_uuid, workflow_id, hidden)
    _publish_changed(workspace_uuid, workflow_id, "updated")
    return workflow


def delete_workflow(workspace_uuid: str, workflow_id: str) -> None:
    if runner_mod.is_workflow_running(workspace_uuid, workflow_id):
        raise WorkflowBusyError("工作流正在运行，无法删除")
    with store.store_lock():
        if not store.delete_workflow(workspace_uuid, workflow_id):
            raise WorkflowNotFoundError("工作流不存在")
    _publish_changed(workspace_uuid, workflow_id, "deleted")


def run_workflow(workspace_uuid: str, workflow_id: str,
                 inputs: dict | None = None, origin_session_id: str = "") -> WorkflowState:
    workflow = get_workflow(workspace_uuid, workflow_id)
    state = store.load_state(workspace_uuid, workflow_id)
    if runner_mod.is_workflow_running(workspace_uuid, workflow_id):
        raise WorkflowBusyError("工作流正在运行")
    values = validate_run_inputs(workflow, inputs)
    state.origin_session_id = origin_session_id or state.panel_session_id
    store.save_state(workspace_uuid, workflow_id, state)
    started = runner_mod.start_workflow_runner(
        workspace_uuid, workflow, state, inputs=values,
        origin_session_id=state.origin_session_id)
    if started is None:
        raise WorkflowBusyError("工作流正在运行或启动失败")
    _publish_changed(workspace_uuid, workflow_id, "run_started")
    return store.load_state(workspace_uuid, workflow_id)




def run_workflow_step(workspace_uuid: str, workflow_id: str,
                      step: Any) -> WorkflowState:
    """只重跑某一步：复用上次运行的目录与上游产物，下游步骤不重跑。"""
    workflow = get_workflow(workspace_uuid, workflow_id)
    state = store.load_state(workspace_uuid, workflow_id)
    if runner_mod.is_workflow_running(workspace_uuid, workflow_id):
        raise WorkflowBusyError("工作流正在运行")
    if not state.run_id:
        raise WorkflowValidationError("还没有运行记录：请先完整运行一次工作流，再按步重跑")
    index = resolve_step_index(workflow, step)
    started = runner_mod.start_workflow_step_runner(
        workspace_uuid, workflow, state, index)
    if started is None:
        raise WorkflowBusyError("工作流正在运行或启动失败")
    _publish_changed(workspace_uuid, workflow_id, "step_started")
    return store.load_state(workspace_uuid, workflow_id)


def stop_workflow(workspace_uuid: str, workflow_id: str) -> bool:
    get_workflow(workspace_uuid, workflow_id)
    stopped = runner_mod.stop_workflow_runner(workspace_uuid, workflow_id)
    if stopped:
        _publish_changed(workspace_uuid, workflow_id, "stopped")
    return stopped
