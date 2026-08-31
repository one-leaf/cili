"""Tests for MessageBus tool and module."""

import pytest
from core.message_bus import MessageBus, get_message_bus, stop_message_bus


class TestMessageBusModule:
    """MessageBus singleton and core operations."""

    def setup_method(self):
        """Reset singleton before each test."""
        stop_message_bus()

    def teardown_method(self):
        stop_message_bus()

    def test_get_message_bus_singleton(self):
        bus1 = get_message_bus()
        bus2 = get_message_bus()
        assert bus1 is bus2

    def test_stop_and_recreate(self):
        bus1 = get_message_bus()
        stop_message_bus()
        bus2 = get_message_bus()
        assert bus1 is not bus2

    def test_send_and_receive(self):
        bus = MessageBus()
        bus.register_session("sess_a")
        bus.register_session("sess_b")
        bus.send("sess_a", "sess_b", "Hello!")
        messages = bus.receive("sess_b")
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello!"
        assert messages[0]["sender_session_id"] == "sess_a"

    def test_receive_marks_read(self):
        bus = MessageBus()
        bus.register_session("a")
        bus.register_session("b")
        bus.send("a", "b", "msg1")
        bus.receive("b")
        # Second receive returns nothing
        assert bus.receive("b") == []

    def test_has_unread(self):
        bus = MessageBus()
        bus.register_session("a")
        bus.register_session("b")
        assert not bus.has_unread("b")
        bus.send("a", "b", "hello")
        assert bus.has_unread("b")
        bus.receive("b")
        assert not bus.has_unread("b")

    def test_unread_count(self):
        bus = MessageBus()
        bus.register_session("a")
        bus.register_session("b")
        bus.send("a", "b", "msg1")
        bus.send("a", "b", "msg2")
        assert bus.unread_count("b") == 2
        bus.receive("b")
        assert bus.unread_count("b") == 0

    def test_list_sessions(self):
        bus = MessageBus()
        bus.register_session("a", "Session A")
        bus.register_session("b", "Session B")
        bus.send("a", "b", "hello")
        sessions = bus.list_sessions()
        assert len(sessions) == 2
        names = {s["session_id"] for s in sessions}
        assert "a" in names
        assert "b" in names

    def test_clear_messages(self):
        bus = MessageBus()
        bus.register_session("a")
        bus.send("a", "a", "msg1")
        bus.send("a", "a", "msg2")
        count = bus.clear("a")
        assert count == 2
        assert bus.receive("a") == []

    def test_unregister_session(self):
        bus = MessageBus()
        bus.register_session("a")
        bus.send("a", "a", "msg")
        bus.unregister_session("a")
        assert bus.list_sessions() == []

    def test_send_to_unregistered_creates_queue(self):
        bus = MessageBus()
        bus.register_session("a")
        bus.send("a", "b", "hello")
        # "b" auto-registered
        messages = bus.receive("b")
        assert len(messages) == 1


class TestMessageBusTool:
    """MessageBusTool actions."""

    def setup_method(self):
        stop_message_bus()
        from core.message_bus import get_message_bus
        self.bus = get_message_bus()

    def teardown_method(self):
        stop_message_bus()

    def _make_tool(self, session_id="test-session"):
        from core.session import SessionManager
        from core.tools.shared.message_bus_tool import MessageBusTool
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        sm = SessionManager(session_id, tmp)
        return MessageBusTool(session_manager=sm)

    def test_send_action(self):
        self.bus.register_session("test-session")
        self.bus.register_session("other-session")
        tool = self._make_tool()
        result = tool.execute(action="send", to_session="other-session", message="hi")
        assert not result.error
        assert "sent" in result.output.lower()

    def test_receive_no_messages(self):
        self.bus.register_session("test-session")
        tool = self._make_tool()
        result = tool.execute(action="receive")
        assert not result.error
        assert "no pending" in result.output.lower()

    def test_send_missing_to_session(self):
        self.bus.register_session("test-session")
        tool = self._make_tool()
        result = tool.execute(action="send", message="hi")
        assert result.error

    def test_send_missing_message(self):
        self.bus.register_session("test-session")
        tool = self._make_tool()
        result = tool.execute(action="send", to_session="other")
        assert result.error

    def test_check_action(self):
        self.bus.register_session("test-session")
        tool = self._make_tool()
        result = tool.execute(action="check")
        assert not result.error
        assert "no unread" in result.output.lower()

    def test_clear_action(self):
        self.bus.register_session("test-session")
        self.bus.send("other", "test-session", "msg")
        tool = self._make_tool()
        result = tool.execute(action="clear")
        assert not result.error
        assert "cleared" in result.output.lower() or "0" in result.output

    def test_list_sessions_empty(self):
        tool = self._make_tool()
        result = tool.execute(action="list_sessions")
        assert not result.error
