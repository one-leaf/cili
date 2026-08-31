"""Bash 工具测试"""

import os
from pathlib import Path


class TestBashTool:
    """Bash 工具测试"""

    def test_bash_basic_command(self, tools):
        """测试 bash 工具 - 基本命令"""
        from core.tools import get_tool_by_name

        bash_tool = get_tool_by_name(tools, "bash")
        result = bash_tool.execute(command="echo 'test'")
        assert not result.error
        assert "test" in result.output

    def test_bash_directory_listing(self, tools, test_workspace):
        """测试 bash 工具 - 目录列表"""
        from core.tools import get_tool_by_name

        # 先创建一些文件
        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path="ls_test.txt", content="test")

        bash_tool = get_tool_by_name(tools, "bash")
        result = bash_tool.execute(command="ls -la")
        assert not result.error
        assert "ls_test.txt" in result.output

    def test_bash_python_execution(self, tools):
        """测试 bash 工具 - Python 执行"""
        from core.tools import get_tool_by_name

        bash_tool = get_tool_by_name(tools, "bash")
        result = bash_tool.execute(command="python3 --version")
        # 某些环境可能没有 python3，尝试 python
        if result.error:
            result = bash_tool.execute(command="python --version")
        # 应该能找到 Python
        assert "Python" in result.output or "python" in result.output.lower()

    def test_bash_working_directory(self, tools, test_workspace):
        """测试 bash 工具 - 工作目录"""
        from core.tools import get_tool_by_name

        bash_tool = get_tool_by_name(tools, "bash")
        result = bash_tool.execute(command="pwd")
        assert not result.error
        # 应该包含测试工作目录的路径
        assert test_workspace.replace("\\", "/") in result.output or \
               Path(test_workspace).name in result.output
