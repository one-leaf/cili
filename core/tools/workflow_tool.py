"""Workflow tool - 维护或运行工作流。

两种模式：
- 工作流面板会话（``metadata.workflow_panel``）：完整 create/get/update/run/stop/status；
- 普通 master 会话：list/get/run/stop/status，用于通过对话调用工作流。

结果会话与步骤执行器不挂载该工具。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from core.workflows.model import WorkflowInputRequiredError, WorkflowValidationError
from core.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


def _svc():
    """延迟导入：避免 core.tools.registry → workflow_tool → service → runner 的循环。"""
    from core.workflows import service, store
    return service, store


class WorkflowTool(Tool):
    name = "workflow"
    description = (
        "Manage or run workflows in this workspace. In a workflow panel session the "
        "workflow is bound; in a normal chat session pass workflow_id explicitly and "
        "use list/get/run/stop/status/runs/get_run (create/update/update_inputs/delete are panel-only).\n\n"
        "Actions (action is REQUIRED):\n"
        "- list: list workflows in this workspace (id/name/status/inputs).\n"
        "- get: read one workflow definition and run state. Params: workflow_id (normal chat).\n"
        "- run: start a workflow; returns immediately. Params: workflow_id (normal chat), "
        "inputs: {key: value}. If required inputs are missing, returns "
        "{\"error\":\"missing_inputs\",\"missing\":[...]}; ask the user and retry.\n"
        "- stop: request the current run to stop. Params: workflow_id.\n"
        "- status: summarize the current/last run. Params: workflow_id.\n"
        "- runs: list recent run history. Params: workflow_id.\n"
        "- get_run: read one run snapshot. Params: workflow_id, run_id.\n"
        "- send_to_user: send the latest (or given run_id) per-step result files to "
        "the current chat. Params: workflow_id, run_id?.\n"
        "- delete: delete the workflow definition (panel only; only after explicit "
        "user confirmation). Params: workflow_id.\n"
        "- update_inputs: panel sessions only; replace all input definitions "
        "with input_defs (full array).\n"
        "- create/update: panel sessions only; create/update may also pass input_defs.\n\n"
        "## input_defs schema (create/update/update_inputs)\n"
        "[{key, label, type: \"text\"|\"textarea\", required?: bool, "
        "default?: string, description?: string}]\n"
        "- key: identifier used in step prompts; declare it when the workflow needs "
        "a value before it can start (document number, date, etc.).\n\n"
        "## steps schema\n"
        "[{id?, name, requirement, sources: [index or step_id], "
        "executor?: \"worker\"|\"lite\", reads?: [path], outputs?: [path]}]\n"
        "- sources use 0-based positions in the submitted list, or an existing step id; "
        "each source must refer to an earlier step. Sources only determine which upstream "
        "artifacts this step reads; steps do not fork each other's conversation.\n"
        "- executor defaults to worker; use lite for small read/summarize steps.\n"
        "- reads: paths/instructions the executor must read with tools; when omitted, "
        "the upstream result.md files are used. outputs: files the step must produce; "
        "when omitted, runs/{run_id}/steps/{n}/result.md is required.\n"
        "- ids are 8-hex short ids; omit to generate new ones; include to preserve existing steps.\n\n"
        "## Rules\n"
        "- Changes apply immediately (no draft/confirm). Only change what the user asked for; "
        "keep everything else unchanged; reply with a concise diff.\n"
        "- Run rejects while the workflow is running; update rejects while running.\n"
        "- Only run when the user explicitly asks; never invent required input values."
    )

    def __init__(self, *args, session=None, workspace_uuid: str = "", **kwargs):
        super().__init__(*args, session=session, workspace_uuid=workspace_uuid, **kwargs)
        self.workspace_uuid = workspace_uuid

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "run", "stop", "status",
                             "create", "update", "update_inputs",
                             "delete", "runs", "get_run", "send_to_user"],
                    "description": "Action to perform. REQUIRED.",
                },
                "workflow_id": {
                    "type": "string",
                    "description": "Target workflow id (normal chat; omitted in a bound panel).",
                },
                "run_id": {
                    "type": "string",
                    "description": "Run id for get_run.",
                },
                "inputs": {
                    "type": ["object", "array"],
                    "description": "run: {key: value}; create/update: input definitions array.",
                },
                "name": {"type": "string", "description": "Workflow name (create/update)."},
                "description": {"type": "string", "description": "Workflow description (create/update)."},
                "input_defs": {
                    "type": "array",
                    "description": "Full input-definition array (create/update/update_inputs).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "label": {"type": "string"},
                            "type": {"type": "string", "enum": ["text", "textarea"]},
                            "required": {"type": "boolean"},
                            "default": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["key", "label"],
                    },
                },
                "steps": {
                    "type": "array",
                    "description": "Full step list (create/update).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "requirement": {"type": "string"},
                            "sources": {
                                "type": "array",
                                "items": {"type": ["integer", "string"]},
                            },
                            "executor": {
                                "type": "string",
                                "enum": ["worker", "lite"],
                            },
                            "reads": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "outputs": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["name", "requirement"],
                    },
                },
            },
            "required": ["action"],
        }

    # ── 入口 ───────────────────────────────────────────────

    def execute(self, action: str | None = None, name: str | None = None,
                description: str | None = None,
                steps: list[dict[str, Any]] | None = None,
                workflow_id: str | None = None,
                inputs: Any = None,
                input_defs: list[dict[str, Any]] | None = None,
                run_id: str | None = None,
                **kwargs) -> ToolResult:
        action = str(action or "").strip()
        if not action:
            return ToolResult("Error: action is required", error=True)
        panel = self._panel_meta()
        service, _store = _svc()
        panel_id = str((panel or {}).get("workflow_id") or "")
        workflow_id = str(workflow_id or panel_id or "")
        try:
            if action == "list":
                return self._list()
            defs = input_defs
            if defs is None and isinstance(inputs, list):
                defs = inputs
            if action == "create":
                if panel is None:
                    return ToolResult("Error: create 仅在工作流面板会话可用", error=True)
                return self._create(panel_id, name, description, steps, defs)
            if not workflow_id:
                return ToolResult("Error: 需要 workflow_id；可先用 workflow.list 查看", error=True)
            if action == "get":
                return self._get(workflow_id)
            if action == "update":
                if panel is None:
                    return ToolResult("Error: update 仅在工作流面板会话可用", error=True)
                return self._update(workflow_id, name, description, steps, defs)
            if action == "update_inputs":
                if panel is None:
                    return ToolResult("Error: update_inputs 仅在工作流面板会话可用", error=True)
                if defs is None:
                    return ToolResult("Error: update_inputs 需要 input_defs 数组", error=True)
                return self._update(workflow_id, None, None, None, defs)
            if action == "run":
                return self._run(workflow_id, inputs)
            if action == "stop":
                return self._stop(workflow_id)
            if action == "status":
                return self._status(workflow_id)
            if action == "runs":
                return self._runs(workflow_id)
            if action == "get_run":
                if not run_id:
                    return ToolResult("Error: get_run 需要 run_id", error=True)
                return self._get_run(workflow_id, str(run_id))
            if action == "send_to_user":
                return self._send_to_user(workflow_id, run_id)
            if action == "delete":
                if panel is None:
                    return ToolResult("Error: delete 仅在工作流面板会话可用", error=True)
                return self._delete(workflow_id)
        except WorkflowInputRequiredError as exc:
            return ToolResult(json.dumps({
                "error": "missing_inputs",
                "message": str(exc),
                "missing": exc.missing,
            }, ensure_ascii=False), error=True)
        except WorkflowValidationError as exc:
            return ToolResult(f"Error: {exc}", error=True)
        except service.WorkflowBusyError as exc:
            return ToolResult(f"Error: {exc}", error=True)
        except service.WorkflowNotFoundError as exc:
            return ToolResult(f"Error: {exc}", error=True)
        return ToolResult(f"Error: unknown action {action!r}", error=True)

    # ── 动作 ───────────────────────────────────────────────

    def _list(self) -> ToolResult:
        service, _store = _svc()
        items = service.list_workflows(self.workspace_uuid)
        out = []
        for item in items:
            wf = item.get("workflow") or {}
            st = item.get("state") or {}
            out.append({
                "id": wf.get("id"),
                "name": wf.get("name"),
                "description": wf.get("description") or "",
                "status": st.get("status"),
                "inputs": wf.get("inputs") or [],
                "steps": len(wf.get("steps") or []),
            })
        return ToolResult(json.dumps({"workflows": out}, ensure_ascii=False),
                          completed=True)

    def _create(self, workflow_id: str, name, description, steps, inputs=None) -> ToolResult:
        service, store = _svc()
        if workflow_id:
            return ToolResult("Error: 当前会话已绑定工作流，不能重复创建", error=True)
        if not name or steps is None:
            return ToolResult("Error: create 需要 name 与 steps", error=True)
        self._teen_guard()
        payload = {"name": name, "description": description or "", "steps": steps}
        if inputs is not None:
            payload["inputs"] = inputs
        workflow = service.create_workflow(self.workspace_uuid, payload)
        state = store.load_state(self.workspace_uuid, workflow.id)
        session = self.session
        if session is not None and hasattr(session, "metadata"):
            session.metadata["workflow_panel"] = {"workflow_id": workflow.id}
            if hasattr(session, "rename"):
                session.rename(f"[工作流面板] {workflow.name}")
            try:
                session.save(force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[workflow] 绑定面板会话失败: %s", exc)
        state.panel_session_id = getattr(session, "session_id", state.panel_session_id)
        store.save_state(self.workspace_uuid, workflow.id, state)
        return ToolResult(json.dumps({
            "ok": True, "workflow_id": workflow.id, "name": workflow.name,
            "inputs": [i.to_dict() for i in workflow.inputs],
            "steps": [{"id": s.id, "name": s.name, "sources": s.sources}
                         for s in workflow.steps],
        }, ensure_ascii=False), completed=True)

    def _get(self, workflow_id: str) -> ToolResult:
        service, store = _svc()
        workflow = service.get_workflow(self.workspace_uuid, workflow_id)
        state = store.load_state(self.workspace_uuid, workflow_id)
        return ToolResult(json.dumps({
            "workflow": workflow.to_dict(),
            "index": {s.id: i for i, s in enumerate(workflow.steps)},
            "state": {
                "status": state.status,
                "run_id": state.run_id,
                "started_at": state.started_at,
                "finished_at": state.finished_at,
                "current_index": state.current_index,
                "panel_session_id": state.panel_session_id,
                "results_session_id": state.results_session_id,
                "origin_session_id": state.origin_session_id,
                "run_inputs": dict(state.run_inputs),
                "items": {sid: st.to_dict() for sid, st in state.items.items()},
            },
        }, ensure_ascii=False), completed=True)

    def _update(self, workflow_id: str, name, description, steps, inputs=None) -> ToolResult:
        service, _store = _svc()
        self._teen_guard()
        old = service.get_workflow(self.workspace_uuid, workflow_id)
        payload = {"name": old.name, "description": old.description,
                   "inputs": [i.to_dict() for i in old.inputs],
                   "steps": [s.to_dict() for s in old.steps]}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if inputs is not None:
            payload["inputs"] = inputs
        if steps is not None:
            payload["steps"] = steps
        workflow = service.update_workflow(self.workspace_uuid, workflow_id, payload)
        # 改名同步维护会话名
        if name and name != old.name:
            session = self.session
            if session is not None and hasattr(session, "rename"):
                try:
                    session.rename(f"[工作流面板] {workflow.name}")
                    if hasattr(session, "save"):
                        session.save()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[workflow] 同步会话名失败: %s", exc)
        return ToolResult(json.dumps({
            "ok": True,
            "workflow": workflow.to_dict(),
            "diff": _diff(old, workflow),
        }, ensure_ascii=False), completed=True)

    def _run(self, workflow_id: str, inputs: Any = None) -> ToolResult:
        service, _store = _svc()
        self._teen_guard()
        values = inputs if isinstance(inputs, dict) else None
        origin = str(getattr(self.session, "session_id", "") or "")
        state = service.run_workflow(self.workspace_uuid, workflow_id,
                                     inputs=values, origin_session_id=origin)
        return ToolResult(json.dumps({
            "ok": True, "status": "running", "run_id": state.run_id,
            "results_session_id": state.results_session_id,
        }, ensure_ascii=False), completed=True)

    def _stop(self, workflow_id: str) -> ToolResult:
        service, _store = _svc()
        self._teen_guard()
        stopped = service.stop_workflow(self.workspace_uuid, workflow_id)
        return ToolResult(json.dumps({"ok": True, "stopped": stopped}, ensure_ascii=False),
                          completed=True)

    def _status(self, workflow_id: str) -> ToolResult:
        service, store = _svc()
        workflow = service.get_workflow(self.workspace_uuid, workflow_id)
        state = store.load_state(self.workspace_uuid, workflow_id)
        items = []
        for index, step_def in enumerate(workflow.steps):
            state_step = state.items.get(step_def.id)
            items.append({
                "index": index,
                "name": step_def.name,
                "executor": step_def.executor,
                "status": state_step.status if state_step else "pending",
                "artifact": state_step.artifact if state_step else "",
                "duration_ms": state_step.duration_ms if state_step else 0,
                "summary": (state_step.summary[:500] if state_step else ""),
                "error": (state_step.error[:500] if state_step else ""),
            })
        return ToolResult(json.dumps({
            "status": state.status,
            "run_id": state.run_id,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "current_index": state.current_index,
            "results_session_id": state.results_session_id,
            "run_inputs": dict(state.run_inputs),
            "items": items,
        }, ensure_ascii=False), completed=True)

    def _runs(self, workflow_id: str) -> ToolResult:
        _service, store = _svc()
        return ToolResult(json.dumps({
            "runs": store.list_runs(self.workspace_uuid, workflow_id),
        }, ensure_ascii=False), completed=True)

    def _get_run(self, workflow_id: str, run_id: str) -> ToolResult:
        _service, store = _svc()
        data = store.read_run_json(self.workspace_uuid, workflow_id, run_id)
        if data is None:
            return ToolResult("Error: 运行记录不存在", error=True)
        return ToolResult(json.dumps({"run": data}, ensure_ascii=False), completed=True)

    def _send_to_user(self, workflow_id: str, run_id: str | None = None) -> ToolResult:
        service, _store = _svc()
        files = service.collect_result_files(
            self.workspace_uuid, workflow_id, str(run_id) if run_id else None)
        if not files:
            return ToolResult("Error: 这次运行没有可发送的结果文件", error=True)
        session = self.session
        session_id = getattr(session, "session_id", "") or ""
        if not session_id:
            return ToolResult("Error: 无法确定当前会话，文件未登记", error=True)
        from core.outbox import enqueue_files
        result = enqueue_files(
            f"{self.workspace_uuid}:{session_id}",
            [(f["path"], f["note"]) for f in files],
        )
        if result.overflow:
            return ToolResult(
                f"Error: 文件数超出本次交付上限（{len(files)} 个），未发送任何文件",
                error=True)
        rels = []
        for path in result.accepted:
            try:
                rels.append(os.path.relpath(path, self.cwd).replace("\\", "/"))
            except ValueError:
                rels.append(os.path.basename(path))
        return ToolResult(json.dumps({
            "ok": True, "count": len(result.accepted), "files": rels,
            "note": "文件会在本回合结束后自动出现在会话中",
        }, ensure_ascii=False), completed=True)

    def _delete(self, workflow_id: str) -> ToolResult:
        service, _store = _svc()
        self._teen_guard()
        service.delete_workflow(self.workspace_uuid, workflow_id)
        session = self.session
        if session is not None and hasattr(session, "metadata"):
            try:
                session.metadata["workflow_panel"] = {"workflow_id": ""}
                if hasattr(session, "rename"):
                    session.rename("[工作流面板] 新建工作流")
                if hasattr(session, "save"):
                    session.save(force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[workflow] 清理面板绑定失败: %s", exc)
        return ToolResult(json.dumps({"ok": True, "deleted": workflow_id},
                                     ensure_ascii=False), completed=True)

    # ── 辅助 ───────────────────────────────────────────────

    def _panel_meta(self) -> dict | None:
        session = self.session
        metadata = getattr(session, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        panel = metadata.get("workflow_panel")
        return panel if isinstance(panel, dict) else None

    @staticmethod
    def _teen_guard() -> None:
        from core.teen_mode import get_teen_mode
        teen = get_teen_mode()
        if (teen.is_enabled()
                and (teen.policy().get("workflows") or "deny") == "deny"
                and not teen.is_unlocked()):
            raise WorkflowValidationError("青少年模式已开启：工作流修改与运行需要家长授权")


def _diff(old, new) -> str:
    """给模型回显的简短变更摘要。"""
    old_names = [s.name for s in old.steps]
    new_names = [s.name for s in new.steps]
    parts: list[str] = []
    if old.name != new.name:
        parts.append(f"工作流名：{old.name} → {new.name}")
    if old.description != new.description:
        parts.append("描述已更新")
    added = [n for n in new_names if n not in old_names]
    removed = [n for n in old_names if n not in new_names]
    if added:
        parts.append("新增：" + "、".join(added))
    if removed:
        parts.append("删除：" + "、".join(removed))
    if old_names == new_names and [s.requirement for s in old.steps] != [
            s.requirement for s in new.steps]:
        parts.append("要求已更新")
    if [s.sources for s in old.steps] != [s.sources for s in new.steps]:
        parts.append("数据来源已更新")
    return "；".join(parts) or "无变化"
