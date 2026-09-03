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

    def test_max_iterations_capped_at_100(self, subagent_tool):
        """max_iterations is capped at 100."""
        mock_subagent = MagicMock()
        mock_subagent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 5,
            "usage": {},
        }
        mock_subagent.close.return_value = None

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            result = subagent_tool.execute(task="test", max_iterations=200)

        # Should not raise; max_iterations capped internally
        assert result.error is False

    def test_max_iterations_minimum_1(self, subagent_tool):
        """max_iterations is at least 1."""
        mock_subagent = MagicMock()
        mock_subagent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_subagent.close.return_value = None

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            result = subagent_tool.execute(task="test", max_iterations=0)

        assert result.error is False

    def test_returns_placeholder_and_starts_thread(self, subagent_tool):
        """Returns placeholder ToolResult and starts background thread."""
        mock_subagent = MagicMock()
        mock_subagent.run.return_value = {
            "status": "completed",
            "summary": "Task completed successfully",
            "iterations": 10,
            "usage": {},
        }
        mock_subagent.close.return_value = None

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            result = subagent_tool.execute(task="test task")

        # Should return placeholder immediately
        assert result.completed is False
        assert "执行中" in result.output
        # exec_id should be in meta
        assert result.meta.get("exec_id") == "exec_123"

        # Wait for background thread to complete
        entry = subagent_tool.get_pending_subagent("exec_123")
        assert entry is not None
        entry["event"].wait(timeout=5)

        # Result should be stored in entry
        assert entry["result"] is not None
        assert entry["result"]["status"] == "completed"
        assert entry["result"]["summary"] == "Task completed successfully"

        # Cleanup
        subagent_tool.remove_pending_subagent("exec_123")

    def test_subagent_failure_stored_in_entry(self, subagent_tool):
        """SubAgent error is stored in pending entry."""
        mock_subagent = MagicMock()
        mock_subagent.run.side_effect = Exception("SubAgent crashed")
        mock_subagent.close.return_value = None

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            result = subagent_tool.execute(task="test")

        # Should return placeholder immediately (not error)
        assert result.completed is False

        # Wait for background thread to complete
        entry = subagent_tool.get_pending_subagent("exec_123")
        entry["event"].wait(timeout=5)

        # Error result should be stored in entry
        assert entry["result"] is not None
        assert entry["result"]["status"] == "error"
        assert "crashed" in entry["result"]["summary"].lower() or "SubAgent crashed" in entry["result"]["summary"]

        # Cleanup
        subagent_tool.remove_pending_subagent("exec_123")

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

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            subagent_tool.execute(task="test")

        # Wait for background thread
        entry = subagent_tool.get_pending_subagent("exec_123")
        entry["event"].wait(timeout=5)

        mock_subagent.close.assert_called_once()
        subagent_tool.remove_pending_subagent("exec_123")

    def test_closes_subagent_on_error(self, subagent_tool):
        """SubAgent.close() is called even when run raises."""
        mock_subagent = MagicMock()
        mock_subagent.run.side_effect = Exception("error")
        mock_subagent.close.return_value = None

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            subagent_tool.execute(task="test")

        # Wait for background thread
        entry = subagent_tool.get_pending_subagent("exec_123")
        entry["event"].wait(timeout=5)

        mock_subagent.close.assert_called_once()
        subagent_tool.remove_pending_subagent("exec_123")

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

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            subagent_tool.execute(task="test")

        # Wait for background thread
        entry = subagent_tool.get_pending_subagent("exec_123")
        entry["event"].wait(timeout=5)

        subagent_tool.session_manager.update_usage.assert_called_once()
        call_kwargs = subagent_tool.session_manager.update_usage.call_args.kwargs
        assert call_kwargs["input_tokens"] == 500
        assert call_kwargs["output_tokens"] == 200
        subagent_tool.remove_pending_subagent("exec_123")

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

        with patch("core.sub_agent.SubAgent", return_value=mock_subagent):
            subagent_tool.execute(task="test")

        # Wait for background thread
        entry = subagent_tool.get_pending_subagent("exec_123")
        entry["event"].wait(timeout=5)

        subagent_tool.session_manager.save.assert_called_once()
        subagent_tool.remove_pending_subagent("exec_123")


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
        assert "max_iterations" in props
        assert "run_in_background" in props
        assert "read_task" in props
        assert "kill_task" in props
        assert "list_tasks" in props

    def test_max_iterations_default(self):
        tool = SubAgentTool()
        assert tool.parameters["properties"]["max_iterations"]["default"] == 50
