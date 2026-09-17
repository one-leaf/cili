"""Step 4 契约测试：Loop 统一骨架、LoopPolicy 实时读、PhaseMachine 阶段转移。

覆盖：interactive/autonomous 共用同一 _run_loop 骨架、预算注入仅 autonomous、
熔断仅 autonomous、LoopPolicy 运行时改 agent 属性立即生效、
PhaseMachine 检查开关/检查轮次上限/额度兜底跳过检查。
"""

from unittest.mock import MagicMock, patch

from core.agent import Agent
from core.agent_runtime.loop import LoopPolicy, PhaseMachine


# ─── 通用 mock 构造（与 test_worker_agent 一致）────────────────────────

def _make_mock_config(max_iterations=200):
    config = MagicMock()
    config.model = MagicMock()
    config.system.max_iterations = max_iterations
    return config


def _make_agent(role="worker", **kwargs):
    """构造指定角色 Agent，mock 掉工具实例化与 LLM client。"""
    config = kwargs.pop("config", None) or _make_mock_config()
    with patch("core.agent.create_tools") as mock_tools, \
         patch("core.agent.create_llm_client") as mock_client:
        mock_tools.return_value = []
        mock_client.return_value = MagicMock()
        return Agent(config, role=role, **kwargs)


def _make_tool_call_response(call_id="toolu_1", name="bash"):
    tc = MagicMock()
    tc.name = name
    tc.id = call_id
    tc.parse_arguments.return_value = {}
    resp = MagicMock()
    resp.get_tool_calls.return_value = [tc]
    resp.content_as_dicts.return_value = [
        {"type": "tool_use", "id": call_id, "name": name, "input": {}}
    ]
    resp.get_text.return_value = ""
    return resp


def _make_text_response(text):
    resp = MagicMock()
    resp.get_tool_calls.return_value = []
    resp.get_text.return_value = text
    resp.content_as_dicts.return_value = [{"type": "text", "text": text}]
    return resp


def _make_tool_result():
    return {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}


# ─── PhaseMachine 纯逻辑单测（无需真实 agent）───────────────────────────

class _FakePolicy:
    """PhaseMachine 单测用参数桩，避免依赖真实 agent。"""

    def __init__(
        self,
        mode="autonomous",
        budget_notice=False,
        check_phase=True,
        check_iterations=None,
    ):
        self.mode = mode
        self.budget_notice = budget_notice
        self.check_phase = check_phase
        self.check_iterations = check_iterations


class TestPhaseMachine:
    def test_interactive_always_complete(self):
        """interactive 单相：无工具调用轮恒判回合完成，无阶段转移。"""
        pm = PhaseMachine(_FakePolicy(mode="interactive"))
        assert pm.decide_no_tool_calls(False).action == "complete"
        assert pm.decide_no_tool_calls(True).action == "complete"

    def test_no_check_phase_delivers(self):
        """check_phase 关闭（lite）：无工具调用 → 直接交付。"""
        pm = PhaseMachine(_FakePolicy(mode="autonomous", check_phase=False))
        out = pm.decide_no_tool_calls(False)
        assert out.done is True
        assert out.action == "deliver"

    def test_check_phase_enters_check(self):
        """check_phase 开启且未进检查：无工具调用 → 转检查阶段。"""
        pm = PhaseMachine(_FakePolicy(mode="autonomous", check_phase=True))
        out = pm.decide_no_tool_calls(False)
        assert out.done is False
        assert out.action == "enter_check"

    def test_budget_final_skips_check(self):
        """额度兜底：final 触发后无工具调用 → 跳过检查直接交付。"""
        pm = PhaseMachine(_FakePolicy(mode="autonomous", budget_notice=True, check_phase=True))
        out = pm.decide_no_tool_calls(True)
        assert out.done is True
        assert out.action == "budget_wrapup"

    def test_check_complete_when_in_check(self):
        """检查阶段内无工具调用 → 任务真正完成。"""
        pm = PhaseMachine(_FakePolicy(mode="autonomous", check_phase=True))
        pm.enter_check(0)
        out = pm.decide_no_tool_calls(False)
        assert out.action == "check_complete"

    def test_enter_check_max_computation(self):
        """检查轮次上限以 max(已耗迭代, 配置) 起算；None = 不设上限。"""
        pm = PhaseMachine(_FakePolicy(check_phase=True, check_iterations=5))
        pm.enter_check(1)
        assert pm.max_check_iterations == 5  # max(1, 5)
        pm.enter_check(8)
        assert pm.max_check_iterations == 8  # max(8, 5)：不压缩检查轮次
        pm = PhaseMachine(_FakePolicy(check_phase=True, check_iterations=None))
        pm.enter_check(3)
        assert pm.max_check_iterations is None

    def test_on_tool_round_cap(self):
        """检查阶段工具调用轮计数：超过上限的第 6 轮触发强制收尾。"""
        pm = PhaseMachine(_FakePolicy(check_phase=True, check_iterations=5))
        pm.enter_check(1)  # max_check = 5
        caps = [pm.on_tool_round() for _ in range(6)]
        assert caps == [False, False, False, False, False, True]
        assert pm.check_iters == 6

    def test_on_tool_round_outside_check_is_free(self):
        """检查阶段外（主阶段）不计数，不触发上限。"""
        pm = PhaseMachine(_FakePolicy(check_phase=True, check_iterations=5))
        assert pm.on_tool_round() is False
        assert pm.check_iters == 0

    def test_mark_check_complete_counts(self):
        """检查阶段文本收尾轮计入 check_iterations。"""
        pm = PhaseMachine(_FakePolicy(check_phase=True, check_iterations=None))
        pm.enter_check(0)
        pm.mark_check_complete()
        assert pm.check_iters == 1


# ─── LoopPolicy 实时读 agent 属性 ─────────────────────────────────────

class TestLoopPolicyLiveReads:
    def test_max_iterations_live(self, agent):
        """运行时改 agent.max_iterations 立即生效（不冻结策略快照）。"""
        policy = agent.loop.policy
        agent.max_iterations = 10
        assert policy.max_iterations == 10
        agent.max_iterations = 3
        assert policy.max_iterations == 3

    def test_check_iterations_live(self, agent):
        """运行时改 role_cfg.check_iterations 立即生效。"""
        policy = agent.loop.policy
        agent.role_cfg.check_iterations = None
        assert policy.check_iterations is None
        agent.role_cfg.check_iterations = 5
        assert policy.check_iterations == 5

    def test_streaming_interactive_reads_agent(self, agent):
        """interactive 流式开关读 agent._streaming（run() 参数可覆盖）。"""
        policy = agent.loop.policy
        agent._streaming = False
        assert policy.streaming is False

    def test_circuit_breaker_off_for_interactive(self, agent):
        """熔断仅 autonomous：interactive 策略 max_consecutive_failures=None。"""
        assert agent.loop.policy.max_consecutive_failures is None

    def test_circuit_breaker_on_for_autonomous(self):
        """worker（autonomous）熔断阈值从角色配置读取。"""
        worker = _make_agent(role="worker", task="t")
        assert worker.loop.policy.max_consecutive_failures == 5


# ─── Loop 统一骨架 ────────────────────────────────────────────────────

class TestLoopUnifiedSkeleton:
    def test_both_entries_drive_shared_run_loop(self, agent):
        """run_interactive/run_autonomous 共用同一 _run_loop 骨架。"""
        with patch.object(agent.loop, "_run_loop") as m:
            agent.loop.run_interactive()
        m.assert_called_once_with(autonomous=False)

    def test_autonomous_entry_drives_run_loop(self):
        worker = _make_agent(role="worker", task="t")
        with patch.object(worker.loop, "_run_loop") as m:
            worker.loop.run_autonomous()
        m.assert_called_once_with(autonomous=True)

    def test_budget_injection_only_autonomous(self, agent):
        """预算注入是 autonomous 专属槽：interactive 循环不调用 _inject_budget_notice。"""
        with patch.object(agent, "_call_llm", return_value=_make_text_response("hi")), \
             patch.object(agent, "_inject_budget_notice") as spy:
            agent.run("hello")
        spy.assert_not_called()

    def test_worker_budget_injection_happens(self):
        """worker（autonomous + budget_notice）每轮调用预算注入槽。"""
        worker = _make_agent(role="worker", task="t")
        with patch.object(worker, "_check_and_compress"), \
             patch.object(worker, "_call_llm", return_value=_make_text_response("done")), \
             patch.object(worker, "_inject_budget_notice") as spy:
            worker.run()
        assert spy.call_count >= 1

    def test_interactive_phase_machine_is_single_phase(self, agent):
        """interactive 模式 PhaseMachine 恒返回 complete，无检查阶段转移。"""
        outcome = agent.loop.phase_machine.decide_no_tool_calls(False)
        assert outcome.action == "complete"

    def test_interactive_loop_driven_by_loop(self, agent):
        """交互回合经 Loop 统一骨架执行（run 入口直接委托 loop）。"""
        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_call_llm", return_value=_make_text_response("ok")) as call_mock:
            agent.run("hi")
        assert call_mock.call_count == 1
