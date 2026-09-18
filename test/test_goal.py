"""Tests for core/goal.py (GoalManager) and web/goal_runner.py (GoalRunner)."""

import threading
from unittest.mock import patch

from core.goal import COMPLETE_MARKER, GoalManager
from web.goal_runner import GoalRunner, format_goal_status, start_goal_runner, stop_goal_runner


class FakeAgent:
    """最小 agent 替身：记录 run 输入，按序返回 assistant 文本。

    after_run(idx) 在每轮 agent.run 返回后回调（供测试在轮间注入 pause/stop）。
    """

    def __init__(self, responses=("进展中",), after_run=None):
        self.responses = list(responses)
        self.after_run = after_run
        self._idx = 0
        self.messages: list[dict] = []
        self.session_manager = None
        self.run_calls: list[str] = []

    def run(self, user_input, **kwargs):
        self.run_calls.append(user_input)
        text = self.responses[min(self._idx, len(self.responses) - 1)]
        self._idx += 1
        self.messages.append({"role": "assistant", "content": text})
        if self.after_run:
            self.after_run(self._idx)


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
    """GoalRunner 轮次循环：完成即停 / 轮次上限 / 暂停 / 停止 / 事件序列。"""

    @staticmethod
    def _make_runner(agent, manager, ws="ws1", sess="s1"):
        return GoalRunner(ws, sess, agent, manager, lambda k: True, lambda k: None)

    def test_completes_on_marker(self, tmp_path):
        agent = FakeAgent(responses=[f"目标已达成\n{COMPLETE_MARKER}"])
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        runner = self._make_runner(agent, m)
        runner.start()
        runner.thread.join(timeout=5)
        assert not runner.is_running()
        assert m.state.status == "complete"
        assert len(agent.run_calls) == 1
        assert "<goal_round>" in agent.run_calls[0]
        assert "测试目标" in agent.run_calls[0]

    def test_round_limit_blocks(self, tmp_path):
        agent = FakeAgent(responses=["进展", "还是进展"])
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
        assert len(agent.run_calls) == 2

    def test_continues_until_marker(self, tmp_path):
        agent = FakeAgent(responses=["第一轮", "第二轮", f"第三轮完成\n{COMPLETE_MARKER}"])
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        m.state.max_rounds = 5
        m.save()
        runner = self._make_runner(agent, m)
        runner.start()
        runner.thread.join(timeout=5)
        assert m.state.status == "complete"
        assert m.state.round == 3
        assert len(agent.run_calls) == 3

    def test_pause_stops_after_round(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        m.state.max_rounds = 10
        m.save()
        agent = FakeAgent(responses=["进展"], after_run=lambda idx: m.pause() if idx == 1 else None)
        runner = self._make_runner(agent, m)
        runner.start()
        runner.thread.join(timeout=5)
        assert not runner.is_running()
        assert m.state.status == "paused"
        assert len(agent.run_calls) == 1

    def test_request_stop_between_rounds(self, tmp_path):
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        m.state.max_rounds = 10
        m.save()
        stop_now = threading.Event()
        first_done = threading.Event()

        def after_run(idx):
            if idx == 1:
                first_done.set()
                stop_now.wait(timeout=5)

        agent = FakeAgent(responses=["进展"], after_run=after_run)
        runner = self._make_runner(agent, m)
        runner.start()
        assert first_done.wait(timeout=5)
        runner.request_stop()
        stop_now.set()
        runner.thread.join(timeout=5)
        assert not runner.is_running()
        assert len(agent.run_calls) == 1

    def test_event_sequence_on_bus(self, tmp_path):
        agent = FakeAgent(responses=[f"完成\n{COMPLETE_MARKER}"])
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        runner = self._make_runner(agent, m)
        bus = FakeBus()
        with patch("web.goal_runner.get_event_bus", return_value=bus):
            runner.start()
            runner.thread.join(timeout=5)
        round_events = [e for e in bus.events if e.get("exec_id") == "goal-1"]
        types = [e["type"] for e in round_events]
        assert types[0] == "agent_start"          # 建卡片先于一切轮次事件
        assert types[-1] == "agent_complete"
        assert round_events[0]["task_summary"].startswith("目标循环第 1 轮")
        assert round_events[-1]["status"] == "completed"
        assert round_events[-1]["workspace_uuid"] == "ws1"
        assert round_events[-1]["session_id"] == "s1"

    def test_start_stop_registry(self, tmp_path):
        agent = FakeAgent(responses=[f"完成\n{COMPLETE_MARKER}"])
        m = GoalManager(tmp_path / "s")
        m.set("测试目标")
        runner = start_goal_runner("ws9", "s9", agent, m, lambda k: True, lambda k: None)
        assert runner is not None
        runner.thread.join(timeout=5)
        assert m.state.status == "complete"
        stop_goal_runner("ws9:s9")  # 已自行注销，stop 应为无操作
