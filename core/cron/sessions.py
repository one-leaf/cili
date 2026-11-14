"""每任务独立 cron 会话。"""

from __future__ import annotations

from pathlib import Path

from core.config import get_workspace_data_dir
from core.cron import store
from core.cron.model import Job, State
from core.session import SessionStore


def workspace_sessions_dir(workspace_uuid: str = "") -> Path:
    return get_workspace_data_dir(workspace_uuid) / "sessions"


def ensure_cron_session(
    workspace_uuid: str, job: Job, state: State
) -> SessionStore:
    """获取/创建该任务自己的 `[Cron] {name}` 会话（幂等）。"""
    sessions_dir = workspace_sessions_dir(workspace_uuid)
    sessions_dir.mkdir(parents=True, exist_ok=True)

    if state.cron_session_id:
        loaded = SessionStore.load_session(state.cron_session_id, sessions_dir)
        if loaded is not None:
            return loaded

    session = SessionStore.create_new_session(sessions_dir, f"[Cron] {job.name}")
    state.cron_session_id = session.session_id
    store.save_state(workspace_uuid, job.id, state)
    return session
