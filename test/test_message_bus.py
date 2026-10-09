"""Tests for AgentMailbox tool and module."""

import pytest
from core.agent_mailbox import AgentMailbox, get_agent_mailbox, stop_agent_mailbox


class TestMessageBusModule:
    """AgentMailbox singleton and core operations."""

    def setup_method(self):
        """Reset singleton before each test."""
        stop_agent_mailbox()

    def teardown_method(self):
        stop_agent_mailbox()

    def test_get_agent_mailbox_singleton(self):
        bus1 = get_agent_mailbox()
        bus2 = get_agent_mailbox()
        assert bus1 is bus2

    def test_stop_and_recreate(self):
        bus1 = get_agent_mailbox()
        stop_agent_mailbox()
        bus2 = get_agent_mailbox()
        assert bus1 is not bus2

    def test_send_and_receive(self):
        bus = AgentMailbox()
        bus.register_session("sess_a")
        bus.register_session("sess_b")
        bus.send("sess_a", "sess_b", "Hello!")
        messages = bus.receive("sess_b")
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello!"
        assert messages[0]["sender_session_id"] == "sess_a"

    def test_receive_marks_read(self):
        bus = AgentMailbox()
        bus.register_session("a")
        bus.register_session("b")
        bus.send("a", "b", "msg1")
        bus.receive("b")
        # Second receive returns nothing
        assert bus.receive("b") == []

    def test_has_unread(self):
        bus = AgentMailbox()
        bus.register_session("a")
        bus.register_session("b")
        assert not bus.has_unread("b")
        bus.send("a", "b", "hello")
        assert bus.has_unread("b")
        bus.receive("b")
        assert not bus.has_unread("b")

    def test_unread_count(self):
        bus = AgentMailbox()
        bus.register_session("a")
        bus.register_session("b")
        bus.send("a", "b", "msg1")
        bus.send("a", "b", "msg2")
        assert bus.unread_count("b") == 2
        bus.receive("b")
        assert bus.unread_count("b") == 0

    def test_list_sessions(self):
        bus = AgentMailbox()
        bus.register_session("a", "Session A")
        bus.register_session("b", "Session B")
        bus.send("a", "b", "hello")
        sessions = bus.list_sessions()
        assert len(sessions) == 2
        names = {s["session_id"] for s in sessions}
        assert "a" in names
        assert "b" in names

    def test_clear_messages(self):
        bus = AgentMailbox()
        bus.register_session("a")
        bus.send("a", "a", "msg1")
        bus.send("a", "a", "msg2")
        count = bus.clear("a")
        assert count == 2
        assert bus.receive("a") == []

    def test_unregister_session(self):
        bus = AgentMailbox()
        bus.register_session("a")
        bus.send("a", "a", "msg")
        bus.unregister_session("a")
        assert bus.list_sessions() == []

    def test_send_to_unregistered_is_dropped(self):
        """T21：未注册目标不自动建队列，send 返回 False，消息被丢弃。"""
        bus = AgentMailbox()
        bus.register_session("a")
        ok = bus.send("a", "b", "hello")
        assert ok is False
        registered = {s["session_id"] for s in bus.list_sessions()}
        assert registered == {"a"}
        assert bus.receive("b") == []

    def test_send_to_registered_succeeds(self):
        bus = AgentMailbox()
        bus.register_session("a")
        bus.register_session("b")
        ok = bus.send("a", "b", "hello")
        assert ok is True
        assert len(bus.receive("b")) == 1

    # ========== Agent 级测试 ==========

    def test_register_agent(self):
        bus = AgentMailbox()
        bus.register_agent("worker-1", "session-1")
        assert bus.get_agent_session("worker-1") == "session-1"
        assert "worker-1" in bus.get_session_agents("session-1")

    def test_register_agent_creates_session_queue(self):
        """register_agent 应自动创建 session 消息队列。"""
        bus = AgentMailbox()
        bus.register_agent("worker-1", "session-1")
        # session 应该存在且可接收消息
        agents = bus.list_agents()
        assert len(agents) == 1
        assert agents[0]["agent_name"] == "worker-1"
        assert agents[0]["session_id"] == "session-1"

    def test_register_multiple_agents_same_session(self):
        """exec_id 和 label 可以同时映射到同一个 session。"""
        bus = AgentMailbox()
        bus.register_agent("agent-1", "session-1")
        bus.register_agent("translate-doc", "session-1")
        assert bus.get_agent_session("agent-1") == "session-1"
        assert bus.get_agent_session("translate-doc") == "session-1"
        agents = bus.get_session_agents("session-1")
        assert set(agents) == {"agent-1", "translate-doc"}

    def test_unregister_agent(self):
        bus = AgentMailbox()
        bus.register_agent("worker-1", "session-1")
        bus.unregister_agent("worker-1")
        assert bus.get_agent_session("worker-1") is None
        assert bus.get_session_agents("session-1") == []

    def test_unregister_agent_preserves_session(self):
        """unregister_agent 不应清除 session 消息队列。"""
        bus = AgentMailbox()
        bus.register_agent("worker-1", "session-1")
        bus.send_to_agent("master", "worker-1", "hello")
        bus.unregister_agent("worker-1")
        # session 消息仍在
        msgs = bus.receive("session-1")
        assert len(msgs) == 1

    def test_send_to_agent(self):
        bus = AgentMailbox()
        bus.register_agent("master", "sess-master")
        bus.register_agent("worker-1", "sess-worker")
        ok = bus.send_to_agent("master", "worker-1", "do task")
        assert ok is True
        msgs = bus.receive("sess-worker")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "do task"
        assert msgs[0]["sender_session_id"] == "sess-master"

    def test_send_to_agent_with_label_alias(self):
        """通过 label 别名发送消息。"""
        bus = AgentMailbox()
        bus.register_agent("master", "sess-master")
        bus.register_agent("agent-1", "sess-worker")
        bus.register_agent("translate-doc", "sess-worker")
        ok = bus.send_to_agent("master", "translate-doc", "start now")
        assert ok is True
        msgs = bus.receive("sess-worker")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "start now"

    def test_send_to_unregistered_agent_fails(self):
        bus = AgentMailbox()
        bus.register_agent("master", "sess-master")
        ok = bus.send_to_agent("master", "nonexistent", "hello")
        assert ok is False

    def test_list_agents(self):
        bus = AgentMailbox()
        bus.register_agent("master", "sess-master")
        bus.register_agent("worker-1", "sess-worker")
        bus.send_to_agent("master", "worker-1", "task 1")
        agents = bus.list_agents()
        assert len(agents) == 2
        names = {a["agent_name"] for a in agents}
        assert names == {"master", "worker-1"}
        # worker-1 有 1 条未读
        worker_entry = [a for a in agents if a["agent_name"] == "worker-1"][0]
        assert worker_entry["unread_count"] == 1
        # master 无未读
        master_entry = [a for a in agents if a["agent_name"] == "master"][0]
        assert master_entry["unread_count"] == 0

    def test_get_session_agent(self):
        bus = AgentMailbox()
        bus.register_agent("worker-1", "sess-1")
        assert bus.get_session_agent("sess-1") == "worker-1"
        assert bus.get_session_agent("nonexistent") is None

    def test_unregister_session_cleans_agent_registry(self):
        """unregister_session 应同时清理 agent 注册表。"""
        bus = AgentMailbox()
        bus.register_agent("worker-1", "sess-1")
        bus.unregister_session("sess-1")
        assert bus.get_agent_session("worker-1") is None
        assert bus.list_agents() == []


class TestMessageBusTool:
    """MessageBusTool actions."""

    def setup_method(self):
        stop_agent_mailbox()
        from core.agent_mailbox import get_agent_mailbox
        self.bus = get_agent_mailbox()

    def teardown_method(self):
        stop_agent_mailbox()

    def _make_tool(self, session_id="test-session"):
        from core.session import SessionStore
        from core.tools.message_bus_tool import MessageBusTool
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        sm = SessionStore(session_id, tmp)
        return MessageBusTool(session=sm)

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

    def test_send_to_agent_action(self):
        self.bus.register_agent("test-session", "test-session")
        self.bus.register_agent("worker-1", "worker-session")
        tool = self._make_tool()
        result = tool.execute(action="send_to_agent", to_agent="worker-1", message="hello")
        assert not result.error
        assert "sent" in result.output.lower()
        msgs = self.bus.receive("worker-session")
        assert len(msgs) == 1

    def test_send_to_agent_missing_to_agent(self):
        self.bus.register_agent("test-session", "test-session")
        tool = self._make_tool()
        result = tool.execute(action="send_to_agent", message="hello")
        assert result.error

    def test_send_to_agent_missing_message(self):
        self.bus.register_agent("test-session", "test-session")
        tool = self._make_tool()
        result = tool.execute(action="send_to_agent", to_agent="worker-1")
        assert result.error

    def test_send_to_agent_unregistered(self):
        self.bus.register_agent("test-session", "test-session")
        tool = self._make_tool()
        result = tool.execute(action="send_to_agent", to_agent="nonexistent", message="hi")
        assert result.error

    def test_list_agents_action(self):
        self.bus.register_agent("master", "sess-1")
        self.bus.register_agent("worker-1", "sess-2")
        tool = self._make_tool()
        result = tool.execute(action="list_agents")
        assert not result.error
        assert "2" in result.output
        assert "master" in result.output
        assert "worker-1" in result.output

    def test_list_agents_empty(self):
        tool = self._make_tool()
        result = tool.execute(action="list_agents")
        assert not result.error
        assert "no registered" in result.output.lower()
