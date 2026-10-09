"""AgentMailbox - cross-session message passing.

Module-level singleton (following BrowserService/CronScheduler pattern).
Thread-safe: all operations protected by threading.Lock.

Design goals:
- Lightweight in-memory message queue per session
- No persistence needed (messages are ephemeral)
- Sessions can send/receive messages asynchronously
- AgentMailbox does NOT inject messages into agent loops;
  agents must use the message_bus tool to check for messages

Usage:
    from core.agent_mailbox import get_agent_mailbox

    bus = get_agent_mailbox()
    bus.send("session_a", "session_b", "Hello from A!")
    messages = bus.receive("session_b")
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BusMessage:
    """A single cross-session message."""
    sender_session_id: str
    content: str
    timestamp: float = field(default_factory=time.time)
    message_type: str = "text"  # "text", "command", "status"
    read: bool = False

    def to_dict(self) -> dict:
        return {
            "sender_session_id": self.sender_session_id,
            "content": self.content,
            "timestamp": self.timestamp,
            "message_type": self.message_type,
            "read": self.read,
        }


class AgentMailbox:
    """Cross-session message bus.

    Thread-safe singleton. Messages are stored per session as a list.
    """

    # 每个会话的消息上限：超出后丢弃最旧消息，防止长期运行内存无限增长
    # （消息只在 receive 或 unregister_session 时清理，会话不退出则一直累积）
    MAX_MESSAGES_PER_SESSION = 100

    # 日志里内容预览的截断长度
    LOG_CONTENT_TRUNCATE = 50

    def __init__(self):
        self._lock = threading.Lock()
        # session_id -> list[BusMessage]
        self._messages: dict[str, list[BusMessage]] = {}
        # session_id -> display name (optional, for listing)
        self._session_names: dict[str, str] = {}
        # agent_name -> session_id（agent 名字注册表，支持名字寻址）
        self._agent_registry: dict[str, str] = {}
        # session_id -> agent_name（反向映射，一个 session 可能对应多个 agent 名字，取最新）
        self._session_to_agents: dict[str, set[str]] = {}

    def register_session(self, session_id: str, name: str = "") -> None:
        """Register a session with the message bus."""
        with self._lock:
            if session_id not in self._messages:
                self._messages[session_id] = []
            if name:
                self._session_names[session_id] = name

    def unregister_session(self, session_id: str) -> None:
        """Unregister a session, clearing all its messages and agent mappings."""
        with self._lock:
            # 清理该 session 关联的所有 agent 名字
            for agent_name in list(self._session_to_agents.get(session_id, set())):
                self._agent_registry.pop(agent_name, None)
            self._session_to_agents.pop(session_id, None)
            self._messages.pop(session_id, None)
            self._session_names.pop(session_id, None)

    def send(self, from_session_id: str, to_session_id: str,
             content: str, message_type: str = "text") -> bool:
        """Send a message from one session to another.

        Returns True if sent successfully, False if target session not registered.
        """
        msg = BusMessage(
            sender_session_id=from_session_id,
            content=content,
            message_type=message_type,
        )
        with self._lock:
            if to_session_id not in self._messages:
                # 目标未注册（不存在/拼错）不静默积压，返回 False 让发送方感知
                # 跨会话消息丢失（T21）
                logger.warning(
                    f"BusMessage drop: target session '{to_session_id}' not registered "
                    f"(sender: {from_session_id})"
                )
                return False
            queue = self._messages[to_session_id]
            queue.append(msg)
            if len(queue) > self.MAX_MESSAGES_PER_SESSION:
                del queue[: len(queue) - self.MAX_MESSAGES_PER_SESSION]
        logger.info(
            f"BusMessage sent: {from_session_id} -> {to_session_id}: "
            f"{content[:self.LOG_CONTENT_TRUNCATE]}"
            f"{'...' if len(content) > self.LOG_CONTENT_TRUNCATE else ''}"
        )
        return True

    def receive(self, session_id: str, mark_read: bool = True) -> list[dict]:
        """Receive all pending (unread) messages for a session.

        Returns list of message dicts. If mark_read=True, marks them as read
        (they stay in the list but won't be returned again).
        """
        with self._lock:
            messages = self._messages.get(session_id, [])
            unread = []
            for msg in messages:
                if not msg.read:
                    unread.append(msg.to_dict())
                    if mark_read:
                        msg.read = True
            return unread

    def has_unread(self, session_id: str) -> bool:
        """Check if a session has unread messages."""
        with self._lock:
            messages = self._messages.get(session_id, [])
            return any(not msg.read for msg in messages)

    def unread_count(self, session_id: str) -> int:
        """Count unread messages for a session."""
        with self._lock:
            messages = self._messages.get(session_id, [])
            return sum(1 for msg in messages if not msg.read)

    def list_sessions(self) -> list[dict]:
        """List all registered sessions with unread message counts."""
        with self._lock:
            result = []
            for sid in self._messages:
                messages = self._messages[sid]
                unread = sum(1 for msg in messages if not msg.read)
                name = self._session_names.get(sid, "")
                result.append({
                    "session_id": sid,
                    "name": name,
                    "total_messages": len(messages),
                    "unread_count": unread,
                })
            return result

    def clear(self, session_id: str) -> int:
        """Clear all messages for a session. Returns number of messages cleared."""
        with self._lock:
            messages = self._messages.get(session_id, [])
            count = len(messages)
            self._messages[session_id] = []
            return count

    # ========== Agent 级方法 ==========

    def register_agent(self, agent_name: str, session_id: str) -> None:
        """Register an agent name mapped to a session.

        Ensures the session is also registered in the message queue.
        Multiple agent names can map to the same session (e.g. exec_id + label).
        """
        with self._lock:
            self._agent_registry[agent_name] = session_id
            self._session_to_agents.setdefault(session_id, set()).add(agent_name)
            # 确保目标 session 有消息队列
            if session_id not in self._messages:
                self._messages[session_id] = []

    def unregister_agent(self, agent_name: str) -> None:
        """Unregister an agent name. Does NOT clear the session's messages."""
        with self._lock:
            session_id = self._agent_registry.pop(agent_name, None)
            if session_id:
                agents_for_session = self._session_to_agents.get(session_id)
                if agents_for_session:
                    agents_for_session.discard(agent_name)
                    if not agents_for_session:
                        del self._session_to_agents[session_id]

    def send_to_agent(
        self, from_agent: str, to_agent: str,
        content: str, message_type: str = "text",
    ) -> bool:
        """Send a message to an agent by name.

        Resolves both names to session IDs via the agent registry.
        Returns False if the target agent is not registered.
        """
        with self._lock:
            target_session = self._agent_registry.get(to_agent)
            if target_session is None:
                logger.warning(
                    f"Agent drop: target agent '{to_agent}' not registered "
                    f"(sender: {from_agent})"
                )
                return False
            from_session = self._agent_registry.get(from_agent, from_agent)
            msg = BusMessage(
                sender_session_id=from_session,
                content=content,
                message_type=message_type,
            )
            queue = self._messages.get(target_session, [])
            queue.append(msg)
            if len(queue) > self.MAX_MESSAGES_PER_SESSION:
                del queue[: len(queue) - self.MAX_MESSAGES_PER_SESSION]
        logger.info(
            f"Agent message sent: {from_agent} -> {to_agent}: "
            f"{content[:self.LOG_CONTENT_TRUNCATE]}"
            f"{'...' if len(content) > self.LOG_CONTENT_TRUNCATE else ''}"
        )
        return True

    def list_agents(self) -> list[dict]:
        """List all registered agents with their session info and unread counts."""
        with self._lock:
            result = []
            for agent_name, session_id in self._agent_registry.items():
                messages = self._messages.get(session_id, [])
                unread = sum(1 for m in messages if not m.read)
                result.append({
                    "agent_name": agent_name,
                    "session_id": session_id,
                    "total_messages": len(messages),
                    "unread_count": unread,
                })
            return result

    def get_agent_session(self, agent_name: str) -> str | None:
        """Get the session_id for an agent name. Returns None if not registered."""
        with self._lock:
            return self._agent_registry.get(agent_name)

    def get_session_agents(self, session_id: str) -> list[str]:
        """Get all agent names registered for a session."""
        with self._lock:
            return list(self._session_to_agents.get(session_id, set()))

    def get_session_agent(self, session_id: str) -> str | None:
        """Get the primary agent name for a session. Returns None if none registered."""
        with self._lock:
            agents = self._session_to_agents.get(session_id, set())
            return next(iter(agents)) if agents else None


# ========== Module-level singleton ==========

_agent_mailbox: AgentMailbox | None = None
_bus_lock = threading.Lock()


def get_agent_mailbox() -> AgentMailbox:
    """Get the global AgentMailbox singleton."""
    global _agent_mailbox
    if _agent_mailbox is None:
        with _bus_lock:
            if _agent_mailbox is None:
                _agent_mailbox = AgentMailbox()
    return _agent_mailbox


def start_agent_mailbox() -> AgentMailbox:
    """Initialize and return the global AgentMailbox (called at startup)."""
    return get_agent_mailbox()


def stop_agent_mailbox() -> None:
    """Clean up the global AgentMailbox."""
    global _agent_mailbox
    with _bus_lock:
        if _agent_mailbox is not None:
            _agent_mailbox = None
