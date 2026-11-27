"""搜索工具测试"""

import shutil
import subprocess

import pytest


class TestSearchTools:
    """搜索工具测试"""

    def test_grep_tool_basic(self, tools, test_workspace):
        """测试 grep 工具 - 基本搜索"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path="grep_test.py", content="def hello():\n    print('hello')")

        grep_tool = get_tool_by_name(tools, "grep")
        result = grep_tool.execute(pattern="hello", path=".")
        assert not result.error
        assert "grep_test.py" in result.output
        assert "hello" in result.output.lower()

    def test_grep_tool_case_insensitive(self, tools, test_workspace):
        """测试 grep 工具 - 忽略大小写"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path="case_test.txt", content="Hello HELLO hello")

        grep_tool = get_tool_by_name(tools, "grep")
        result = grep_tool.execute(pattern="hello", path="case_test.txt", case_insensitive=True)
        assert not result.error
        # 应该找到匹配
        assert "hello" in result.output.lower()

    def test_glob_tool_basic(self, tools, test_workspace):
        """测试 glob 工具 - 基本查找"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path="find_me.txt", content="Found me!")
        write_tool.execute(file_path="other.py", content="Not me")

        glob_tool = get_tool_by_name(tools, "glob")
        result = glob_tool.execute(pattern="*.txt", path=".")
        assert not result.error
        assert "find_me.txt" in result.output
        assert "other.py" not in result.output

    def test_glob_tool_recursive(self, tools, test_workspace):
        """测试 glob 工具 - 递归查找"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path="level1/file1.txt", content="L1")
        write_tool.execute(file_path="level1/level2/file2.txt", content="L2")

        glob_tool = get_tool_by_name(tools, "glob")
        result = glob_tool.execute(pattern="*.txt", path=".")
        assert not result.error
        assert "file1.txt" in result.output
        assert "file2.txt" in result.output

    def test_glob_explicit_dot_dir(self, tools, test_workspace):
        """显式写进 pattern 的点目录（.cili/tmp）不再被默认忽略。"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path=".cili/tmp/hidden.txt", content="hidden")

        glob_tool = get_tool_by_name(tools, "glob")
        result = glob_tool.execute(pattern=".cili/tmp/*.txt")
        assert not result.error
        assert "hidden.txt" in result.output

        # 未显式点名时仍按默认忽略列表跳过
        result = glob_tool.execute(pattern="**/*.txt")
        assert "hidden.txt" not in result.output

    @pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
    def test_glob_and_grep_respect_gitignore(self, tools, test_workspace):
        """git 工作区按 .gitignore 排除；显式点名忽略目录时仍可搜索。"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path=".gitignore", content="ignored/\n")
        write_tool.execute(file_path="visible.txt", content="needle")
        write_tool.execute(file_path="ignored/hidden.txt", content="needle")

        subprocess.run(
            ["git", "init", "-q"], cwd=test_workspace, check=True,
            capture_output=True,
        )

        glob_tool = get_tool_by_name(tools, "glob")
        result = glob_tool.execute(pattern="**/*.txt")
        assert "visible.txt" in result.output
        assert "ignored" not in result.output

        # 显式点名被忽略目录：绕过 .gitignore
        result = glob_tool.execute(pattern="ignored/*.txt")
        assert "hidden.txt" in result.output

        grep_tool = get_tool_by_name(tools, "grep")
        result = grep_tool.execute(pattern="needle", path=".")
        assert "visible.txt" in result.output
        assert "hidden.txt" not in result.output

        result = grep_tool.execute(pattern="needle", path="ignored")
        assert "hidden.txt" in result.output
