"""文件操作工具测试"""

import os


class TestFileTools:
    """文件操作工具测试"""

    def test_write_tool_basic(self, tools, test_workspace):
        """测试 write 工具 - 基本写入"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        result = write_tool.execute(
            file_path="test_basic.txt",
            content="Hello, Cili!"
        )
        assert not result.error
        assert os.path.exists(os.path.join(test_workspace, "test_basic.txt"))

    def test_write_tool_nested_dir(self, tools, test_workspace):
        """测试 write 工具 - 自动创建父目录"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        result = write_tool.execute(
            file_path="nested/dir/test.txt",
            content="Nested file"
        )
        assert not result.error
        assert os.path.exists(os.path.join(test_workspace, "nested/dir/test.txt"))

    def test_read_tool_basic(self, tools, test_workspace):
        """测试 read 工具 - 基本读取"""
        from core.tools import get_tool_by_name

        # 先写入
        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path="read_test.txt", content="Line 1\nLine 2\nLine 3")

        # 再读取
        read_tool = get_tool_by_name(tools, "read")
        result = read_tool.execute(file_path="read_test.txt")
        assert not result.error
        assert "Line 1" in result.output
        assert "Line 2" in result.output

    def test_read_tool_with_offset(self, tools, test_workspace):
        """测试 read 工具 - 带偏移量读取"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        content = "\n".join([f"Line {i}" for i in range(1, 21)])
        write_tool.execute(file_path="offset_test.txt", content=content)

        read_tool = get_tool_by_name(tools, "read")
        result = read_tool.execute(file_path="offset_test.txt", offset=5, limit=3)
        assert not result.error
        assert "Line 5" in result.output
        assert "Line 6" in result.output
        assert "Line 7" in result.output

    def test_edit_tool_basic(self, tools, test_workspace):
        """测试 edit 工具 - 基本编辑"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path="edit_test.txt", content="Hello World")

        edit_tool = get_tool_by_name(tools, "edit")
        result = edit_tool.execute(
            file_path="edit_test.txt",
            old_text="Hello",
            new_text="Hi"
        )
        assert not result.error

        # 验证修改结果
        read_tool = get_tool_by_name(tools, "read")
        result = read_tool.execute(file_path="edit_test.txt")
        assert "Hi World" in result.output

    def test_edit_tool_not_found(self, tools, test_workspace):
        """测试 edit 工具 - 未找到匹配文本"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path="edit_notfound.txt", content="Test content")

        edit_tool = get_tool_by_name(tools, "edit")
        result = edit_tool.execute(
            file_path="edit_notfound.txt",
            old_text="Nonexistent",
            new_text="New text"
        )
        assert result.error
