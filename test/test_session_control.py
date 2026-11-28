"""会话活动快照与两级停止测试。

覆盖：
- BackgroundTask 的 workspace/session 归属与快照过滤；
- stop_background 对 agent/shell 的停止传播；
- /status 的 background/busy 字段；
- /stop 的 turn/background 作用域；
- 公共命令 /stop bg。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import core.session_control as session_control
from core.tools.agent_background import AgentBackgroundMixin
from core.tools.background import (
    BackgroundTask,
    BackgroundTaskManager,
    owner_session_id,
)


@pytest.fixture(autouse=True)
def clean_background_tasks():
    """每个用例使用独立的全局任务表，避免互相污染。"""
    with BackgroundTaskManager._lock:
        previous = dict(BackgroundTaskManager._tasks)
        BackgroundTaskManager._tasks.clear()
    yield
    with BackgroundTaskManager._lock:
        BackgroundTaskManager._tasks.clear()
        BackgroundTaskManager._tasks.update(previous)


class _Registry:
    def __init__(self, runner):
        self.runner = runner

    def get(self, key):
        return self.runner

    def __contains__(self, key):
        return self.runner is not None


def _task(task_id: str, *, ws="ws-1", sid="sess-1", kind="agent", **kwargs):
    task = BackgroundTask(
        task_id=task_id,
        workspace_uuid=ws,
        session_id=sid,
        kind=kind,
        label=task_id,
        exec_id=f"exec_{task_id}" if kind == "agent" else None,
        **kwargs,
    )
    BackgroundTaskManager.register(task)
    return task


def test_owner_session_id_prefers_master():
    assert owner_session_id(SimpleNamespace(session_id="sub-1", master_session_id="master-1")) == "master-1"
    assert owner_session_id(SimpleNamespace(session_id="master-1")) == "master-1"
    assert owner_session_id(None) == ""


def test_snapshot_filters_workspace_and_session():
    _task("agent-1", ws="ws-1", sid="sess-1", kind="agent")
    _task("bg-1", ws="ws-1", sid="sess-1", kind="shell")
    _task("agent-2", ws="ws-2", sid="sess-1", kind="agent")
    _task("agent-3", ws="ws-1", sid="sess-2", kind="agent")

    snap = BackgroundTaskManager.session_snapshot("ws-1", "sess-1")
    assert snap["agents"] == 1
    assert snap["shells"] == 1
    assert snap["active"] == 2
    assert {item["task_id"] for item in snap["items"]} == {"agent-1", "bg-1"}


def test_snapshot_excludes_finished_tasks():
    _task("agent-1", status="stopped", result={"status": "stopped"})
    _task("bg-1", kind="shell")
    # shell 无 process 时按 status 判断；显式 killed 不算活跃
    BackgroundTaskManager.get("bg-1").status = "killed"

    snap = BackgroundTaskManager.session_snapshot("ws-1", "sess-1")
    assert snap["active"] == 0
    assert snap["items"] == []


def test_stop_background_stops_agent_and_shell(monkeypatch):
    stopped = []
    monkeypatch.setattr(
        "core.session_control.ShellMixin._kill_process_tree",
        staticmethod(lambda proc: stopped.append(proc)),
    )

    runner = MagicMock()
    agent = _task("agent-1", kind="agent", runner=runner)
    process = MagicMock()
    process.poll.return_value = None
    shell = _task("bg-1", kind="shell", process=process)

    count = session_control.stop_background("ws-1", "sess-1")

    assert count == 2
    runner.stop.assert_called_once()
    assert agent.stop_requested is True
    assert agent.suppress_auto_resume is True
    assert agent.status == "stopping"
    assert stopped == [process]
    assert shell.stop_requested is True
    assert shell.status == "killed"


def test_stop_session_includes_turn_and_background(monkeypatch):
    runner = SimpleNamespace(
        running=True,
        is_running=lambda: True,
        stop=MagicMock(),
    )
    registry = SimpleNamespace(get=lambda key: runner)
    monkeypatch.setattr("core.session_registry.registry", registry)
    _task("agent-1", kind="agent", runner=MagicMock())

    result = session_control.stop_session("ws-1", "sess-1")

    assert result == {"turn": True, "background": 1}
    runner.stop.assert_called_once()


def test_status_route_includes_background(monkeypatch):
    from web import routes_chat

    runner = SimpleNamespace(is_running=lambda: False)
    monkeypatch.setattr(routes_chat, "registry", _Registry(runner))
    _task("agent-1", kind="agent")

    result = asyncio.run(routes_chat.get_runner_status("ws-1", "sess-1"))

    assert result["running"] is False
    assert result["busy"] is True
    assert result["background"]["agents"] == 1
    assert result["background"]["active"] == 1


def test_stop_route_turn_scope(monkeypatch):
    from web import routes_chat

    runner = SimpleNamespace(
        running=True,
        is_running=lambda: True,
        stop=MagicMock(),
    )
    monkeypatch.setattr(routes_chat, "registry", _Registry(runner))

    result = asyncio.run(routes_chat.stop_runner("ws-1", "sess-1", None))

    assert result["success"] is True
    assert result["stopped"] == {"turn": True, "background": 0}
    runner.stop.assert_called_once()


def test_background_status_is_not_placeholder():
    """agent action=status 的 tool meta 不能带 completed=False。

    completed=False 是 ask_user/审批占位协议；后台仍在跑时带它会让主循环
    误判为等待外部输入，直接结束回合、不产生回复正文。
    """
    runner = MagicMock()
    runner.messages = []
    runner._current_tool = "none"
    _task("agent-1", kind="agent", runner=runner)

    result = AgentBackgroundMixin._read_background_runner(
        SimpleNamespace(), "agent-1"
    )

    assert result.meta["completed"] is True
    assert result.meta["background_running"] is True


def test_kill_runner_is_not_placeholder():
    runner = MagicMock()
    task = _task("agent-1", kind="agent", runner=runner)

    result = AgentBackgroundMixin._kill_background_runner(
        SimpleNamespace(_publish=None), "agent-1"
    )

    assert result.meta["completed"] is True
    assert result.meta["status"] == "stopping"
    assert task.stop_requested is True
    assert BackgroundTaskManager.get("agent-1") is task


def test_stop_route_background_scope_without_master(monkeypatch):
    from web import routes_chat

    monkeypatch.setattr(routes_chat, "registry", _Registry(None))
    _task("agent-1", kind="agent", runner=MagicMock())

    result = asyncio.run(routes_chat.stop_runner(
        "ws-1", "sess-1", routes_chat.StopRequest(scope="background")
    ))

    assert result["success"] is True
    assert result["stopped"] == {"turn": False, "background": 1}


def test_stop_route_turn_scope_does_not_touch_background(monkeypatch):
    from web import routes_chat

    runner = SimpleNamespace(
        running=True,
        is_running=lambda: True,
        stop=MagicMock(),
    )
    monkeypatch.setattr(routes_chat, "registry", _Registry(runner))
    agent = _task("agent-1", kind="agent", runner=MagicMock())

    result = asyncio.run(routes_chat.stop_runner(
        "ws-1", "sess-1", routes_chat.StopRequest(scope="turn")
    ))

    assert result["stopped"] == {"turn": True, "background": 0}
    assert agent.stop_requested is False
    assert agent.runner.stop.called is False


def test_common_stop_bg(monkeypatch):
    from interfaces.common import commands

    monkeypatch.setattr(
        commands.session_control, "stop_background", lambda ws, sid: 2
    )
    ctx = SimpleNamespace(
        chat=SimpleNamespace(workspace_uuid="ws-1", session_id="sess-1"),
        args="bg",
    )
    assert "已停止 2 个后台任务" in commands._cmd_stop(ctx)
