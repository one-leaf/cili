"""Tests for core/tools/agent_tool.py."""

import json
from unittest.mock import MagicMock, patch

import pytest

from core.tools.agent_tool import AgentTool
from core.tools.base import ToolResult


@pytest.fixture
def agent_tool(test_workspace):
    tool = AgentTool(cwd=test_workspace, workspace_uuid="test-workspace")
    tool.session_manager = MagicMock()
    tool.session_manager._generate_exec_id.return_value = "exec_123"
    return tool


class TestAgentToolExecute:
    """AgentTool.execute() with mocked Agent."""

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

        with patch("core.agent.Agent", return_value=mock_agent):
            result = agent_tool.execute(task="test task")

        # Should return completed result directly (no placeholder)
        assert result.completed is True
        assert result.meta.get("exec_id") == "exec_123"
        # Result should be JSON with the agent result
        result_data = json.loads(result.output)
        assert result_data["status"] == "completed"
        assert result_data["summary"] == "Task completed successfully"

        # Pending entry should be cleaned up
        entry = agent_tool.get_pending_agent("exec_123")
        assert entry is None

    def test_agent_failure_returned_in_result(self, agent_tool):
        """Agent error is returned in the ToolResult."""
        mock_agent = MagicMock()
        mock_agent.run.side_effect = Exception("Agent crashed")
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.agent.Agent", return_value=mock_agent):
            result = agent_tool.execute(task="test")

        # Should return error result directly
        assert result.completed is True
        result_data = json.loads(result.output)
        assert result_data["status"] == "error"
        assert "crashed" in result_data["summary"].lower() or "Agent crashed" in result_data["summary"]

        # Pending entry should be cleaned up
        entry = agent_tool.get_pending_agent("exec_123")
        assert entry is None

    def test_closes_agent_after_run(self, agent_tool):
        """Agent.close() is called after run."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.agent.Agent", return_value=mock_agent):
            agent_tool.execute(task="test")

        # execute() waits synchronously, no need to wait for background thread
        mock_agent.close.assert_called_once()

    def test_closes_agent_on_error(self, agent_tool):
        """Agent.close() is called even when run raises."""
        mock_agent = MagicMock()
        mock_agent.run.side_effect = Exception("error")
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.agent.Agent", return_value=mock_agent):
            agent_tool.execute(task="test")

        # execute() waits synchronously, no need to wait for background thread
        mock_agent.close.assert_called_once()

    def test_forwards_usage_to_session(self, agent_tool):
        """Agent usage is forwarded to session manager."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 5,
            "usage": {
                "input_tokens": 500,
                "output_tokens": 200,
                "cache_read_tokens": 50,
                "cache_creation_tokens": 10,
            },
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.agent.Agent", return_value=mock_agent):
            agent_tool.execute(task="test")

        # execute() waits synchronously, usage is forwarded before return
        agent_tool.session_manager.update_usage.assert_called_once()
        call_kwargs = agent_tool.session_manager.update_usage.call_args.kwargs
        assert call_kwargs["input_tokens"] == 500
        assert call_kwargs["output_tokens"] == 200

    def test_fires_start_callback(self, agent_tool):
        """on_agent_start callback is fired before run."""
        agent_tool.on_agent_start = MagicMock()

        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_agent.close.return_value = None

        with patch("core.agent.Agent", return_value=mock_agent):
            agent_tool.execute(task="test task description")

        agent_tool.on_agent_start.assert_called_once()
        call_args = agent_tool.on_agent_start.call_args.args
        assert call_args[0] == "exec_123"  # exec_id
        assert "test task" in call_args[1]  # task summary (truncated)

    def test_plan_passed_to_agent(self, agent_tool):
        """Plan parameter is forwarded to Agent."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_agent.close.return_value = None

        plan = ["step 1", "step 2", "step 3"]
        with patch("core.agent.Agent", return_value=mock_agent) as MockAgent:
            agent_tool.execute(task="test", plan=plan)

        # Verify Agent was created with plan
        call_kwargs = MockAgent.call_args.kwargs
        assert call_kwargs["plan"] == plan

    def test_saves_session_after_run(self, agent_tool):
        """Session is saved after Agent completes."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.agent.Agent", return_value=mock_agent):
            agent_tool.execute(task="test")

        # execute() waits synchronously, session is saved before return
        agent_tool.session_manager.save.assert_called()


class TestAgentToolParameters:
    """AgentTool parameter schema."""

    def test_required_task(self):
        tool = AgentTool()
        # task is no longer required (run_in_background/read_task/kill_task/list_tasks are alternatives)
        assert "task" in tool.parameters["properties"]

    def test_optional_parameters(self):
        tool = AgentTool()
        props = tool.parameters["properties"]
        assert "plan" in props
        assert "run_in_background" in props
        assert "read_task" in props
        assert "kill_task" in props
        assert "list_tasks" in props


class TestDelegationDepthLimit:
    """委派深度限制：只有 master(0) 能委派 worker/lite(1)；depth≥1 的子代理禁止再委派。"""

    def test_depth_1_worker_returns_error(self, agent_tool):
        """depth=1 的 worker 再调用 agent 工具 → 直接报错，不构造子 Agent。"""
        agent_tool.delegation_depth = 1
        with patch("core.agent.Agent") as MockAgent:
            result = agent_tool.execute(task="delegate me")
        assert result.error is True
        assert "depth" in result.output.lower()
        MockAgent.assert_not_called()

    def test_depth_1_lite_also_blocked(self, agent_tool):
        """depth=1 的 agent 委派 lite 同样报错（只有 master 能委派）。"""
        agent_tool.delegation_depth = 1
        with patch("core.agent.Agent") as MockAgent:
            result = agent_tool.execute(task="delegate me", agent_type="lite")
        assert result.error is True
        MockAgent.assert_not_called()

    def test_depth_1_background_also_blocked(self, agent_tool):
        """background 委派同样受 depth 限制。"""
        agent_tool.delegation_depth = 1
        with patch("core.agent.Agent") as MockAgent:
            result = agent_tool.execute(task="delegate me", run_in_background=True)
        assert result.error is True
        MockAgent.assert_not_called()

    def test_depth_0_forwards_depth_1(self, agent_tool):
        """master（depth=0）正常委派 worker，子 Agent 收到 depth=1。"""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed", "summary": "ok", "iterations": 1, "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []
        with patch("core.agent.Agent", return_value=mock_agent) as MockAgent:
            result = agent_tool.execute(task="test")
        assert result.error is not True
        assert MockAgent.call_args.kwargs["delegation_depth"] == 1

    def test_depth_0_lite_allowed(self, agent_tool):
        """master（depth=0）可委派 lite，子 Agent 收到 depth=1 且 role=lite。"""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed", "summary": "ok", "iterations": 1, "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []
        with patch("core.agent.Agent", return_value=mock_agent) as MockAgent:
            result = agent_tool.execute(task="test", agent_type="lite")
        assert result.error is not True
        assert MockAgent.call_args.kwargs["delegation_depth"] == 1
        assert MockAgent.call_args.kwargs["role"] == "lite"


class TestBackgroundAgentConcurrency:
    """后台子代理并发上限（config.system.max_concurrent_agents）。"""

    @pytest.fixture(autouse=True)
    def _clean_active(self, monkeypatch):
        """每个用例隔离全局活跃列表。"""
        from core.tools import base as base_mod
        monkeypatch.setattr(base_mod, "_active_background_agents", [])
        yield
        with base_mod._background_agents_cond:
            base_mod._active_background_agents.clear()
            base_mod._background_agents_cond.notify_all()

    def test_acquire_below_limit(self, agent_tool):
        """低于上限立即获得槽位并加入活跃列表。"""
        from core.tools import base as base_mod
        agent = MagicMock()
        err = agent_tool._acquire_background_agent_slot(agent)
        assert err is None
        assert agent in base_mod._active_background_agents

    def test_blocks_until_slot_freed(self, agent_tool):
        """达到上限时阻塞，释放后获得槽位。"""
        from core.tools import base as base_mod
        holder = MagicMock()
        base_mod._active_background_agents.append(holder)
        config = MagicMock()
        config.system.max_concurrent_agents = 1
        agent_tool.config = config

        results = {}

        def try_acquire():
            results["err"] = agent_tool._acquire_background_agent_slot(MagicMock())

        import threading
        import time
        t = threading.Thread(target=try_acquire, daemon=True)
        t.start()
        time.sleep(0.3)
        assert t.is_alive(), "达到上限时应阻塞等待"

        with base_mod._background_agents_cond:
            base_mod._active_background_agents.remove(holder)
            base_mod._background_agents_cond.notify_all()
        t.join(timeout=5)
        assert not t.is_alive()
        assert results["err"] is None
        assert len(base_mod._active_background_agents) == 1

    def test_stop_check_returns_error(self, agent_tool):
        """任务停止时立即返回错误，不阻塞。"""
        from core.tools import base as base_mod
        base_mod._active_background_agents.append(MagicMock())
        config = MagicMock()
        config.system.max_concurrent_agents = 1
        agent_tool.config = config
        agent_tool.stop_check = lambda: True

        err = agent_tool._acquire_background_agent_slot(MagicMock())
        assert err is not None
        assert err.error is True
        assert "上限" in err.output

    def test_timeout_returns_error(self, agent_tool, monkeypatch):
        """等待超时返回错误。"""
        from core.tools import base as base_mod
        base_mod._active_background_agents.append(MagicMock())
        config = MagicMock()
        config.system.max_concurrent_agents = 1
        agent_tool.config = config
        agent_tool.stop_check = None

        counter = {"n": 0}

        def fake_time():
            counter["n"] += 1
            return 99999 if counter["n"] > 1 else 0  # deadline=3600，随后时间越过 deadline

        monkeypatch.setattr(base_mod.time, "time", fake_time)
        err = agent_tool._acquire_background_agent_slot(MagicMock())
        assert err is not None
        assert err.error is True
        assert "超时" in err.output
