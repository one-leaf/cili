"""会话删除的唯一顺序：停编排 → 摘 runner/队列 → 清邮箱 → 删目录。

Web 与其他接入端共用；目标循环的停止由调用方注入（web 提供 GoalRunner 实现）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from core.agent_mailbox import get_agent_mailbox
from core.goal import release_goal_manager
from core.session import delete_session_dir, session_key
from core.session_registry import registry
from core.turn_queue import turn_queue


def purge_session(
    workspace_uuid: str,
    session_id: str,
    sessions_dir: Path,
    *,
    stop_goal: Callable[[str], None] | None = None,
) -> None:
    """停止并摘除会话的编排/队列/runner，再删目录。

    ``stop_goal`` 接收 session_key（web 传 GoalRunner 的停止实现）。
    """
    key = session_key(workspace_uuid, session_id)
    session_dir = Path(sessions_dir) / session_id
    if stop_goal is not None:
        stop_goal(key)
    with registry.lock:
        turn_queue.remove_session(key)
        registry.remove(key)
    # 邮箱按 session_id 分键：不清会让消息与 agent 名字在进程内永久累积
    get_agent_mailbox().unregister_session(session_id)
    delete_session_dir(Path(sessions_dir), session_id)
    release_goal_manager(session_dir)