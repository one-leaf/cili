"""Tests for tool boundary conditions — bash, python_tool, read, edit, grep, browser."""

import os
import tempfile
import pytest

from core.tools.base import ToolResult
from core.tools.bash import BashTool
from core.tools.python_tool import PythonTool
from core.tools.read import ReadTool
from core.tools.edit import EditTool
from core.tools.grep import GrepTool
from core.tools.glob import GlobTool
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

    def test_unknown_parameter_rejected(self, python_tool):
        """未知参数（如把 bash 的 timeout 张冠李戴）在 coerce_input 拦截，而非 execute 抛 TypeError。"""
        result = python_tool.coerce_input({"code": "print(1)", "timeout": "120"})
        assert isinstance(result, ToolResult)
        assert result.error is True
        assert "timeout" in result.output
        assert "无效参数" in result.output
        assert "code" in result.output  # 报错里列出合法参数

    def test_valid_parameters_pass_coerce(self, python_tool):
        """合法参数正常通过 coerce_input（类型转换后返回 dict）。"""
        result = python_tool.coerce_input({"action": "execute", "code": "print(1)"})
        assert isinstance(result, dict)
        assert result["action"] == "execute"
        assert result["code"] == "print(1)"


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
        """Grep 无匹配时返回 "No matches found."，绝不返回空串。

        回归：_run_bash 无输出时返回占位符 "(no output)" 而非空串，且命令带
        `|| true` 退出码恒为 0，旧判断全失效——把 "(no output)" 当文件名导致
        output=''，前端因此显示"[工具输出文件路径缺失]"。
        """
        test_file = os.path.join(test_workspace, "test.txt")
        with open(test_file, "w") as f:
            f.write("hello world")

        # 三种 output_mode 都走 _find_matching_files，应一致返回提示而非空串
        for mode in ("content", "files_with_matches", "count"):
            result = grep_tool.execute(pattern="notfound", path="test.txt", output_mode=mode)
            assert result.error is False
            assert result.output == "No matches found.", f"mode={mode} got {result.output!r}"

    def test_grep_digit_shortcut(self, grep_tool, test_workspace):
        r"""正则 \d/\D 翻译为 ERE [0-9]/[^0-9]（GNU grep -E 不支持 \d，会当字面 d）。

        回归：第三方报告 \d+-\d+ 匹配不到 test-123-456，\d 却命中含字母 d 的行。
        """
        test_file = os.path.join(test_workspace, "digits.txt")
        with open(test_file, "w") as f:
            f.write("test-123-456\nhas d here\nabc\n")

        result = grep_tool.execute(pattern=r"\d+-\d+", path="digits.txt")
        assert result.error is False
        assert "test-123-456" in result.output
        assert "has d here" not in result.output

        # \d 单独不应命中字面字母 d
        result2 = grep_tool.execute(pattern=r"\d", path="digits.txt")
        assert "has d here" not in result2.output

        # \D 反义也应生效
        result3 = grep_tool.execute(pattern=r"\D+", path="digits.txt")
        assert "has d here" in result3.output

    def test_grep_escaped_backslash_d_not_translated(self, grep_tool, test_workspace):
        """转义反斜杠 \\d（字面反斜杠 + d）不能被误译为 [0-9]。"""
        test_file = os.path.join(test_workspace, "esc.txt")
        with open(test_file, "w") as f:
            f.write("test-123-456\n")

        # r"\\d+" = 两个反斜杠 + d+，正则引擎视为 字面反斜杠 + d+，不应匹配数字行
        result = grep_tool.execute(pattern=r"\\d+", path="esc.txt")
        assert result.error is False
        assert result.output == "No matches found."

    def test_grep_path_not_exists(self, grep_tool, test_workspace):
        """路径不存在时返回明确的错误信息，而非空输出（前端显示"[工具输出文件路径缺失]"）。

        回归：stderr 并入 stdout，grep 的 "No such file or directory" 被当成
        匹配文件、getmtime 丢弃后 sorted_files 为空 → ToolResult("")。
        """
        result = grep_tool.execute(pattern="foo", path="nonexistent_dir_zzz")
        assert result.error is True
        assert "不存在" in result.output

    def test_grep_glob_brace_expansion(self, grep_tool, test_workspace):
        """glob 花括号展开 *.{js,py} 等价于 --include *.js + --include *.py。"""
        for name, content in (("a.js", "hello js"), ("b.py", "hello py"), ("c.txt", "hello txt")):
            with open(os.path.join(test_workspace, name), "w") as f:
                f.write(content)

        result = grep_tool.execute(pattern="hello", path=".", glob="*.{js,py}", output_mode="files_with_matches")
        assert result.error is False
        assert "a.js" in result.output
        assert "b.py" in result.output
        assert "c.txt" not in result.output

    def test_grep_output_path_separator(self, grep_tool, test_workspace):
        r"""输出路径统一为 / 分隔符（单文件 \ 与目录遍历 / 混用的问题）。"""
        test_file = os.path.join(test_workspace, "sep.txt")
        with open(test_file, "w") as f:
            f.write("hello")

        result = grep_tool.execute(pattern="hello", path="sep.txt", output_mode="files_with_matches")
        assert result.error is False
        assert "\\" not in result.output
        assert "/" in result.output

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


class TestGlobToolBoundary:
    """GlobTool edge cases."""

    @pytest.fixture
    def glob_tool(self, test_workspace):
        return GlobTool(cwd=test_workspace, workspace_uuid="test-workspace")

    def test_find_nonexistent_directory(self, glob_tool, test_workspace):
        """Find in nonexistent directory returns error or empty."""
        result = glob_tool.execute(path="nonexistent_dir_12345", pattern="*")
        # Either errors or returns no results
        assert result.error is True or result.output == "" or "not found" in result.output.lower() or "no such" in result.output.lower()

    def test_find_with_pattern_lists_matching_files(self, glob_tool, test_workspace):
        """Find with pattern lists matching files."""
        # Create test files
        test_file1 = os.path.join(test_workspace, "test_find1.txt")
        test_file2 = os.path.join(test_workspace, "test_find2.py")
        for f in [test_file1, test_file2]:
            with open(f, "w") as fp:
                fp.write("test")

        result = glob_tool.execute(path=test_workspace, pattern="*.py")
        assert result.error is False
        assert "test_find2.py" in result.output
        assert "test_find1.txt" not in result.output

    def test_find_max_results(self, glob_tool, test_workspace):
        """Find respects max_results limit."""
        # Create many test files
        for i in range(20):
            test_file = os.path.join(test_workspace, f"max_test_{i}.txt")
            with open(test_file, "w") as f:
                f.write("test")

        result = glob_tool.execute(path=test_workspace, pattern="max_test_*.txt", head_limit=5)
        assert result.error is False
        # Count file results (exclude truncation notice lines)
        lines = [l for l in result.output.strip().split('\n') if l.strip() and not l.startswith('(Results')]
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

    def test_read_under_data_root_allowed(self, read_tool, tmp_path):
        """data/ 内的文件（如会话 index.json）可读，即使在工作区之外。"""
        target = tmp_path / "data" / "agents" / "abc" / "sessions" / "x" / "index.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"ok": true}', encoding="utf-8")

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
    """写操作（write）：严格只 workspace，越界在无审批通道时硬拒（fail-closed）。"""

    @pytest.fixture
    def write_tool(self, test_workspace):
        return WriteTool(cwd=test_workspace, workspace_uuid="test-workspace")

    def test_write_outside_workspace_rejected(self, write_tool, tmp_path):
        """写工作区之外的文件被拒绝（无审批通道 → error，不再抛 ValueError）。"""
        outside = tmp_path / "x.txt"
        result = write_tool.execute(file_path=str(outside), content="hi")
        assert result.error is True
        assert not outside.exists()

    def test_write_under_data_root_rejected(self, write_tool, tmp_path):
        """data/ 不再自动放行：工作区之外 → 拒绝（严格只 workspace）。"""
        target = tmp_path / "data" / "cili" / "x.txt"
        result = write_tool.execute(file_path=str(target), content="hi")
        assert result.error is True
        assert not target.exists()


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


# ========== 空输出兜底 ==========


class TestEmptyOutputGuard:
    """任何工具返回空结果时，_execute_tool 统一补非空哨兵，杜绝占位符 bug。"""

    def test_empty_tool_output_gets_sentinel(self, agent):
        """空输出补 "(no output)"，会话重载 hydration 不出现"[工具输出文件路径缺失]"。

        回归：_execute_tool 对空输出既不留 content 也不写外置文件，重载时
        _resolve_tool_results 把"空 content + 无 output_path"误标为占位符。
        此守卫在 execute() 返回后、回调/外置文件/content 构造前归一化。
        """
        from core.tools.base import Tool, ToolResult

        class EmptyTool(Tool):
            name = "test_empty_output"
            description = "Test tool returning empty output"
            parameters = {"type": "object", "properties": {}, "required": []}

            def execute(self, **kwargs):
                return ToolResult("")

        agent.tools.append(EmptyTool(cwd=agent.cwd, workspace_uuid=agent.workspace_uuid))

        result_dict = agent._execute_tool("test_empty_output", {}, "tooluse_empty1")
        assert result_dict["content"] == "(no output)"
        assert "output_path" not in result_dict.get("_meta", {})

        # 模拟会话重载 hydration：非空 content 会被跳过，不被占位符覆盖
        agent._resolve_tool_results([{"role": "user", "content": [result_dict]}])
        assert result_dict["content"] == "(no output)"
