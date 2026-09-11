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


class TestLineEndings:
    """行尾保持：LF 文件不被写成 CRLF（Windows 文本模式默认会转换），CRLF 文件保持 CRLF"""

    def test_write_tool_lf_line_endings(self, tools, test_workspace):
        """write 工具写入的内容保持 LF，不被转换为 CRLF"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        result = write_tool.execute(
            file_path="lf_test.sh",
            content="#!/bin/bash\necho hello\necho world\n"
        )
        assert not result.error

        with open(os.path.join(test_workspace, "lf_test.sh"), "rb") as f:
            data = f.read()
        assert b"\r\n" not in data
        assert data == b"#!/bin/bash\necho hello\necho world\n"

    def test_edit_tool_preserves_lf(self, tools, test_workspace):
        """edit 工具编辑 LF 文件后仍是 LF"""
        from core.tools import get_tool_by_name

        path = os.path.join(test_workspace, "edit_lf.txt")
        with open(path, "wb") as f:
            f.write(b"line one\nline two\nline three\n")

        edit_tool = get_tool_by_name(tools, "edit")
        result = edit_tool.execute(
            file_path="edit_lf.txt",
            old_text="line two",
            new_text="line TWO"
        )
        assert not result.error

        with open(path, "rb") as f:
            data = f.read()
        assert b"\r\n" not in data
        assert b"line TWO" in data
        assert b"line one\nline TWO\nline three\n" == data

    def test_edit_tool_preserves_crlf(self, tools, test_workspace):
        """edit 工具编辑 CRLF 文件后仍是 CRLF（new_text 用 \n 书写，写回时转换）"""
        from core.tools import get_tool_by_name

        path = os.path.join(test_workspace, "edit_crlf.txt")
        with open(path, "wb") as f:
            f.write(b"line one\r\nline two\r\nline three\r\n")

        edit_tool = get_tool_by_name(tools, "edit")
        result = edit_tool.execute(
            file_path="edit_crlf.txt",
            old_text="line two",
            new_text="line TWO\nline 2.5"
        )
        assert not result.error

        with open(path, "rb") as f:
            data = f.read()
        # 替换生效且多行 new_text 也统一为 CRLF
        assert b"line TWO\r\nline 2.5\r\nline three\r\n" in data
        # 原有的 CRLF 行未被破坏
        assert b"line one\r\n" in data
        # 不应出现孤立的 \r（即没有混入裸 LF）
        assert data.replace(b"\r\n", b"").find(b"\n") == -1


class TestReadPages:
    """read 工具 PDF 页区间解析：超大区间在展开前拦截，防 OOM。"""

    def _parse_pages(self, pages):
        from core.tools.read import ReadTool
        return ReadTool._parse_pages(pages)

    def test_single_page(self):
        assert self._parse_pages("3") == [3]

    def test_range(self):
        assert self._parse_pages("1-5") == [1, 2, 3, 4, 5]

    def test_comma_mixed(self):
        assert self._parse_pages("1-3,7,9-11") == [1, 2, 3, 7, 9, 10, 11]

    def test_oversized_range_rejected_before_expansion(self):
        """1-1000000000 必须在校验时拒绝，不能先展开 10 亿整数。"""
        import pytest
        with pytest.raises(ValueError):
            self._parse_pages("1-1000000000")

    def test_exact_max_allowed(self):
        from core.tools.read import ReadTool
        assert len(self._parse_pages(f"1-{ReadTool.MAX_PAGES_PER_READ}")) == ReadTool.MAX_PAGES_PER_READ

    def test_combined_pages_over_max_rejected(self):
        import pytest
        with pytest.raises(ValueError):
            self._parse_pages("1-10,20-30")  # 21 页 > 20
