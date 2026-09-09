"""Tests for core/sub_agent.py — SubAgent construction and pure logic."""

from unittest.mock import MagicMock, patch

import pytest


class TestSubAgentConstruction:
    """SubAgent initialization."""

    def test_basic_construction(self):
        """SubAgent can be created with minimal params."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_config.system.max_iterations = 200
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(task="test task")

            assert agent.task == "test task"
            assert agent.plan is None
            assert agent.max_iterations == 200  # default
            assert agent.max_consecutive_failures == 5  # default
            assert agent.messages == []

    def test_custom_parameters(self):
        """SubAgent accepts custom parameters."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_config.system.max_iterations = 200
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(
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
            assert agent.cwd == "/tmp"

    def test_stop_check_stored(self):
        """stop_check callable is stored."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            stop_fn = lambda: False
            agent = SubAgent(task="test", stop_check=stop_fn)

            assert agent.stop_check is stop_fn

    def test_exec_id_stored(self):
        """exec_id is stored for session tracking."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(task="test", exec_id="exec_abc123")

            assert agent._exec_id == "exec_abc123"

    def test_builds_tool_schemas(self):
        """Tool schemas are built from tool instances."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config

            # Create mock tools with to_schema
            mock_tool1 = MagicMock()
            mock_tool1.to_schema.return_value = {"name": "bash", "description": "run commands", "input_schema": {}}
            mock_tool2 = MagicMock()
            mock_tool2.to_schema.return_value = {"name": "read", "description": "read files", "input_schema": {}}
            mock_tools.return_value = [mock_tool1, mock_tool2]

            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(task="test")

            assert len(agent.tool_schemas) == 2
            assert agent.tool_schemas[0]["name"] == "bash"
            assert agent.tool_schemas[1]["name"] == "read"


class TestSubAgentBuildTaskSection:
    """SubAgent._build_task_section() task description formatting."""

    def test_task_only(self):
        """Task without plan."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(task="translate the file")
            section = agent._build_task_message()

            assert "translate the file" in section

    def test_task_with_plan(self):
        """Task with plan includes plan steps."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(task="deploy app", plan=["build", "test", "deploy"])
            section = agent._build_task_message()

            assert "deploy app" in section
            assert "build" in section
            assert "test" in section
            assert "deploy" in section


class TestSubAgentElapsedSeconds:
    """SubAgent._elapsed_seconds() timing."""

    def test_not_started(self):
        """Returns 0 when not started."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(task="test")

            assert agent._elapsed_seconds() == 0.0


class TestSubAgentLLMError:
    """LLM 错误路径：_call_llm 抛异常时 run() 必须返回 status=error，不得误判为完成。"""

    def _make_agent(self):
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_config.system.max_iterations = 200
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            return SubAgent(task="test task")

    def test_llm_error_returns_error_status(self):
        """_call_llm 抛 RuntimeError → run() 返回 status=error，错误文本透传"""
        agent = self._make_agent()

        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_call_llm",
                          side_effect=RuntimeError("LLM 错误 500: overloaded")):
            result = agent.run()

        assert result["status"] == "error"
        assert "500" in result["message"]
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


class TestSubAgentBudgetAwareness:
    """迭代额度感知：预警注入、跳过检查、timeout 兜底。"""

    def _make_agent(self):
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_config.system.max_iterations = 200
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            return SubAgent(task="test task")

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
