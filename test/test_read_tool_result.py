"""Tests for read_tool_result tool."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock


class TestReadToolResult:
    """read_tool_result tool tests."""

    def test_reads_compacted_result(self, test_workspace):
        """Should read tool result file from session directory."""
        from core.tools.shared.read_tool_result import ReadToolResultTool

        # Create mock session manager
        test_workspace = Path(test_workspace)
        session_dir = test_workspace / "sessions" / "abc123"
        session_dir.mkdir(parents=True, exist_ok=True)

        # Create a test tool result file
        tool_use_id = "toolu_test123"
        result_file = session_dir / f"{tool_use_id}.txt"
        result_file.write_text("Original tool output content", encoding="utf-8")

        mock_sm = MagicMock()
        mock_sm.session_dir = session_dir

        tool = ReadToolResultTool(cwd=str(test_workspace), session_manager=mock_sm)
        result = tool.execute(tool_use_id=tool_use_id)

        assert not result.is_error
        assert "Original tool output content" in result.output

    def test_file_not_found(self, test_workspace):
        """Should return error when file doesn't exist."""
        from core.tools.shared.read_tool_result import ReadToolResultTool

        test_workspace = Path(test_workspace)
        session_dir = test_workspace / "sessions" / "abc123"
        session_dir.mkdir(parents=True, exist_ok=True)

        mock_sm = MagicMock()
        mock_sm.session_dir = session_dir

        tool = ReadToolResultTool(cwd=str(test_workspace), session_manager=mock_sm)
        result = tool.execute(tool_use_id="nonexistent_id")

        assert result.is_error
        assert "not found" in result.output.lower()

    def test_searches_exec_directories(self, test_workspace):
        """Should search exec_* subdirectories for SubAgent results."""
        from core.tools.shared.read_tool_result import ReadToolResultTool

        test_workspace = Path(test_workspace)
        session_dir = test_workspace / "sessions" / "abc123"
        exec_dir = session_dir / "exec_001"
        exec_dir.mkdir(parents=True)

        # Create file in exec directory
        tool_use_id = "toolu_subagent123"
        result_file = exec_dir / f"{tool_use_id}.txt"
        result_file.write_text("SubAgent tool output", encoding="utf-8")

        mock_sm = MagicMock()
        mock_sm.session_dir = session_dir

        tool = ReadToolResultTool(cwd=str(test_workspace), session_manager=mock_sm)
        result = tool.execute(tool_use_id=tool_use_id)

        assert not result.is_error
        assert "SubAgent tool output" in result.output

    def test_empty_output(self, test_workspace):
        """Should handle empty file gracefully."""
        from core.tools.shared.read_tool_result import ReadToolResultTool

        test_workspace = Path(test_workspace)
        session_dir = test_workspace / "sessions" / "abc123"
        session_dir.mkdir(parents=True, exist_ok=True)

        tool_use_id = "toolu_empty"
        result_file = session_dir / f"{tool_use_id}.txt"
        result_file.write_text("", encoding="utf-8")

        mock_sm = MagicMock()
        mock_sm.session_dir = session_dir

        tool = ReadToolResultTool(cwd=str(test_workspace), session_manager=mock_sm)
        result = tool.execute(tool_use_id=tool_use_id)

        assert not result.is_error
        assert "empty" in result.output.lower()

    def test_no_session_manager(self, test_workspace):
        """Should return error when session_manager is not available."""
        from core.tools.shared.read_tool_result import ReadToolResultTool

        test_workspace = Path(test_workspace)
        tool = ReadToolResultTool(cwd=str(test_workspace), session_manager=None)
        result = tool.execute(tool_use_id="toolu_test")

        assert result.is_error
        assert "not available" in result.output.lower()
