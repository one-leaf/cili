# -*- coding: utf-8 -*-
"""测试 Windows UTF-8 编码修复"""

import pytest
import sys
import os
import tempfile
import shutil
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.tools.shared.read import ReadTool
from core.tools.shared.write import WriteTool
from core.tools.shared.edit import EditTool
from core.tools.shared.python_tool import PythonTool


class TestUTF8Encoding:
    """测试 UTF-8 编码处理"""

    def setup_method(self):
        """每个测试前创建临时目录"""
        self.temp_dir = tempfile.mkdtemp(prefix="cili_test_utf8_")
        self.cwd = self.temp_dir

    def teardown_method(self):
        """每个测试后清理临时目录"""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_read_utf8_content(self):
        """测试读取包含特殊 Unicode 字符的文件"""
        # 创建包含特殊字符的测试文件
        test_content = """这是中文测试
包含特殊字符：鿿 (CJK Extension B)
数学符号: ∀ ∃ ∑
制表符: ─│┌┐
"""
        test_file = os.path.join(self.temp_dir, "utf8_test.txt")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write(test_content)

        # 使用 ReadTool 读取
        read_tool = ReadTool(cwd=self.cwd)
        result = read_tool.execute(file_path=test_file)

        # 验证没有错误
        assert not result.error, f"读取失败: {result.output}"
        assert "这是中文测试" in result.output
        assert "鿿" in result.output

    def test_write_utf8_content(self):
        """测试写入包含特殊 Unicode 字符的文件"""
        test_file = os.path.join(self.temp_dir, "write_test.txt")

        # 使用 WriteTool 写入 - 测试常见的 Unicode 字符
        write_tool = WriteTool(cwd=self.cwd)
        result = write_tool.execute(
            file_path=test_file,
            content="中文内容\n特殊字符: 鿿 ∀ ∃ ∑\n"
        )

        # 验证写入成功
        assert not result.error, f"写入失败: {result.output}"
        assert os.path.exists(test_file)

        # 验证文件内容
        with open(test_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert "中文内容" in content
        assert "鿿" in content

    def test_edit_utf8_content(self):
        """测试编辑包含特殊 Unicode 字符的文件"""
        test_file = os.path.join(self.temp_dir, "edit_test.txt")

        # 先写入初始内容
        initial_content = "原始内容\n包含 鿿 字符\n结束行"
        with open(test_file, "w", encoding="utf-8") as f:
            f.write(initial_content)

        # 使用 EditTool 编辑 - 测试常见的 Unicode 字符
        edit_tool = EditTool(cwd=self.cwd)
        result = edit_tool.execute(
            file_path=test_file,
            old_text="包含 鿿 字符",
            new_text="修改后包含 ∀ ∃ ∑ 字符"
        )

        # 验证编辑成功
        assert not result.error, f"编辑失败: {result.output}"

        # 验证文件内容已修改
        with open(test_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert "原始内容" in content
        assert "修改后包含 ∀ ∃ ∑ 字符" in content
        assert "鿿" not in content

    def test_python_execute_utf8(self):
        """测试执行包含特殊 Unicode 字符的 Python 代码"""
        python_tool = PythonTool(cwd=self.cwd)

        # 执行打印特殊字符的代码
        test_code = """
print("中文输出")
print(f"特殊字符: {'\\u9fff'}")
print(f"数学符号: {'\\u2200 \\u2203 \\u2211'}")
"""
        result = python_tool.execute(action="execute", code=test_code)

        # 验证没有错误
        assert not result.error, f"Python 执行失败: {result.output}"
        assert "中文输出" in result.output
        assert "鿿" in result.output

    def test_python_execute_exception(self):
        """测试执行抛出异常的 Python 代码"""
        python_tool = PythonTool(cwd=self.cwd)

        # 执行会抛出异常的代码
        test_code = """
x = "包含特殊字符 鿿"
raise ValueError(f"测试错误: {x}")
"""
        result = python_tool.execute(action="execute", code=test_code)

        # 验证有错误输出
        assert result.error, "应该有错误"
        assert "测试错误" in result.output
        assert "鿿" in result.output

    def test_read_large_utf8_file(self):
        """测试读取包含大量特殊字符的大文件"""
        test_file = os.path.join(self.temp_dir, "large_utf8.txt")

        # 创建包含大量特殊字符的文件
        with open(test_file, "w", encoding="utf-8") as f:
            for i in range(100):
                f.write(f"第 {i} 行: 中文 鿿 ∀ ∃ ∑\n")

        # 使用 ReadTool 读取
        read_tool = ReadTool(cwd=self.cwd)
        result = read_tool.execute(file_path=test_file)

        # 验证没有错误
        assert not result.error, f"读取大文件失败: {result.output}"
        assert "第 0 行" in result.output
        assert "第 99 行" in result.output


def main():
    """运行测试"""
    pytest.main([__file__, "-v", "--tb=short"])


if __name__ == "__main__":
    main()
