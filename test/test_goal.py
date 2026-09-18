"""Tests for core/goal.py (GoalManager) and web/goal_runner.py (GoalRunner)."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

from core.goal import COMPLETE_MARKER, GoalManager
from web.goal_runner import GoalRunner, format_goal_status, start_goal_runner, stop_goal_runner


class FakeAgentTool:
    """最小 agent 工具替身：记录 execute 调用，按序返回 worker 结果。

    after_execute(idx) 在每次 execute 返回后回调（供测试在轮间注入 pause/stop）。
    """

    name = "agent"

    def __init__(self, summaries=("进展中",), after_execute=None):
        self.summaries = list(summaries)
        self.after_execute = after_execute
        self._idx = 0
        self.calls: list[str] = []

    def execute(self, task, agent_type="worker", **kwargs):
        self.calls.append(task)
        self.last_exec_id = kwargs.get("exec_id", "")
        text = self.summaries[min(self._idx, len(self.summaries) - 1)]
        self._idx += 1
        if self.after_execute:
            self.after_execute(self._idx)
        return SimpleNamespace(
            output=json.dumps(
                {"status": "completed", "summary": text, "iterations": 0, "message_count": 0},
                ensure_ascii=False,
            ),
            meta={"exec_id": f"exec_{self._idx}", "completed": True},
        )


class FakeAgent:
    """最小 master agent 替身：带 agent 工具 + 会话引用。"""

    def __init__(self, agent_tool, session_manager=None):
        self.tools = [agent_tool]
        self.session_manager = session_manager
        self._stopped = False


class FakeSessionManager:
    """模拟 SessionManager 的消息接口：记录 add_message/save（含 force）。"""

    def __init__(self, exec_id="exec_goal_1"):
        self.messages: list[dict] = []
        self.saves: list[bool] = []
        self._exec_id = exec_id
        self.agent_logs = SimpleNamespace(_generate_exec_id=lambda: self._exec_id)

    def add_message(self, role, content):
        self.messages.append({"role": role, "content": content})

    def save(self, force=False):
        self.saves.append(force)


class FakeBus:
    def __init__(self):
        self.events: list[dict] = []

    def publish(self, event):
        self.events.append(event)


class TestGoalManager:
    """GoalManager 状态机与持久化。"""

    def test_set_makes_active_armed(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("  目标内容  ")
        assert m.exists()
        assert m.state.objective == "目标内容"
        assert m.state.round == 0
        assert m.state.armed is True
        assert m.state.status == "active"
        assert m.is_active()

    def test_set_empty_is_noop(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("   \n ")
        assert not m.exists()

    def test_pause_resume(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("目标")
        m.pause()
        assert m.state.status == "paused"
        assert not m.is_active()
        m.resume()
        assert m.state.status == "active"
        assert m.state.armed is True
        assert m.is_active()

    def test_pause_without_goal_noop(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.pause()
        assert not m.exists()

    def test_clear(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("目标")
        m.clear()
        assert not m.exists()
        assert not m.is_active()

    def test_mark_complete(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("目标")
        m.mark_complete(summary="搞定")
        assert m.state.status == "complete"
        assert not m.is_active()
        assert m.state.last_summary == "搞定"

    def test_block(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("目标")
        m.block("round_limit")
        assert m.state.status == "blocked"
        assert m.state.blocked_reason == "round_limit"
        assert not m.is_active()

    def test_persistence_roundtrip(self, tmp_path):
        path = tmp_path / "s"
        m1 = GoalManager(path)
        m1.set("持久化目标")
        m1.state.round = 5
        m1.set_last_summary("上轮摘要")
        m1.save()

        m2 = GoalManager(path)
        assert m2.exists()
        assert m2.state.objective == "持久化目标"
        assert m2.state.round == 5
        assert m2.state.last_summary == "上轮摘要"

    def test_restart_disarms(self, tmp_path):
        path = tmp_path / "s"
        m1 = GoalManager(path)
        m1.set("目标")
        assert m1.is_active()

        m2 = GoalManager(path)  # 重启后 load：armed 强制置 False
        assert m2.exists()
        assert m2.state.armed is False
        assert not m2.is_active()

    def test_is_complete_marker(self):
        assert GoalManager.is_complete(f"工作完成\n{COMPLETE_MARKER}")
        assert not GoalManager.is_complete("状态: 进行中")

    def test_next_round_prompt_template(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("翻译 README")
        m.state.round = 3
        m.set_last_summary("已完成第一章")
        prompt = m.next_round_prompt()
        assert "目标: 翻译 README" in prompt
        assert "轮次: 3/20" in prompt
        assert "已完成第一章" in prompt
        assert "不要询问用户" in prompt
        assert COMPLETE_MARKER in prompt

    def test_next_round_prompt_no_summary(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("目标")
        m.state.round = 1
        prompt = m.next_round_prompt()
        assert "上一轮进展: 无" in prompt

    def test_format_goal_status(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("翻译")
        text = format_goal_status(m)
        assert "**目标：** 翻译" in text
        assert "执行中" in text
        assert "**轮次：** 0/20" in text


class TestGoalRunner:
    """GoalRunner 轮次循环：master 经 agent 工具委派 worker——完成即停 / 轮次上限 / 暂停 / 停止 / 事件序列。"""

    @staticmethod
    def _make_runner(agent, manager, ws="ws1", sess="s1"):
        return GoalRunner(ws, sess, agent, manager, agent_tool=agent.tools[0])

    def test_completes_on_marker(self, tmp_path):
        tool = FakeAgentTool(summaries=[f"目标已达成\n{COMPLETE_MARKER}"])
        agent = FakeAgent(tool)
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        runner = self._make_runner(agent, m)
        runner.start()
        runner.thread.join(timeout=5)
        assert not runner.is_running()
        assert m.state.status == "complete"
        assert len(tool.calls) == 1
        assert "<goal_round>" in tool.calls[0]
        assert "测试目标" in tool.calls[0]

    def test_round_limit_blocks(self, tmp_path):
        tool = FakeAgentTool(summaries=["进展", "还是进展"])
        agent = FakeAgent(tool)
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        m.state.max_rounds = 2
        m.save()
        runner = self._make_runner(agent, m)
        runner.start()
        runner.thread.join(timeout=5)
        assert not runner.is_running()
        assert m.state.status == "blocked"
        assert m.state.blocked_reason == "round_limit"
        assert len(tool.calls) == 2

    def test_continues_until_marker(self, tmp_path):
        tool = FakeAgentTool(summaries=["第一轮", "第二轮", f"第三轮完成\n{COMPLETE_MARKER}"])
        agent = FakeAgent(tool)
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        m.state.max_rounds = 5
        m.save()
        runner = self._make_runner(agent, m)
        runner.start()
        runner.thread.join(timeout=5)
        assert m.state.status == "complete"
        assert m.state.round == 3
        assert len(tool.calls) == 3

    def test_pause_stops_after_round(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        m.state.max_rounds = 10
        m.save()
        tool = FakeAgentTool(summaries=["进展"], after_execute=lambda idx: m.pause() if idx == 1 else None)
        agent = FakeAgent(tool)
        runner = self._make_runner(agent, m)
        runner.start()
        runner.thread.join(timeout=5)
        assert not runner.is_running()
        assert m.state.status == "paused"
        assert len(tool.calls) == 1

    def test_request_stop_between_rounds(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        m.state.max_rounds = 10
        m.save()
        stop_now = threading.Event()
        first_done = threading.Event()

        def after_execute(idx):
            if idx == 1:
                first_done.set()
                stop_now.wait(timeout=5)

        tool = FakeAgentTool(summaries=["进展"], after_execute=after_execute)
        agent = FakeAgent(tool)
        runner = self._make_runner(agent, m)
        runner.start()
        assert first_done.wait(timeout=5)
        runner.request_stop()
        stop_now.set()
        runner.thread.join(timeout=5)
        assert not runner.is_running()
        assert len(tool.calls) == 1

    def test_agent_stopped_pauses(self, tmp_path):
        """UI 停止按钮置 master._stopped → goal 暂停（不空转，不启动新轮）。"""
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        m.state.max_rounds = 10
        m.save()
        tool = FakeAgentTool(summaries=["进展"])
        agent = FakeAgent(tool)
        agent._stopped = True
        runner = self._make_runner(agent, m)
        runner.start()
        runner.thread.join(timeout=5)
        assert not runner.is_running()
        assert m.state.status == "paused"
        assert len(tool.calls) == 0

    def test_event_sequence_on_bus(self, tmp_path):
        tool = FakeAgentTool(summaries=[f"完成\n{COMPLETE_MARKER}"])
        agent = FakeAgent(tool)
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        runner = self._make_runner(agent, m)
        bus = FakeBus()
        with patch("web.goal_runner.get_event_bus", return_value=bus):
            runner.start()
            runner.thread.join(timeout=5)
        # 轮次事件由真实 AgentTool 发布（exec_* + agent_start/complete），不在本单测范围；
        # GoalRunner 只发布 goal 级完成文本（无 exec_id → 主聊天渲染）
        assert not any("exec_id" in e for e in bus.events)
        assert len(bus.events) == 1
        assert bus.events[0]["content"].startswith("🎯 目标已完成")
        assert bus.events[0]["workspace_uuid"] == "ws1"
        assert bus.events[0]["session_id"] == "s1"

    def test_round_records_agent_ref(self, tmp_path):
        """每轮在 master 会话落 agent_ref 占位消息：exec_id 透传 + 完成后 _meta 更新。

        回归：前端 loadSession 重渲染 / 重载时 renderMessages 按 user tool_result 的
        _meta.exec_id 重建 worker 卡；若 goal runner 不落该消息，worker 卡会消失。
        """
        sm = FakeSessionManager("exec_goal_7")
        tool = FakeAgentTool(summaries=[f"完成\n{COMPLETE_MARKER}"])
        agent = FakeAgent(tool, session_manager=sm)
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        runner = self._make_runner(agent, m)
        runner.start()
        runner.thread.join(timeout=5)
        assert m.state.status == "complete"

        # 预生成的 exec_id 传给 execute（占位消息与事件流共用同一 id）
        assert tool.last_exec_id == "exec_goal_7"

        # 占位消息：assistant tool_use + user tool_result（带 _meta.exec_id）
        asst = next(
            msg for msg in sm.messages
            if msg["role"] == "assistant" and msg["content"][0].get("type") == "tool_use"
        )
        user = next(
            msg for msg in sm.messages
            if msg["role"] == "user" and msg["content"][0].get("type") == "tool_result"
        )
        assert asst["content"][0]["name"] == "agent"
        assert asst["content"][0]["id"] == "goal-exec_goal_7"
        block = user["content"][0]
        assert block["_meta"]["exec_id"] == "exec_goal_7"
        assert block["_meta"]["completed"] is True  # 完成后置为完成态
        assert "测试目标" in block["_meta"]["task_summary"]
        # 完成态保存必须强制重写（原地改 _meta 不触发版本变化，普通 save 会短路）
        assert sm.saves.count(True) >= 1

    def test_start_stop_registry(self, tmp_path):
        tool = FakeAgentTool(summaries=[f"完成\n{COMPLETE_MARKER}"])
        agent = FakeAgent(tool)
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        runner = start_goal_runner("ws9", "s9", agent, m)
        assert runner is not None
        runner.thread.join(timeout=5)
        assert m.state.status == "complete"
        stop_goal_runner("ws9:s9")  # 已自行注销，stop 应为无操作
