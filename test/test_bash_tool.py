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


class TestBashOutputStreaming:
    """stdout 读取改为分块后，输出内容与实时性必须与逐字符读取一致。

    回归背景：reader 从文本层 read(1) 改为底层 buffer.read1(8192) 后，
    绕过了文本层的 universal newlines 转换，需自行把 CRLF/孤立 CR 归一为 LF。
    """

    def _bash(self, tools):
        from core.tools import get_tool_by_name
        return get_tool_by_name(tools, "bash")

    def test_crlf_normalized_to_lf(self, tools):
        result = self._bash(tools).execute(command="printf 'a\r\nb\r\nc\r\n'")
        assert not result.error
        assert result.output.strip() == "a\nb\nc"
        assert "\r" not in result.output

    def test_lone_cr_normalized(self, tools):
        result = self._bash(tools).execute(command="printf 'x\ry\r'")
        assert not result.error
        assert "\r" not in result.output

    def test_large_output_not_lost_across_chunks(self, tools):
        """大输出跨多个读取块时不得丢内容（按流式回调统计行数，绕开工具截断）。"""
        tool = self._bash(tools)
        received: list[str] = []
        tool.on_output = lambda text, written: received.append(text)
        try:
            result = tool.execute(command="seq 1 20000", timeout=60)
        finally:
            tool.on_output = None
        assert not result.error
        assert "".join(received).count("\n") == 20000

    def test_streaming_callback_receives_output(self, tools):
        """on_output 回调仍能收到增量输出（实时流式不退化）。"""
        tool = self._bash(tools)
        chunks: list[str] = []
        tool.on_output = lambda text, written: chunks.append(text)
        try:
            result = tool.execute(command="echo streamed-line")
        finally:
            tool.on_output = None
        assert not result.error
        assert "streamed-line" in "".join(chunks)
