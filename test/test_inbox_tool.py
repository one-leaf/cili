"""send_notification 工具：参数、来源字段与限流。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.session import SessionIdentity
from core.tools.registry import create_tools
from core.tools.send_notification import SendNotificationTool


def _tool(tmp_path, session_id="sess-1", name="测试会话"):
    tool = SendNotificationTool(
        cwd=str(tmp_path),
        workspace_uuid="test-uuid",
        session=SimpleNamespace(session_id=session_id, name=name),
    )
    tool.agent_role = "worker"
    tool.interface_profile = SimpleNamespace(name="web")
    return tool


def test_send_notification_fields(tmp_path, inbox_store):
    tool = _tool(tmp_path)
    result = tool.execute("完成", "正文", level="success")
    assert not result.is_error
    assert "Notification sent" in result.output

    items, total = inbox_store.list()
    assert total == 1
    item = items[0]
    assert item.title == "完成"
    assert item.body == "正文"
    assert item.level == "success"
    assert item.source == {"type": "tool", "agent": "worker", "interface": "web"}
    assert item.workspace_uuid == "test-uuid"
    assert item.workspace_name == "Test test-uuid"
    assert item.session_id == "sess-1"
    assert item.session_title == "测试会话"


def test_empty_title_rejected(tmp_path, inbox_store):
    tool = _tool(tmp_path)
    result = tool.execute("   ")
    assert result.is_error
    assert inbox_store.list()[1] == 0


def test_new_session_placeholder_title_dropped(tmp_path, inbox_store):
    tool = _tool(tmp_path, name="New Session")
    tool.execute("t")
    assert inbox_store.list()[0][0].session_title == ""


def test_subagent_notification_uses_root_session(tmp_path, inbox_store):
    tool = SendNotificationTool(
        cwd=str(tmp_path),
        workspace_uuid="test-uuid",
        session=SessionIdentity("exec-1", master_session_id="root-1"),
    )
    tool.execute("t")
    assert inbox_store.list()[0][0].session_id == "root-1"


def test_rate_limit(tmp_path, inbox_store, monkeypatch):
    monkeypatch.setattr("core.tools.send_notification.MAX_PER_SESSION_PER_MINUTE", 2)
    tool = _tool(tmp_path)
    assert not tool.execute("1").is_error
    assert not tool.execute("2").is_error
    third = tool.execute("3")
    assert third.is_error
    assert "too many notifications" in third.output
    assert inbox_store.list()[1] == 2


def test_rate_limit_is_per_session(tmp_path, inbox_store, monkeypatch):
    monkeypatch.setattr("core.tools.send_notification.MAX_PER_SESSION_PER_MINUTE", 1)
    assert not _tool(tmp_path, session_id="a").execute("a").is_error
    assert not _tool(tmp_path, session_id="b").execute("b").is_error
    assert _tool(tmp_path, session_id="a").execute("a2").is_error


@pytest.mark.parametrize("role", ["master", "worker", "workflow"])
def test_notification_tool_visible_to_roles(tmp_path, role):
    tools = create_tools(role=role, cwd=str(tmp_path), workspace_uuid="test-uuid")
    matches = [tool for tool in tools if tool.name == "send_notification"]
    assert len(matches) == 1
    assert matches[0].agent_role == role
