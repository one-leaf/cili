"""会话管理测试 - 使用 SessionManager"""

import os
from pathlib import Path
from core.session import SessionManager


class TestSessionManagement:
    """会话管理功能测试"""

    def test_session_creation(self, test_workspace):
        """测试会话创建"""
        sessions_dir = Path(test_workspace) / ".sessions"
        session = SessionManager.create_new_session(sessions_dir, "Test")
        sessions = SessionManager.list_sessions(sessions_dir)
        assert len(sessions) >= 1
        assert sessions[0]["session_id"] == session.session_id

    def test_session_new(self, test_workspace):
        """测试创建新会话"""
        sessions_dir = Path(test_workspace) / ".sessions_new"
        SessionManager.create_new_session(sessions_dir, "Session 1")
        initial_count = len(SessionManager.list_sessions(sessions_dir))

        SessionManager.create_new_session(sessions_dir, "Test Session")
        sessions = SessionManager.list_sessions(sessions_dir)
        assert len(sessions) == initial_count + 1
        assert any(s["name"] == "Test Session" for s in sessions)

    def test_session_messages(self, test_workspace):
        """测试会话消息管理"""
        sessions_dir = Path(test_workspace) / ".sessions_msgs"
        session = SessionManager.create_new_session(sessions_dir, "Msg Test")

        session.add_message("user", "Hello")
        session.add_message("assistant", [{"type": "text", "text": "Hi!"}])

        assert session.get_message_count() == 2
        messages = session.get_messages()
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    def test_session_persistence(self, test_workspace):
        """测试会话持久化"""
        sessions_dir = Path(test_workspace) / ".sessions_persist"

        # 创建会话并添加消息
        session1 = SessionManager.create_new_session(sessions_dir, "Persist Test")
        session1.add_message("user", "Test message")
        session1.save()
        session_id = session1.session_id

        # 从磁盘加载（模拟重启）
        session2 = SessionManager.load_session(session_id, sessions_dir)
        assert session2 is not None
        assert session2.get_message_count() == 1

        # 列出会话应该能找到
        sessions = SessionManager.list_sessions(sessions_dir)
        assert any(s["session_id"] == session_id for s in sessions)

    def test_session_delete(self, test_workspace):
        """测试会话删除"""
        sessions_dir = Path(test_workspace) / ".sessions_del"
        session = SessionManager.create_new_session(sessions_dir, "To Delete")
        session_id = session.session_id

        initial_count = len(SessionManager.list_sessions(sessions_dir))
        session.delete()
        sessions = SessionManager.list_sessions(sessions_dir)

        assert len(sessions) == initial_count - 1
        assert not any(s["session_id"] == session_id for s in sessions)

    def test_session_clear(self, test_workspace):
        """测试清空会话"""
        sessions_dir = Path(test_workspace) / ".sessions_clear"
        session = SessionManager.create_new_session(sessions_dir, "Clear Test")
        session.add_message("user", "Message 1")
        session.add_message("user", "Message 2")

        assert session.get_message_count() == 2

        session.clear()
        assert session.get_message_count() == 0

    def test_valid_messages_filter(self, test_workspace):
        """测试有效消息过滤"""
        sessions_dir = Path(test_workspace) / ".sessions_filter"
        session = SessionManager.create_new_session(sessions_dir, "Filter Test")

        session.add_message("user", "Hello")
        session.add_message("assistant", [
            {"type": "thinking", "thinking": "Let me think...", "_valid": False},
            {"type": "text", "text": "Response"},
        ])

        valid = session.get_valid_messages()
        assert len(valid) == 2
        # Thinking block should be filtered out
        assistant_content = valid[1]["content"]
        assert len(assistant_content) == 1
        assert assistant_content[0]["type"] == "text"

    def test_usage_tracking(self, test_workspace):
        """测试使用量追踪"""
        sessions_dir = Path(test_workspace) / ".sessions_usage"
        session = SessionManager.create_new_session(sessions_dir, "Usage Test")

        session.update_usage(input_tokens=100, output_tokens=50, api_calls=1)
        session.update_usage(input_tokens=200, output_tokens=100, api_calls=1)

        usage = session.get_usage()
        assert usage["input_tokens"] == 300
        assert usage["output_tokens"] == 150
        assert usage["api_calls"] == 2

    def test_rename_session(self, test_workspace):
        """测试会话重命名"""
        sessions_dir = Path(test_workspace) / ".sessions_rename"
        session = SessionManager.create_new_session(sessions_dir, "Old Name")

        session.rename("New Name")
        assert session.name == "New Name"

        session.save()
        loaded = SessionManager.load_session(session.session_id, sessions_dir)
        assert loaded.name == "New Name"
