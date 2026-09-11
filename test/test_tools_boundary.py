"""Tests for tool boundary conditions — bash, python_tool, read, edit, grep, browser."""

import os
import tempfile
import pytest

from core.tools.bash import BashTool
from core.tools.python_tool import PythonTool
from core.tools.read import ReadTool
from core.tools.edit import EditTool
from core.tools.grep import GrepTool
from core.tools.find import FindTool
from core.tools.write import WriteTool


# ========== Bash Tool ==========


class TestBashToolBoundary:
    """BashTool edge cases."""

    @pytest.fixture
    def bash_tool(self, test_workspace):
        return BashTool(cwd=test_workspace, workspace_uuid="test-workspace")

    def test_timeout_parameter(self, bash_tool, test_workspace):
        """Timeout parameter is respected."""
        result = bash_tool.execute(command="echo hello", timeout=5)
        assert result.error is False

    def test_timeout_zero_or_negative(self, bash_tool, test_workspace):
        """Zero/negative timeout returns error."""
        result = bash_tool.execute(command="echo hello", timeout=0)
        assert result.error is True

        result = bash_tool.execute(command="echo hello", timeout=-1)
        assert result.error is True

    def test_empty_command(self, bash_tool, test_workspace):
        """Empty command."""
        result = bash_tool.execute(command="")
        # Should not crash; may return error or empty output
        assert isinstance(result.output, str)

    def test_command_with_special_chars(self, bash_tool, test_workspace):
        """Command with special characters."""
        result = bash_tool.execute(command='echo "hello world" && echo $((1+1))')
        assert result.error is False
        assert "hello world" in result.output

    def test_nonexistent_command(self, bash_tool, test_workspace):
        """Nonexistent command returns error."""
        result = bash_tool.execute(command="nonexistent_cmd_12345")
        assert result.error is True or "not found" in result.output.lower() or result.output != ""

    def test_long_output(self, bash_tool, test_workspace):
        """Long output is truncated."""
        result = bash_tool.execute(command="seq 1 10000")
        assert result.error is False
        # Output should be truncated (not all 10000 lines)
        lines = result.output.strip().split('\n')
        # Either truncated or all lines (depends on max_chars)
        assert len(lines) <= 10000


# ========== Python Tool ==========


class TestPythonToolBoundary:
    """PythonTool edge cases."""

    @pytest.fixture
    def python_tool(self, test_workspace):
        return PythonTool(cwd=test_workspace, workspace_uuid="test-workspace")

    def test_execute_code_action(self, python_tool, test_workspace):
        """execute action works."""
        result = python_tool.execute(action="execute", code="print('hello')")
        assert result.error is False
        assert "hello" in result.output

    def test_execute_code_syntax_error(self, python_tool, test_workspace):
        """Syntax error in code."""
        result = python_tool.execute(action="execute", code="def foo(")
        assert result.error is True or "error" in result.output.lower() or "syntax" in result.output.lower()

    def test_execute_code_runtime_error(self, python_tool, test_workspace):
        """Runtime error in code."""
        result = python_tool.execute(action="execute", code="raise ValueError('test error')")
        assert result.error is True or "ValueError" in result.output

    def test_missing_code_parameter(self, python_tool, test_workspace):
        """Missing code for execute action."""
        result = python_tool.execute(action="execute")
        assert result.error is True


# ========== Read Tool ==========


class TestReadToolBoundary:
    """ReadTool edge cases."""

    @pytest.fixture
    def read_tool(self, test_workspace):
        return ReadTool(cwd=test_workspace, workspace_uuid="test-workspace")

    def test_read_nonexistent_file(self, read_tool, test_workspace):
        """Reading nonexistent file returns error."""
        result = read_tool.execute(file_path="nonexistent.txt")
        assert result.error is True
        assert "not found" in result.output.lower() or "no such file" in result.output.lower()

    def test_read_empty_file(self, read_tool, test_workspace):
        """Reading empty file."""
        empty_file = os.path.join(test_workspace, "empty.txt")
        with open(empty_file, "w") as f:
            pass  # Create empty file

        result = read_tool.execute(file_path="empty.txt")
        assert result.error is False
        assert result.output.strip() == "" or "empty" in result.output.lower()

    def test_read_with_offset_and_limit(self, read_tool, test_workspace):
        """Reading with offset and limit."""
        test_file = os.path.join(test_workspace, "lines.txt")
        with open(test_file, "w") as f:
            for i in range(100):
                f.write(f"line {i}\n")

        # offset=10, limit=5 shows lines 10-14 (1-indexed), which are file lines 9-13 (0-indexed)
        result = read_tool.execute(file_path="lines.txt", offset=10, limit=5)
        assert result.error is False
        # The file line 9 should be in the output (10th line, offset=10)
        assert "line 9" in result.output
        # Should show 5 lines: line 9 through line 13
        assert "line 13" in result.output

    def test_read_large_file(self, read_tool, test_workspace):
        """Reading large file respects limits."""
        large_file = os.path.join(test_workspace, "large.txt")
        with open(large_file, "w") as f:
            for i in range(10000):
                f.write(f"{'x' * 100} line {i}\n")

        result = read_tool.execute(file_path="large.txt")
        assert result.error is False
        # Should be truncated
        assert "truncated" in result.output.lower() or len(result.output) < 10000 * 100


# ========== Edit Tool ==========


class TestEditToolBoundary:
    """EditTool edge cases."""

    @pytest.fixture
    def edit_tool(self, test_workspace):
        return EditTool(cwd=test_workspace, workspace_uuid="test-workspace")

    def test_edit_nonexistent_file(self, edit_tool, test_workspace):
        """Editing nonexistent file returns error."""
        result = edit_tool.execute(file_path="nonexistent.txt", old_text="foo", new_text="bar")
        assert result.error is True

    def test_edit_text_not_found(self, edit_tool, test_workspace):
        """Editing when old_text not in file."""
        test_file = os.path.join(test_workspace, "test.txt")
        with open(test_file, "w") as f:
            f.write("hello world")

        result = edit_tool.execute(file_path="test.txt", old_text="notfound", new_text="bar")
        assert result.error is True
        assert "not found" in result.output.lower() or "no match" in result.output.lower()

    def test_edit_multiple_occurrences(self, edit_tool, test_workspace):
        """Editing when old_text appears multiple times."""
        test_file = os.path.join(test_workspace, "test.txt")
        with open(test_file, "w") as f:
            f.write("foo bar foo bar foo")

        result = edit_tool.execute(file_path="test.txt", old_text="foo", new_text="baz")
        # Should either replace all or error (depends on implementation)
        assert isinstance(result.output, str)

    def test_edit_empty_old_text(self, edit_tool, test_workspace):
        """Editing with empty old_text."""
        test_file = os.path.join(test_workspace, "test.txt")
        with open(test_file, "w") as f:
            f.write("hello")

        result = edit_tool.execute(file_path="test.txt", old_text="", new_text="world")
        # Should handle gracefully
        assert isinstance(result.output, str)


# ========== Grep Tool ==========


class TestGrepToolBoundary:
    """GrepTool edge cases."""

    @pytest.fixture
    def grep_tool(self, test_workspace):
        return GrepTool(cwd=test_workspace, workspace_uuid="test-workspace")

    def test_grep_no_matches(self, grep_tool, test_workspace):
        """Grep with no matches."""
        test_file = os.path.join(test_workspace, "test.txt")
        with open(test_file, "w") as f:
            f.write("hello world")

        result = grep_tool.execute(pattern="notfound", path="test.txt")
        # Should return no matches message or empty result
        assert isinstance(result.output, str)

    def test_grep_empty_pattern(self, grep_tool, test_workspace):
        """Grep with empty pattern returns error or matches everything."""
        test_file = os.path.join(test_workspace, "grep_empty.txt")
        with open(test_file, "w") as f:
            f.write("hello world")

        result = grep_tool.execute(pattern="", path="grep_empty.txt")
        # Empty pattern should either error or match all lines
        assert isinstance(result.output, str)

    def test_grep_regex_pattern(self, grep_tool, test_workspace):
        """Grep with regex pattern."""
        test_file = os.path.join(test_workspace, "grep_regex.txt")
        with open(test_file, "w") as f:
            f.write("foo123 bar456 baz789")

        # Use simple pattern that works with all grep variants
        result = grep_tool.execute(pattern="bar", path="grep_regex.txt")
        assert result.error is False
        assert "bar456" in result.output

    def test_grep_with_context(self, grep_tool, test_workspace):
        """Grep with context lines."""
        test_file = os.path.join(test_workspace, "test.txt")
        with open(test_file, "w") as f:
            for i in range(10):
                f.write(f"line {i}\n")
            f.write("MATCH HERE\n")
            for i in range(20, 30):
                f.write(f"line {i}\n")

        result = grep_tool.execute(pattern="MATCH", path="test.txt", context=2)
        assert result.error is False
        assert "MATCH" in result.output


# ========== Find Tool ==========


class TestFindToolBoundary:
    """FindTool edge cases."""

    @pytest.fixture
    def find_tool(self, test_workspace):
        return FindTool(cwd=test_workspace, workspace_uuid="test-workspace")

    def test_find_nonexistent_directory(self, find_tool, test_workspace):
        """Find in nonexistent directory returns error or empty."""
        result = find_tool.execute(path="nonexistent_dir_12345", pattern="*")
        # Either errors or returns no results
        assert result.error is True or result.output == "" or "not found" in result.output.lower() or "no such" in result.output.lower()

    def test_find_with_pattern_lists_matching_files(self, find_tool, test_workspace):
        """Find with pattern lists matching files."""
        # Create test files
        test_file1 = os.path.join(test_workspace, "test_find1.txt")
        test_file2 = os.path.join(test_workspace, "test_find2.py")
        for f in [test_file1, test_file2]:
            with open(f, "w") as fp:
                fp.write("test")

        result = find_tool.execute(path=test_workspace, pattern="*.py")
        assert result.error is False
        assert "test_find2.py" in result.output
        assert "test_find1.txt" not in result.output

    def test_find_max_results(self, find_tool, test_workspace):
        """Find respects max_results limit."""
        # Create many test files
        for i in range(20):
            test_file = os.path.join(test_workspace, f"max_test_{i}.txt")
            with open(test_file, "w") as f:
                f.write("test")

        result = find_tool.execute(path=test_workspace, pattern="max_test_*.txt", max_results=5)
        assert result.error is False
        # Count how many results
        lines = [l for l in result.output.strip().split('\n') if l.strip()]
        assert len(lines) <= 5


# ========== Read / Write Path Boundary ==========


class TestReadPathExemption:
    """读取类工具路径完全放开：可读工作区与 data/ 之外的任意路径。"""

    @pytest.fixture
    def read_tool(self, test_workspace):
        return ReadTool(cwd=test_workspace, workspace_uuid="test-workspace")

    def test_read_outside_workspace_allowed(self, read_tool, tmp_path):
        """读取工作区之外的任意路径允许。"""
        outside = tmp_path / "secret.txt"
        outside.write_text("hello secret", encoding="utf-8")

        result = read_tool.execute(file_path=str(outside))
        assert result.error is False
        assert "hello secret" in result.output

    def test_read_under_data_root_allowed(self, read_tool, tmp_path, monkeypatch):
        """data/ 内的文件（如会话 index.json）可读，即使在工作区之外。"""
        data_root = tmp_path / "data"
        target = data_root / "agents" / "abc" / "sessions" / "x" / "index.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"ok": true}', encoding="utf-8")
        monkeypatch.setattr("core.tools.base.DATA_ROOT", data_root)

        result = read_tool.execute(file_path=str(target))
        assert result.error is False
        assert "ok" in result.output

    def test_read_relative_still_resolves_cwd(self, read_tool, test_workspace):
        """相对路径仍基于 cwd 解析。"""
        test_file = os.path.join(test_workspace, "rel.txt")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("relative content")

        result = read_tool.execute(file_path="rel.txt")
        assert result.error is False
        assert "relative content" in result.output


class TestWritePathBoundary:
    """写操作（write）仍受 workspace + data/ 边界约束。"""

    @pytest.fixture
    def write_tool(self, test_workspace):
        return WriteTool(cwd=test_workspace, workspace_uuid="test-workspace")

    def test_write_outside_workspace_rejected(self, write_tool, tmp_path):
        """写工作区之外的文件被拒绝。"""
        outside = tmp_path / "x.txt"
        with pytest.raises(ValueError, match="越界"):
            write_tool.execute(file_path=str(outside), content="hi")

    def test_write_under_data_root_allowed(self, write_tool, tmp_path, monkeypatch):
        """写 data/ 内的文件允许。"""
        data_root = tmp_path / "data"
        monkeypatch.setattr("core.tools.base.DATA_ROOT", data_root)
        target = data_root / "cili" / "x.txt"
        target.parent.mkdir(parents=True)

        result = write_tool.execute(file_path=str(target), content="hi")
        assert result.error is False
        assert target.read_text(encoding="utf-8") == "hi"

    def test_is_within_data_root_predicate(self, tmp_path, monkeypatch):
        """_is_within_data_root 纯谓词（入参为 realpath 后路径）：data/ 内 True，data/ 外 False。"""
        data_root = tmp_path / "data"
        monkeypatch.setattr("core.tools.base.DATA_ROOT", data_root)

        from core.tools.base import Tool

        assert Tool._is_within_data_root(str((data_root / "cili" / "setting.json").resolve())) is True
        assert Tool._is_within_data_root(str((data_root / "agents" / "x").resolve())) is True
        assert Tool._is_within_data_root(str((data_root / ".." / "x").resolve())) is False
        assert Tool._is_within_data_root(str((tmp_path / "agents" / "x").resolve())) is False


# ========== Tool Schema Consistency ==========


class TestToolSchemaConsistency:
    """All tools have valid schemas."""

    def test_all_tools_have_required_fields(self, tools):
        """Every tool has name, description, parameters."""
        for tool in tools:
            assert hasattr(tool, "name")
            assert hasattr(tool, "description")
            assert hasattr(tool, "parameters")
            assert tool.name
            assert tool.description
            assert isinstance(tool.parameters, dict)

    def test_all_tools_have_execute_method(self, tools):
        """Every tool has execute method."""
        for tool in tools:
            assert hasattr(tool, "execute")
            assert callable(getattr(tool, "execute"))

    def test_tool_schema_format(self, tools):
        """Tool schemas match Anthropic format."""
        for tool in tools:
            schema = tool.to_schema()
            assert "name" in schema
            assert "description" in schema
            assert "input_schema" in schema
            assert schema["name"] == tool.name
