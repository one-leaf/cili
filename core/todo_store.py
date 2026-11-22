"""Todo 持久化与会话视图。

存储：``{workspace}/.cili/tools/todo/{session_id}.json``；
兼容旧格式（session metadata 内的 todos），读取时自动迁移。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from core.config import get_workspace_tools_dir
from core.fs_utils import atomic_write_text
from core.time_utils import now_str

if TYPE_CHECKING:
    from core.session import SessionStore

logger = logging.getLogger(__name__)

def _get_todo_dir(workspace_uuid: str = "") -> Path:
    """Get the todo storage directory for a workspace."""
    return get_workspace_tools_dir(workspace_uuid) / "todo"


def get_todo_file_path(session_id: str, workspace_uuid: str = "") -> Path:
    """Get the path to the todo file for a given session."""
    return _get_todo_dir(workspace_uuid) / f"{session_id}.json"


def read_todos(session_id: str, workspace_uuid: str = "") -> list[dict]:
    """Read todos from the session's todo file."""
    todo_file = get_todo_file_path(session_id, workspace_uuid)
    if not todo_file.exists():
        return []
    try:
        data = json.loads(todo_file.read_text(encoding="utf-8"))
        return data.get("todos", [])
    except Exception as e:
        logger.warning(f"[todo] 读取 {todo_file} 失败: {e}")
        return []


def write_todos(session_id: str, todos: list[dict], workspace_uuid: str = "") -> None:
    """Write todos to the session's todo file."""
    todo_dir = _get_todo_dir(workspace_uuid)
    todo_dir.mkdir(parents=True, exist_ok=True)
    todo_file = get_todo_file_path(session_id, workspace_uuid)
    data = {
        "session_id": session_id,
        "updated_at": now_str(),
        "todos": todos,
    }
    atomic_write_text(todo_file, json.dumps(data, ensure_ascii=False, indent=2))


def todo_update_event(tool_name: str, is_error: bool, session: SessionStore | None,
                      workspace_uuid: str = "") -> dict | None:
    """工具执行后可能产生的 todo 更新事件（接口无关）。

    传输层（Web SSE / QQ / ...）只需把返回值序列化后下发，无需知道 todo 工具的名字
    与数据来源——原先这段逻辑硬编码在 web 的 SSE 回调里。
    """
    if tool_name != "todo_write" or is_error:
        return None
    todos = get_todos_from_session(session, workspace_uuid)
    if todos is None:
        return None
    # 带 session_id：推流可能持续到前端切走之后，接收端据此判断是否属于当前会话
    return {
        "type": "todo_update",
        "session_id": getattr(session, "session_id", "") or "",
        "todos": todos,
    }


def get_todos_from_session(session: SessionStore | None, workspace_uuid: str = "") -> list[dict] | None:
    """Helper to get current todos from session's todo file.

    Supports new format (independent file) and old format (session metadata).
    """
    if session is None:
        return None

    # Try new format: independent todo file
    session_id = getattr(session, 'session_id', None)
    has_file = False
    if session_id:
        todo_file = get_todo_file_path(session_id, workspace_uuid)
        has_file = todo_file.exists()
        if has_file:
            todos = read_todos(session_id, workspace_uuid)
            if todos:
                return todos

    # Fall back to old format: session metadata (for backward compatibility)
    if hasattr(session, 'metadata'):
        old_todos = session.metadata.get("todos")
        if old_todos:
            if session_id:
                # Migrate to new format（无 session_id 时跳过迁移，
                # 否则文件名会变成字面量 "None.json"）
                write_todos(session_id, old_todos, workspace_uuid)
                # Remove from session metadata
                del session.metadata["todos"]
            return old_todos

    # 文件存在但为空/损坏：显式返回 []（供“清空 todo”推送事件）；无文件则 None
    return [] if has_file else None
