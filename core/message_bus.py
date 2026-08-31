"""MessageBus - cross-session message passing.

Module-level singleton (following BrowserService/CronScheduler pattern).
Thread-safe: all operations protected by threading.Lock.

Design goals:
- Lightweight in-memory message queue per session
- No persistence needed (messages are ephemeral)
- Sessions can send/receive messages asynchronously
- MessageBus does NOT inject messages into agent loops;
  agents must use the message_bus tool to check for messages

Usage:
    from core.message_bus import get_message_bus

    bus = get_message_bus()
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
class Message:
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


class MessageBus:
    """Cross-session message bus.

    Thread-safe singleton. Messages are stored per session as a list.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # session_id -> list[Message]
        self._messages: dict[str, list[Message]] = {}
        # session_id -> display name (optional, for listing)
        self._session_names: dict[str, str] = {}

    def register_session(self, session_id: str, name: str = "") -> None:
        """Register a session with the message bus."""
        with self._lock:
            if session_id not in self._messages:
                self._messages[session_id] = []
            if name:
                self._session_names[session_id] = name

    def unregister_session(self, session_id: str) -> None:
        """Unregister a session, clearing all its messages."""
        with self._lock:
            self._messages.pop(session_id, None)
            self._session_names.pop(session_id, None)

    def send(self, from_session_id: str, to_session_id: str,
             content: str, message_type: str = "text") -> bool:
        """Send a message from one session to another.

        Returns True if sent successfully, False if target session not registered.
        """
        msg = Message(
            sender_session_id=from_session_id,
            content=content,
            message_type=message_type,
        )
        with self._lock:
            if to_session_id not in self._messages:
                # Auto-register target session (messages will wait)
                self._messages[to_session_id] = []
            self._messages[to_session_id].append(msg)
        logger.info(
            f"Message sent: {from_session_id} -> {to_session_id}: "
            f"{content[:50]}{'...' if len(content) > 50 else ''}"
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


# ========== Module-level singleton ==========

_message_bus: MessageBus | None = None
_bus_lock = threading.Lock()


def get_message_bus() -> MessageBus:
    """Get the global MessageBus singleton."""
    global _message_bus
    if _message_bus is None:
        with _bus_lock:
            if _message_bus is None:
                _message_bus = MessageBus()
    return _message_bus


def start_message_bus() -> MessageBus:
    """Initialize and return the global MessageBus (called at startup)."""
    return get_message_bus()


def stop_message_bus() -> None:
    """Clean up the global MessageBus."""
    global _message_bus
    with _bus_lock:
        if _message_bus is not None:
            _message_bus = None
