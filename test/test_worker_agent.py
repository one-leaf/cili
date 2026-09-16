"""Tests for core/agent.py — Worker (autonomous) construction and pure logic."""

import os
from unittest.mock import MagicMock, patch

import pytest

from core.agent import Agent


def _make_mock_config(max_iterations=200):
    """构造 worker 测试用 mock 全局配置。"""
    config = MagicMock()
    config.model = MagicMock()
    config.system.max_iterations = max_iterations
    return config


def _make_agent(config=None, tools=None, **kwargs):
    """构造 Worker Agent，mock 掉工具实例化与 LLM client。"""
    if config is None:
        config = _make_mock_config()
    with patch("core.agent.create_tools") as mock_tools, \
         patch("core.agent.create_llm_client") as mock_client:
        mock_tools.return_value = tools or []
        mock_client.return_value = MagicMock()
        return Agent(config, role="worker", **kwargs)


class TestWorkerConstruction:
    """Worker Agent initialization."""

    def test_basic_construction(self):
        """Worker can be created with minimal params."""
        agent = _make_agent(task="test task")

        assert agent.task == "test task"
        assert agent.plan is None
        assert agent.role == "worker"
        assert agent.max_iterations == 200  # default (from config fallback)
        assert agent.max_consecutive_failures == 5  # default (from role JSON)
        assert agent.messages == []

    def test_custom_parameters(self):
        """Worker accepts custom parameters."""
        agent = _make_agent(
            task="complex task",
            plan=["step 1", "step 2"],
            max_consecutive_failures=10,
            workspace_uuid="test-ws",
            cwd="/tmp",
        )

        assert agent.task == "complex task"
        assert agent.plan == ["step 1", "step 2"]
        assert agent.max_iterations == 200  # from config
        assert agent.max_consecutive_failures == 10
        assert agent.workspace_uuid == "test-ws"
        assert os.path.abspath("/tmp") == agent.cwd

    def test_stop_check_stored(self):
        """stop_check callable is stored."""
        stop_fn = lambda: False
        agent = _make_agent(task="test", stop_check=stop_fn)

        assert agent.stop_check is stop_fn

    def test_exec_id_stored(self):
        """exec_id is stored for session tracking."""
        agent = _make_agent(task="test", exec_id="exec_abc123")

        assert agent._exec_id == "exec_abc123"

    def test_builds_tool_schemas(self):
        """Tool schemas are built from tool instances."""
        mock_tool1 = MagicMock()
        mock_tool1.to_schema.return_value = {"name": "bash", "description": "run commands", "input_schema": {}}
        mock_tool2 = MagicMock()
        mock_tool2.to_schema.return_value = {"name": "read", "description": "read files", "input_schema": {}}

        agent = _make_agent(task="test", tools=[mock_tool1, mock_tool2])

        assert len(agent.tool_schemas) == 2
        assert agent.tool_schemas[0]["name"] == "bash"
        assert agent.tool_schemas[1]["name"] == "read"


class TestWorkerBuildTaskSection:
    """Agent._build_task_message() task description formatting."""

    def test_task_only(self):
        """Task without plan."""
        agent = _make_agent(task="translate the file")
        section = agent._build_task_message()

        assert "translate the file" in section

    def test_task_with_plan(self):
        """Task with plan includes plan steps."""
        agent = _make_agent(task="deploy app", plan=["build", "test", "deploy"])
        section = agent._build_task_message()

        assert "deploy app" in section
        assert "build" in section
        assert "test" in section
        assert "deploy" in section


class TestWorkerElapsedSeconds:
    """Agent._elapsed_seconds() timing."""

    def test_not_started(self):
        """Returns 0 when not started."""
        agent = _make_agent(task="test")

        assert agent._elapsed_seconds() == 0.0


class TestWorkerLLMError:
    """LLM 错误路径：_call_llm 抛异常时 run() 必须返回 status=error，不得误判为完成。"""

    def _make_agent(self):
        return _make_agent(task="test task")

    def test_llm_error_returns_error_status(self):
        """_call_llm 抛 RuntimeError → run() 返回 status=error，错误文本透传"""
        agent = self._make_agent()

        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_call_llm",
                          side_effect=RuntimeError("LLM 错误 500: overloaded")):
            result = agent.run()

        assert result["status"] == "error"
        assert "500" in (result.get("summary") or result.get("message") or "")
        assert result["iterations"] == 0

    def test_llm_error_stops_immediately(self):
        """错误后不应继续迭代（只调用一次 _call_llm）"""
        agent = self._make_agent()

        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_call_llm",
                          side_effect=RuntimeError("LLM 请求失败: connection refused")) as mock_call:
            result = agent.run()

        assert mock_call.call_count == 1
        assert result["status"] == "error"


def _make_tool_call_response(call_id="toolu_1", name="bash"):
    """构造带 tool_calls 的 LLM 响应 mock。"""
    tc = MagicMock()
    tc.name = name
    tc.id = call_id
    tc.parse_arguments.return_value = {}
    resp = MagicMock()
    resp.get_tool_calls.return_value = [tc]
    resp.content_as_dicts.return_value = [{"type": "tool_use", "id": call_id, "name": name, "input": {}}]
    return resp


def _make_text_response(text):
    """构造纯文本（无 tool_calls）的 LLM 响应 mock。"""
    resp = MagicMock()
    resp.get_tool_calls.return_value = []
    resp.get_text.return_value = text
    resp.content_as_dicts.return_value = [{"type": "text", "text": text}]
    return resp


def _make_tool_result():
    return {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}


class TestWorkerBudgetAwareness:
    """迭代额度感知：预警注入、跳过检查、timeout 兜底。"""

    def _make_agent(self):
        return _make_agent(task="test task")

    def test_warn_injected_once(self):
        """额度用到 80% 时注入一次预警，内容含已用/总额"""
        agent = self._make_agent()
        agent.max_iterations = 10  # warn 阈值 = int(10*0.8) = 8，final 阈值 = 9

        responses = [_make_tool_call_response() for _ in range(9)] + [_make_text_response("done")]
        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_execute_tool", return_value=_make_tool_result()), \
             patch.object(agent, "_call_llm", side_effect=responses):
            agent.run()

        warn_msgs = [m for m in agent.messages if m.get("_meta", {}).get("budget") == "warn"]
        assert len(warn_msgs) == 1
        assert "8/10" in warn_msgs[0]["content"][0]["text"]

    def test_final_notice_skips_check_phase(self):
        """final 预警后模型输出文本 → 跳过检查阶段，直接返回 budget_wrapup"""
        agent = self._make_agent()
        agent.max_iterations = 10  # i=9 触发 final，i=9 模型输出文本

        responses = [_make_tool_call_response() for _ in range(9)] + [_make_text_response("最终总结")]
        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_execute_tool", return_value=_make_tool_result()), \
             patch.object(agent, "_call_llm", side_effect=responses):
            result = agent.run()

        assert result["status"] == "completed"
        assert result["budget_wrapup"] is True
        assert result["summary"] == "最终总结"
        assert result["iterations"] == 10
        # 未注入检查提示
        assert not any(
            isinstance(m.get("content"), str) and "检查阶段" in m["content"]
            for m in agent.messages
        )

    def test_same_iteration_dual_hit_injects_final_only(self):
        """同一轮同时命中 warn 和 final 阈值时只注入 final"""
        agent = self._make_agent()
        agent.max_iterations = 2  # warn 阈值 = int(1.6) = 1，final 阈值 = int(1.9) = 1

        responses = [_make_tool_call_response(), _make_text_response("done")]
        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_execute_tool", return_value=_make_tool_result()), \
             patch.object(agent, "_call_llm", side_effect=responses):
            result = agent.run()

        final_msgs = [m for m in agent.messages if m.get("_meta", {}).get("budget") == "final"]
        warn_msgs = [m for m in agent.messages if m.get("_meta", {}).get("budget") == "warn"]
        assert len(final_msgs) == 1
        assert len(warn_msgs) == 0
        assert result["budget_wrapup"] is True

    def test_timeout_wrapup_success(self):
        """循环耗尽 → 兜底 LLM 调用生成总结，status=timeout 且带 summary/wrapped_up"""
        agent = self._make_agent()
        agent.max_iterations = 3

        responses = [_make_tool_call_response() for _ in range(3)]
        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_execute_tool", return_value=_make_tool_result()), \
             patch.object(agent, "_call_llm", side_effect=responses), \
             patch.object(agent.client, "chat") as mock_chat:
            mock_resp = MagicMock()
            mock_resp.usage = None  # 防 MagicMock 混入 _update_usage
            mock_resp.get_text.return_value = "兜底总结文本"
            mock_chat.return_value = mock_resp
            result = agent.run()

        assert result["status"] == "timeout"
        assert result["summary"] == "兜底总结文本"
        assert result["wrapped_up"] is True
        assert "summary" in result
        # 兜底调用不带 tools，杜绝再次触发工具循环
        assert "tools" not in mock_chat.call_args.kwargs

    def test_timeout_wrapup_failure_falls_back(self):
        """兜底调用失败 → 回退到原始 timeout 文本，不抛异常"""
        agent = self._make_agent()
        agent.max_iterations = 3

        responses = [_make_tool_call_response() for _ in range(3)]
        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_execute_tool", return_value=_make_tool_result()), \
             patch.object(agent, "_call_llm", side_effect=responses), \
             patch.object(agent.client, "chat", side_effect=Exception("boom")):
            result = agent.run()

        assert result["status"] == "timeout"
        assert result["summary"] == "Exceeded max iterations (3)"
        assert "wrapped_up" not in result

    def test_no_notice_below_threshold(self):
        """额度充足时正常走完执行+检查流程，无任何预算消息"""
        agent = self._make_agent()
        agent.max_iterations = 200

        responses = [
            _make_tool_call_response(),
            _make_tool_call_response(),
            _make_text_response("主阶段结束"),
            _make_text_response("检查完成"),
        ]
        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_execute_tool", return_value=_make_tool_result()), \
             patch.object(agent, "_call_llm", side_effect=responses):
            result = agent.run()

        assert result["status"] == "completed"
        assert result["check_iterations"] == 1
        assert "budget_wrapup" not in result
        assert not any(m.get("_meta", {}).get("budget") for m in agent.messages)


class TestWorkerCheckIterations:
    """检查阶段迭代上限：null（不设上限）与数字上限两态"""

    def test_worker_loads_check_iterations_none_from_role(self):
        """worker.json 配置 check_iterations=null → role_cfg 解析为 None（不设上限）"""
        agent = _make_agent(task="test task")
        assert agent.role_cfg.check_phase is True
        assert agent.role_cfg.check_iterations is None

    def test_no_cap_when_check_iterations_none(self):
        """check_iterations=None 时检查阶段不受轮次上限约束，直至正常收尾"""
        agent = _make_agent(task="test task")
        agent.role_cfg.check_iterations = None
        agent.max_iterations = 200

        # 主阶段 1 轮 → 进入检查 → 检查阶段连续 25 轮工具调用 → 最终文本收尾
        responses = (
            [_make_tool_call_response(), _make_text_response("主阶段结束")]
            + [_make_tool_call_response() for _ in range(25)]
            + [_make_text_response("检查完成")]
        )
        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_execute_tool", return_value=_make_tool_result()), \
             patch.object(agent, "_call_llm", side_effect=responses):
            result = agent.run()

        assert result["status"] == "completed"
        # 25 轮检查工具 + 1 轮收尾文本 = 26，未被上限截断
        assert result["check_iterations"] == 26

    def test_numeric_cap_still_enforced(self):
        """check_iterations 为数字时仍按上限强制收尾"""
        agent = _make_agent(task="test task")
        agent.role_cfg.check_iterations = 5
        agent.max_iterations = 200

        # 主阶段 1 轮 → 进入检查（max(1,5)=5）→ 检查阶段 10 轮工具调用（无收尾文本）
        responses = (
            [_make_tool_call_response(), _make_text_response("主阶段结束")]
            + [_make_tool_call_response() for _ in range(10)]
        )
        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_execute_tool", return_value=_make_tool_result()), \
             patch.object(agent, "_call_llm", side_effect=responses):
            result = agent.run()

        # 检查阶段第 6 轮（check_iters=6 > 5）触发上限强制收尾
        assert result["status"] == "completed"
        assert result["check_iterations"] == 6


class TestWorkerEventCallbacks:
    """worker 事件回调：_on_text/_on_thinking/_on_tool_call/_on_tool_result 逐事件触发"""

    def _make_tool(self, name="bash"):
        """构造最小 mock 工具：coerce_input 透传入参，execute 返回固定结果"""
        from core.tools.base import ToolResult
        tool = MagicMock()
        tool.name = name
        tool.coerce_input.side_effect = lambda x: x
        tool.execute.return_value = ToolResult("ok", completed=True, meta={})
        return tool

    def test_text_and_thinking_deltas(self):
        """流式 delta 触发 _on_text/_on_thinking，顺序与内容正确"""
        agent = _make_agent(task="test task")
        texts = []
        thinkings = []
        agent._on_text = texts.append
        agent._on_thinking = thinkings.append

        mock_resp = MagicMock()
        mock_resp.get_tool_calls.return_value = []
        mock_resp.get_text.return_value = "最终回答"
        mock_resp.content_as_dicts.return_value = [{"type": "text", "text": "最终回答"}]
        mock_resp.usage = None

        round_count = [0]

        def fake_chat_stream(messages, system, tools, on_text, on_thinking, **kwargs):
            # 仅第一轮模拟流式 delta（worker 有检查阶段会再调一次，但回调只盯第一轮）
            round_count[0] += 1
            if round_count[0] == 1:
                on_thinking("我先思考")
                on_text("最终")
                on_text("回答")
            return mock_resp

        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent.client, "chat_stream", side_effect=fake_chat_stream):
            result = agent.run()

        assert result["status"] == "completed"
        assert "".join(thinkings) == "我先思考"
        assert "".join(texts) == "最终回答"

    def test_tool_call_then_result_order(self):
        """tool_call 在 execute 前触发、tool_result 在 execute 后触发，携带正确 payload"""
        tool = self._make_tool()
        agent = _make_agent(task="test task", tools=[tool])
        calls = []
        results = []
        agent._on_tool_call = lambda name, inp, tid: calls.append((name, inp, tid))
        agent._on_tool_result = lambda name, content, is_error, tid: results.append((name, content, is_error))

        resp_tool = MagicMock()
        tc = MagicMock()
        tc.name = "bash"
        tc.id = "toolu_1"
        tc.parse_arguments.return_value = {"command": "echo hi"}
        resp_tool.get_tool_calls.return_value = [tc]
        resp_tool.content_as_dicts.return_value = [
            {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "echo hi"}}
        ]
        resp_tool.usage = None

        resp_text = _make_text_response("done")

        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_call_llm", side_effect=[resp_tool, resp_text, resp_text]):
            result = agent.run()

        assert result["status"] == "completed"
        assert calls, "应触发 _on_tool_call"
        assert calls[0][0] == "bash"
        assert calls[0][1] == {"command": "echo hi"}
        assert results, "应触发 _on_tool_result"
        assert results[0][0] == "bash"
        assert "ok" in results[0][1]
        assert results[0][2] is False  # 非错误

    def test_tool_result_preview_truncated_500(self):
        """大工具输出 preview 截断到 500 字符并标注总量"""
        tool = self._make_tool()
        from core.tools.base import ToolResult
        big_output = "x" * 1000
        tool.execute.return_value = ToolResult(big_output, completed=True, meta={})
        agent = _make_agent(task="test task", tools=[tool])
        results = []
        agent._on_tool_result = lambda name, content, is_error, tid: results.append(content)

        resp_tool = MagicMock()
        tc = MagicMock()
        tc.name = "bash"
        tc.id = "toolu_2"
        tc.parse_arguments.return_value = {}
        resp_tool.get_tool_calls.return_value = [tc]
        resp_tool.content_as_dicts.return_value = [
            {"type": "tool_use", "id": "toolu_2", "name": "bash", "input": {}}
        ]
        resp_tool.usage = None

        resp_text = _make_text_response("done")

        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_call_llm", side_effect=[resp_tool, resp_text, resp_text]):
            agent.run()

        assert results and len(results[0]) == 500 + len("\n... (1000 chars total)")
        assert "1000 chars total" in results[0]
        assert results[0].count("x") == 500
