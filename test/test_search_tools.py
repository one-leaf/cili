"""搜索工具测试"""

import os


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

    def test_find_tool_basic(self, tools, test_workspace):
        """测试 find 工具 - 基本查找"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path="find_me.txt", content="Found me!")
        write_tool.execute(file_path="other.py", content="Not me")

        find_tool = get_tool_by_name(tools, "find")
        result = find_tool.execute(pattern="*.txt", path=".")
        assert not result.error
        assert "find_me.txt" in result.output
        assert "other.py" not in result.output

    def test_find_tool_recursive(self, tools, test_workspace):
        """测试 find 工具 - 递归查找"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path="level1/file1.txt", content="L1")
        write_tool.execute(file_path="level1/level2/file2.txt", content="L2")

        find_tool = get_tool_by_name(tools, "find")
        result = find_tool.execute(pattern="*.txt", path=".")
        assert not result.error
        assert "file1.txt" in result.output
        assert "file2.txt" in result.output
