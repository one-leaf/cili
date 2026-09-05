"""Tests for core/tools/root/subagent_tool.py."""

import json
from unittest.mock import MagicMock, patch

import pytest

from core.tools.root.subagent_tool import SubAgentTool
from core.tools.shared.base import ToolResult


@pytest.fixture
def subagent_tool(test_workspace):
    tool = SubAgentTool(cwd=test_workspace, workspace_uuid="test-workspace")
    tool.session_manager = MagicMock()
    tool.session_manager._generate_exec_id.return_value = "exec_123"
    return tool


class TestSubAgentToolExecute:
    """SubAgentTool.execute() with mocked SubAgent."""

    def test_empty_task_error(self, subagent_tool):
        """Empty task returns error."""
        result = subagent_tool.execute(task="")
        assert result.error is True
        assert "required" in result.output or "empty" in result.output

    def test_whitespace_only_task_error(self, subagent_tool):
        """Whitespace-only task returns error."""
        result = subagent_tool.execute(task="   \n  ")
        assert result.error is True

    def test_returns_result_synchronously(self, subagent_tool):
        """Returns complete result synchronously (no placeholder)."""
        mock_subagent = MagicMock()
        mock_subagent.run.return_value = {
            "status": "completed",
            "summary": "Task completed successfully",
            "iterations": 10,
            "usage": {},
        }
        mock_subagent.close.return_value = None
        mock_subagent.messages = []

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            result = subagent_tool.execute(task="test task")

        # Should return completed result directly (no placeholder)
        assert result.completed is True
        assert result.meta.get("exec_id") == "exec_123"
        # Result should be JSON with the subagent result
        result_data = json.loads(result.output)
        assert result_data["status"] == "completed"
        assert result_data["summary"] == "Task completed successfully"

        # Pending entry should be cleaned up
        entry = subagent_tool.get_pending_subagent("exec_123")
        assert entry is None

    def test_subagent_failure_returned_in_result(self, subagent_tool):
        """SubAgent error is returned in the ToolResult."""
        mock_subagent = MagicMock()
        mock_subagent.run.side_effect = Exception("SubAgent crashed")
        mock_subagent.close.return_value = None
        mock_subagent.messages = []

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            result = subagent_tool.execute(task="test")

        # Should return error result directly
        assert result.completed is True
        result_data = json.loads(result.output)
        assert result_data["status"] == "error"
        assert "crashed" in result_data["summary"].lower() or "SubAgent crashed" in result_data["summary"]

        # Pending entry should be cleaned up
        entry = subagent_tool.get_pending_subagent("exec_123")
        assert entry is None

    def test_closes_subagent_after_run(self, subagent_tool):
        """SubAgent.close() is called after run."""
        mock_subagent = MagicMock()
        mock_subagent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_subagent.close.return_value = None
        mock_subagent.messages = []

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            subagent_tool.execute(task="test")

        # execute() waits synchronously, no need to wait for background thread
        mock_subagent.close.assert_called_once()

    def test_closes_subagent_on_error(self, subagent_tool):
        """SubAgent.close() is called even when run raises."""
        mock_subagent = MagicMock()
        mock_subagent.run.side_effect = Exception("error")
        mock_subagent.close.return_value = None
        mock_subagent.messages = []

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            subagent_tool.execute(task="test")

        # execute() waits synchronously, no need to wait for background thread
        mock_subagent.close.assert_called_once()

    def test_forwards_usage_to_session(self, subagent_tool):
        """SubAgent usage is forwarded to session manager."""
        mock_subagent = MagicMock()
        mock_subagent.run.return_value = {
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
        mock_subagent.close.return_value = None
        mock_subagent.messages = []

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            subagent_tool.execute(task="test")

        # execute() waits synchronously, usage is forwarded before return
        subagent_tool.session_manager.update_usage.assert_called_once()
        call_kwargs = subagent_tool.session_manager.update_usage.call_args.kwargs
        assert call_kwargs["input_tokens"] == 500
        assert call_kwargs["output_tokens"] == 200

    def test_fires_start_callback(self, subagent_tool):
        """on_subagent_start callback is fired before run."""
        subagent_tool.on_subagent_start = MagicMock()

        mock_subagent = MagicMock()
        mock_subagent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_subagent.close.return_value = None

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            subagent_tool.execute(task="test task description")

        subagent_tool.on_subagent_start.assert_called_once()
        call_args = subagent_tool.on_subagent_start.call_args.args
        assert call_args[0] == "exec_123"  # exec_id
        assert "test task" in call_args[1]  # task summary (truncated)

    def test_plan_passed_to_subagent(self, subagent_tool):
        """Plan parameter is forwarded to SubAgent."""
        mock_subagent = MagicMock()
        mock_subagent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_subagent.close.return_value = None

        plan = ["step 1", "step 2", "step 3"]
        with patch("core.sub_agent.SubAgent", return_value=mock_subagent) as MockSubAgent:
            subagent_tool.execute(task="test", plan=plan)

        # Verify SubAgent was created with plan
        call_kwargs = MockSubAgent.call_args.kwargs
        assert call_kwargs["plan"] == plan

    def test_saves_session_after_run(self, subagent_tool):
        """Session is saved after SubAgent completes."""
        mock_subagent = MagicMock()
        mock_subagent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_subagent.close.return_value = None
        mock_subagent.messages = []

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            subagent_tool.execute(task="test")

        # execute() waits synchronously, session is saved before return
        subagent_tool.session_manager.save.assert_called()


class TestSubAgentToolParameters:
    """SubAgentTool parameter schema."""

    def test_required_task(self):
        tool = SubAgentTool()
        # task is no longer required (run_in_background/read_task/kill_task/list_tasks are alternatives)
        assert "task" in tool.parameters["properties"]

    def test_optional_parameters(self):
        tool = SubAgentTool()
        props = tool.parameters["properties"]
        assert "plan" in props
        assert "run_in_background" in props
        assert "read_task" in props
        assert "kill_task" in props
        assert "list_tasks" in props
