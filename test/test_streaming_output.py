"""测试 bash/python 工具实时流式输出功能"""

import os
import threading
import time
import tempfile
from pathlib import Path

import pytest


class TestStreamingOutput:
    """实时流式输出测试"""

    def test_bash_streaming_no_newline(self, tools, test_workspace):
        """测试 bash 工具 - 无换行符的实时流式输出"""
        from core.tools import get_tool_by_name

        bash_tool = get_tool_by_name(tools, "bash")

        # 创建临时文件用于流式输出
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, dir=test_workspace, encoding='utf-8') as f:
            output_file = f.name

        bash_tool.output_file = output_file

        # 监控文件大小变化
        sizes = []
        stop_monitoring = False

        def monitor():
            while not stop_monitoring:
                try:
                    size = os.path.getsize(output_file)
                    sizes.append(size)
                except Exception:
                    pass
                time.sleep(0.05)

        monitor_thread = threading.Thread(target=monitor)
        monitor_thread.start()

        # 执行命令：每 0.2 秒输出一个字符，无换行符
        # 用纯 bash（printf/sleep）而非 python：bash 工具的黑名单禁止从 bash 调 python
        start_time = time.time()
        result = bash_tool.execute(
            command='for c in A B C D E; do printf "%s" "$c"; sleep 0.2; done',
            timeout=30
        )

        stop_monitoring = True
        monitor_thread.join(timeout=2)

        # 验证输出正确
        assert not result.error
        assert "ABCDE" in result.output

        # 验证文件有内容
        final_size = os.path.getsize(output_file)
        assert final_size > 0, "输出文件应该有内容"

        # 验证文件大小有多次变化（实时流式）
        unique_sizes = len(set(s for s in sizes if s > 0))
        assert unique_sizes >= 2, f"应该有至少2次文件大小变化，实际: {sizes}"

        # 清理
        os.unlink(output_file)

    def test_bash_streaming_with_newline(self, tools, test_workspace):
        """测试 bash 工具 - 有换行符的实时流式输出"""
        from core.tools import get_tool_by_name

        bash_tool = get_tool_by_name(tools, "bash")

        # 创建临时文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, dir=test_workspace, encoding='utf-8') as f:
            output_file = f.name

        bash_tool.output_file = output_file

        # 执行命令：每行输出一个字符，有换行符
        result = bash_tool.execute(
            command='for c in A B C D E; do printf "%s\\n" "$c"; sleep 0.1; done',
            timeout=30
        )

        # 验证输出
        assert not result.error
        lines = result.output.strip().split('\n')
        assert len(lines) >= 3

        # 验证文件有内容
        final_size = os.path.getsize(output_file)
        assert final_size > 0

        # 清理
        os.unlink(output_file)

    def test_python_streaming_no_newline(self, tools, test_workspace):
        """测试 python 工具 - 无换行符的实时流式输出"""
        from core.tools import get_tool_by_name

        python_tool = get_tool_by_name(tools, "python")

        # 创建临时文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, dir=test_workspace, encoding='utf-8') as f:
            output_file = f.name

        python_tool.output_file = output_file

        # 监控文件大小
        sizes = []
        stop_monitoring = False

        def monitor():
            while not stop_monitoring:
                try:
                    size = os.path.getsize(output_file)
                    sizes.append(size)
                except Exception:
                    pass
                time.sleep(0.05)

        monitor_thread = threading.Thread(target=monitor)
        monitor_thread.start()

        # 执行 Python 代码：无换行符，实时输出
        code = """
import time
for i in range(5):
    print(i, end=' ', flush=True)
    time.sleep(0.2)
"""
        result = python_tool.execute(action='execute', code=code)

        stop_monitoring = True
        monitor_thread.join(timeout=2)

        # 验证输出
        assert not result.error
        assert "0 1 2 3 4" in result.output

        # 验证文件有内容
        final_size = os.path.getsize(output_file)
        assert final_size > 0

        # 清理
        os.unlink(output_file)

    def test_streaming_large_output(self, tools, test_workspace):
        """测试流式输出 - 大量数据"""
        from core.tools import get_tool_by_name

        bash_tool = get_tool_by_name(tools, "bash")

        # 创建临时文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, dir=test_workspace, encoding='utf-8') as f:
            output_file = f.name

        bash_tool.output_file = output_file

        # 执行命令：输出 100 行
        result = bash_tool.execute(
            command='i=0; while [ $i -lt 100 ]; do printf "Line %d: data" $i; printf "x%.0s" {1..50}; printf "\\n"; i=$((i+1)); sleep 0.05; done',
            timeout=30
        )

        # 验证输出
        assert not result.error
        lines = result.output.strip().split('\n')
        assert len(lines) >= 100

        # 验证文件有内容
        final_size = os.path.getsize(output_file)
        assert final_size > 5000  # 每行约 60 字符，100 行 > 5000

        # 清理
        os.unlink(output_file)

    def test_streaming_unicode(self, tools, test_workspace):
        """测试流式输出 - Unicode 字符"""
        from core.tools import get_tool_by_name

        bash_tool = get_tool_by_name(tools, "bash")

        # 创建临时文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, dir=test_workspace, encoding='utf-8') as f:
            output_file = f.name

        bash_tool.output_file = output_file

        # 执行命令：输出 Unicode（纯 bash printf，Git Bash 下 UTF-8 正常）
        result = bash_tool.execute(
            command='for i in 0 1 2 3 4; do printf "测试 %s " "$i"; sleep 0.1; done',
            timeout=30
        )

        # 验证输出
        assert not result.error
        assert "测试" in result.output

        # 清理
        os.unlink(output_file)

    def test_bash_on_output_hook(self, tools, test_workspace):
        """bash 实时输出触发 on_output 钩子：多 chunk、字节 offset 单调递增、终值=文件字节数"""
        from core.tools import get_tool_by_name

        bash_tool = get_tool_by_name(tools, "bash")
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, dir=test_workspace, encoding='utf-8') as f:
            output_file = f.name

        bash_tool.output_file = output_file
        chunks = []
        offsets = []
        bash_tool.on_output = lambda chunk, offset: (chunks.append(chunk), offsets.append(offset))

        # 每字符带换行：每个字符触发一次 emit，验证增量流式
        result = bash_tool.execute(
            command='for c in A B C D E; do printf "%s\n" "$c"; sleep 0.1; done',
            timeout=30
        )

        assert not result.error
        assert len(chunks) >= 3, f"应收到多个实时 chunk，实际: {chunks}"
        assert "".join(chunks).replace("\n", "") == "ABCDE"
        # offset 严格单调递增
        assert all(b > a for a, b in zip(offsets, offsets[1:])), f"offset 应单调递增: {offsets}"
        # 终值 = 输出文件实际字节数（/stream 端点 f.seek(offset) 契约）
        final_size = os.path.getsize(output_file)
        assert offsets[-1] == final_size, f"offset 终值 {offsets[-1]} != 文件字节数 {final_size}"

        bash_tool.on_output = None
        os.unlink(output_file)

    def test_on_output_unicode_byte_offset(self, tools, test_workspace):
        """Unicode 输出的字节 offset（UTF-8 多字节字符按字节计，非字符数）"""
        from core.tools import get_tool_by_name

        bash_tool = get_tool_by_name(tools, "bash")
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, dir=test_workspace, encoding='utf-8') as f:
            output_file = f.name

        bash_tool.output_file = output_file
        offsets = []
        bash_tool.on_output = lambda chunk, offset: offsets.append(offset)

        result = bash_tool.execute(command='printf "测试\n"', timeout=30)

        assert not result.error
        final_size = os.path.getsize(output_file)
        assert offsets and offsets[-1] == final_size, \
            f"字节 offset {offsets[-1] if offsets else None} != 文件字节数 {final_size}（测试=3字符x3字节+换行=7）"

        bash_tool.on_output = None
        os.unlink(output_file)

    def test_on_output_error_does_not_break_execution(self, tools, test_workspace):
        """on_output 回调抛异常不影响工具执行（钩子静默保护）"""
        from core.tools import get_tool_by_name

        bash_tool = get_tool_by_name(tools, "bash")
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, dir=test_workspace, encoding='utf-8') as f:
            output_file = f.name

        bash_tool.output_file = output_file

        def bad_hook(chunk, offset):
            raise RuntimeError("hook failed")

        bash_tool.on_output = bad_hook
        result = bash_tool.execute(command='echo hello', timeout=30)

        assert not result.error
        assert "hello" in result.output

        bash_tool.on_output = None
        os.unlink(output_file)
