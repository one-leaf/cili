"""工作流会话辅助：工作区路径、根会话、维护/结果会话。"""

from __future__ import annotations

import logging
from pathlib import Path

from core.config import get_workspace_data_dir, load_workspace_config
from core.event_bus import get_event_bus
from core.session import SessionStore
from core.workflows import store

logger = logging.getLogger(__name__)


def _publish_session_changed(workspace_uuid: str, session_id: str) -> None:
    """新建会话后广播，前端会话列表据此重拉（面板/结果会话不走用户请求）。"""
    get_event_bus().publish({
        "type": "session_changed",
        "workspace_uuid": workspace_uuid,
        "session_id": session_id,
    })


def workspace_sessions_dir(workspace_uuid: str = "") -> Path:
    return get_workspace_data_dir(workspace_uuid) / "sessions"


def workspace_cwd(workspace_uuid: str = "") -> str:
    ws_config = load_workspace_config(workspace_uuid)
    if ws_config and ws_config.get("directory"):
        return str(ws_config["directory"])
    return str(Path(get_workspace_data_dir(workspace_uuid)).parent)


def workflow_metadata(workflow_id: str, *, run_id: str = "", step_id: str = "",
                  executor: str = "") -> dict:
    return {
        "hidden": True,
        "workflow": {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "step_id": step_id,
            "executor": executor,
        },
    }


def ensure_panel_session(workspace_uuid: str, workflow, state) -> SessionStore:
    """工作流的维护会话（编辑标签）：绑定 metadata.workflow_panel。"""
    sessions_dir = workspace_sessions_dir(workspace_uuid)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if state.panel_session_id:
        loaded = SessionStore.load_session(state.panel_session_id, sessions_dir)
        if loaded is not None:
            return loaded
    session = SessionStore.create_new_session(
        sessions_dir, f"[工作流面板] {workflow.name}",
        metadata={"hidden": True, "workflow_panel": {"workflow_id": workflow.id}},
    )
    state.panel_session_id = session.session_id
    store.save_state(workspace_uuid, workflow.id, state)
    _publish_session_changed(workspace_uuid, session.session_id)
    return session


def ensure_new_workflow_panel_session(workspace_uuid: str) -> SessionStore:
    """未绑定的新建工作流会話：工作区复用一个，避免堆积。"""
    sessions_dir = workspace_sessions_dir(workspace_uuid)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    for item in SessionStore.list_sessions(sessions_dir):
        panel = item.get("workflow_panel")
        if isinstance(panel, dict) and not panel.get("workflow_id"):
            loaded = SessionStore.load_session(item["session_id"], sessions_dir)
            if loaded is not None:
                return loaded
    session = SessionStore.create_new_session(
        sessions_dir, "[工作流面板] 新建工作流",
        metadata={"hidden": True, "workflow_panel": {"workflow_id": ""}},
    )
    _publish_session_changed(workspace_uuid, session.session_id)
    return session


def ensure_results_session(workspace_uuid: str, workflow, state,
                           *, fresh: bool = False) -> SessionStore:
    """运行结果会话（结果标签）：运行事件 + 临时对话。

    ``fresh=True`` 时每次运行新建一个结果会话，标签只展示最近一次运行；
    旧结果会话留在磁盘上（hidden），不做清理。
    """
    sessions_dir = workspace_sessions_dir(workspace_uuid)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if state.results_session_id:
        loaded = SessionStore.load_session(state.results_session_id, sessions_dir)
        if loaded is not None:
            if not fresh:
                return loaded
            # fresh 模式复用「还没跑过」的空结果会话，避免留下孤儿会话
            if not loaded.messages:
                return loaded
    name = f"[运行结果] {workflow.name}"
    if fresh and state.run_id:
        name = f"{name} · {state.run_id}"
    session = SessionStore.create_new_session(
        sessions_dir, name,
        metadata={"hidden": True, "run_results": {"workflow_id": workflow.id}},
    )
    state.results_session_id = session.session_id
    store.save_state(workspace_uuid, workflow.id, state)
    _publish_session_changed(workspace_uuid, session.session_id)
    return session


def find_workflow_by_panel_session(workspace_uuid: str, session: SessionStore) -> str:
    """会话绑定的 workflow_id（维护或结果会话）。"""
    for key in ("workflow_panel", "run_results"):
        panel = session.metadata.get(key)
        if isinstance(panel, dict) and panel.get("workflow_id"):
            return str(panel["workflow_id"])
    return ""
