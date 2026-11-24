"""Tests for core/tools/agent_tool.py."""

import json
from unittest.mock import MagicMock, patch

from core.tools.agent import AgentTool


class TestAgentToolExecute:
    """AgentTool.execute() with mocked SessionRunner."""

    def test_empty_task_error(self, agent_tool):
        """Empty task returns error."""
        result = agent_tool.execute(task="")
        assert result.error is True
        assert "required" in result.output or "empty" in result.output

    def test_whitespace_only_task_error(self, agent_tool):
        """Whitespace-only task returns error."""
        result = agent_tool.execute(task="   \n  ")
        assert result.error is True

    def test_returns_result_synchronously(self, agent_tool):
        """Returns complete result synchronously (no placeholder)."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "Task completed successfully",
            "iterations": 10,
            "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            result = agent_tool.execute(task="test task")

        # Should return completed result directly (no placeholder)
        assert result.completed is True
        assert result.meta.get("exec_id") == "exec_123"
        # Result should be JSON with the agent result
        result_data = json.loads(result.output)
        assert result_data["status"] == "completed"
        assert result_data["summary"] == "Task completed successfully"

    def test_agent_failure_returned_in_result(self, agent_tool):
        """SessionRunner error is returned in the ToolResult."""
        mock_agent = MagicMock()
        mock_agent.run.side_effect = Exception("SessionRunner crashed")
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            result = agent_tool.execute(task="test")

        # Should return error result directly
        assert result.completed is True
        result_data = json.loads(result.output)
        assert result_data["status"] == "error"
        assert "crashed" in result_data["summary"].lower() or "SessionRunner crashed" in result_data["summary"]

    def test_closes_agent_after_run(self, agent_tool):
        """SessionRunner.close() is called after run."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            agent_tool.execute(task="test")

        # execute() waits synchronously, no need to wait for background thread
        mock_agent.close.assert_called_once()

    def test_closes_agent_on_error(self, agent_tool):
        """SessionRunner.close() is called even when run raises."""
        mock_agent = MagicMock()
        mock_agent.run.side_effect = Exception("error")
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            agent_tool.execute(task="test")

        # execute() waits synchronously, no need to wait for background thread
        mock_agent.close.assert_called_once()

    def test_does_not_write_session_usage(self, agent_tool):
        """用量归集不在工具层：agent_tool 不得写 session.usage（读取时聚合）。"""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed", "summary": "done", "iterations": 1,
            "usage": {"input_tokens": 500, "output_tokens": 200},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            agent_tool.execute(task="test")

        agent_tool.session.update_usage.assert_not_called()

    def test_fires_start_callback(self, agent_tool):
        """on_session_start callback is fired before run."""
        agent_tool.on_session_start = MagicMock()

        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_agent.close.return_value = None

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            agent_tool.execute(task="test task description")

        agent_tool.on_session_start.assert_called_once()
        call_args = agent_tool.on_session_start.call_args.args
        assert call_args[0] == "exec_123"  # exec_id
        assert "test task" in call_args[1]  # task summary (truncated)

    def test_plan_passed_to_agent(self, agent_tool):
        """Plan parameter is forwarded to SessionRunner."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_agent.close.return_value = None

        plan = ["step 1", "step 2", "step 3"]
        with patch("core.session_runner.SessionRunner", return_value=mock_agent) as MockSessionRunner:
            agent_tool.execute(task="test", plan=plan)

        # Verify SessionRunner was created with plan
        call_kwargs = MockSessionRunner.call_args.kwargs
        assert call_kwargs["plan"] == plan

    def test_saves_session_after_run(self, agent_tool):
        """Session is saved after SessionRunner completes."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            agent_tool.execute(task="test")

        # execute() waits synchronously, session is saved before return
        agent_tool.session.save.assert_called()


class TestAgentToolParameters:
    """AgentTool parameter schema."""

    def test_required_task(self):
        tool = AgentTool()
        # task is no longer required (action='status'/'stop'/'list' are alternatives to 'spawn')
        assert "task" in tool.parameters["properties"]

    def test_optional_parameters(self):
        tool = AgentTool()
        props = tool.parameters["properties"]
        assert "plan" in props
        assert "run_in_background" in props
        assert "action" in props
        assert "spawn" in props["action"]["enum"]
        assert "status" in props["action"]["enum"]
        assert "stop" in props["action"]["enum"]
        assert "list" in props["action"]["enum"]
        assert "agent_id" in props
        assert "subagent_type" in props


class TestDelegationDepthLimit:
    """委派深度限制：master(0) 可委派 worker/lite；depth1 子代理仅可委派 lite；depth≥2 禁止再委派。"""

    def _mock_agent_run(self):
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed", "summary": "ok", "iterations": 1, "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []
        return mock_agent

    def test_depth_1_worker_blocked(self, agent_tool):
        """depth=1 的子代理委派 worker → 报错，不构造子 SessionRunner（depth1 仅可委派 lite）。"""
        agent_tool.delegation_depth = 1
        with patch("core.session_runner.SessionRunner") as MockSessionRunner:
            result = agent_tool.execute(task="delegate me")
        assert result.error is True
        assert "lite" in result.output.lower()
        MockSessionRunner.assert_not_called()

    def test_depth_1_lite_allowed(self, agent_tool):
        """depth=1 的 worker 委派 lite → 允许，子 SessionRunner 收到 depth=2 且 role=lite。"""
        agent_tool.delegation_depth = 1
        mock_agent = self._mock_agent_run()
        with patch("core.session_runner.SessionRunner", return_value=mock_agent) as MockSessionRunner:
            result = agent_tool.execute(task="delegate me", subagent_type="lite")
        assert result.error is not True
        assert MockSessionRunner.call_args.kwargs["delegation_depth"] == 2
        assert MockSessionRunner.call_args.kwargs["role"] == "lite"

    def test_depth_2_blocked(self, agent_tool):
        """depth=2 的子代理（lite）再委派 → 报错，不构造子 SessionRunner。"""
        agent_tool.delegation_depth = 2
        with patch("core.session_runner.SessionRunner") as MockSessionRunner:
            result = agent_tool.execute(task="delegate me", subagent_type="lite")
        assert result.error is True
        assert "depth" in result.output.lower()
        MockSessionRunner.assert_not_called()

    def test_depth_2_background_blocked(self, agent_tool):
        """depth=2 background 委派同样受限制。"""
        agent_tool.delegation_depth = 2
        with patch("core.session_runner.SessionRunner") as MockSessionRunner:
            result = agent_tool.execute(task="delegate me", run_in_background=True, subagent_type="lite")
        assert result.error is True
        MockSessionRunner.assert_not_called()

    def test_depth_1_background_lite_allowed(self, agent_tool):
        """depth=1 background 委派 lite → 允许，子 SessionRunner 收到 depth=2。"""
        agent_tool.delegation_depth = 1
        mock_agent = self._mock_agent_run()
        with patch("core.session_runner.SessionRunner", return_value=mock_agent) as MockSessionRunner:
            result = agent_tool.execute(task="delegate me", run_in_background=True, subagent_type="lite")
        assert result.error is not True
        assert MockSessionRunner.call_args.kwargs["delegation_depth"] == 2

    def test_depth_0_forwards_depth_1(self, agent_tool):
        """master（depth=0）正常委派 worker，子 SessionRunner 收到 depth=1。"""
        mock_agent = self._mock_agent_run()
        with patch("core.session_runner.SessionRunner", return_value=mock_agent) as MockSessionRunner:
            result = agent_tool.execute(task="test")
        assert result.error is not True
        assert MockSessionRunner.call_args.kwargs["delegation_depth"] == 1

    def test_depth_0_lite_allowed(self, agent_tool):
        """master（depth=0）可委派 lite，子 SessionRunner 收到 depth=1 且 role=lite。"""
        mock_agent = self._mock_agent_run()
        with patch("core.session_runner.SessionRunner", return_value=mock_agent) as MockSessionRunner:
            result = agent_tool.execute(task="test", subagent_type="lite")
        assert result.error is not True
        assert MockSessionRunner.call_args.kwargs["delegation_depth"] == 1
        assert MockSessionRunner.call_args.kwargs["role"] == "lite"


class TestAgentToolHardSwitch:
    """硬切换：注册表/白名单不再接受 session 工具名。"""

    def test_registry_has_agent_not_session(self):
        from core.tools.registry import TOOL_REGISTRY

        assert "agent" in TOOL_REGISTRY
        assert "session" not in TOOL_REGISTRY

    def test_role_whitelists(self):
        from core.role_config import load_role

        assert "agent" in load_role("master").tools
        assert "agent" in load_role("worker").tools
        assert "agent" not in load_role("lite").tools
        for role in ("master", "worker", "lite"):
            assert "handoff" not in load_role(role).tools

    def test_status_requires_agent_id(self):
        result = AgentTool().execute(action="status")
        assert result.error is True
        assert "agent_id" in result.output

    def test_stop_requires_agent_id(self):
        result = AgentTool().execute(action="stop")
        assert result.error is True
        assert "agent_id" in result.output


def test_invalid_action_rejected():
    """旧 action 别名与未知 action 都应直接报错，不能落入 spawn 分支。"""
    tool = AgentTool()
    for action in ("start", "read", "kill", "bogus"):
        result = tool.execute(action=action)
        assert result.error is True
        assert "invalid action" in result.output


def test_continuation_result_meta_and_chain(agent_tool):
    """worker 迭代耗尽 -> 同一 exec_id 的后台 continuation，不提前 complete。"""
    mock_agent = MagicMock()
    mock_agent.run.return_value = {
        "status": "handoff",
        "summary": "exhausted",
        "iterations": 200,
        "usage": {},
        "continuation": {
            "exec_id": "exec_123", "epoch": 1, "exit_reason": "max_iterations",
            "truncated": True, "document": "DOC", "next_steps": "continue",
            "summary": "exhausted", "iterations": 200, "max_iterations": 200,
        },
    }
    mock_agent.close.return_value = None
    mock_agent.messages = []
    mock_agent._started_at = None
    mock_agent.max_iterations = 200

    with patch("core.session_runner.SessionRunner", return_value=mock_agent),          patch.object(agent_tool, "_start_continuation_chain") as chain:
        result = agent_tool.execute(task="test task")

    assert result.meta["status"] == "running"
    assert result.meta["continuation"] is True
    assert result.meta["epoch"] == 1
    assert result.meta["max_epochs"] == 3
    chain.assert_called_once()

