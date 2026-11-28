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

    def test_write_blocks_workflow_definition(self, tools, test_workspace):
        """工作流定义必须走 workflow 工具：直接写 .cili/workflows 被拒绝"""
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        result = write_tool.execute(
            file_path=".cili/workflows/abcd1234/workflow.json",
            content="{}",
        )
        assert result.error
        assert "workflow" in result.output
        assert not os.path.exists(
            os.path.join(test_workspace, ".cili/workflows/abcd1234/workflow.json"))

    def test_edit_blocks_workflow_definition(self, tools):
        """edit 同样不允许改工作流定义文件"""
        from core.tools import get_tool_by_name

        edit_tool = get_tool_by_name(tools, "edit")
        result = edit_tool.execute(
            file_path=".cili/workflows/abcd1234/workflow.json",
            old_text="a",
            new_text="b",
        )
        assert result.error
        assert "workflow" in result.output

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

    def test_read_tool_directory_reports_not_a_file(self, tmp_path):
        """读目录报 not a file，而不是 Windows 的 Permission denied"""
        from core.tools.read import ReadTool

        (tmp_path / "adir").mkdir()
        result = ReadTool(cwd=str(tmp_path)).execute(file_path="adir")
        assert result.error
        assert "not a file" in result.output

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


class TestPowerShellScriptEncoding:
    """PowerShell 脚本统一写 UTF-8 BOM。

    Windows PowerShell 5.1 读取无 BOM 的 UTF-8 脚本时按系统 ANSI 代码页
    （简中为 GBK）解码，中文内容会触发莫名其妙的语法错误。
    """

    def test_write_ps1_adds_utf8_bom(self, tools, test_workspace):
        from pathlib import Path
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        result = write_tool.execute(file_path="script.ps1", content="Write-Output '中文'\n")
        assert not result.error

        raw = (Path(test_workspace) / "script.ps1").read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert not raw[3:].startswith(b"\xef\xbb\xbf")
        assert raw.decode("utf-8-sig") == "Write-Output '中文'\n"

    def test_write_ps1_strips_leading_bom(self, tools, test_workspace):
        from pathlib import Path
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        result = write_tool.execute(
            file_path="script_bom.ps1", content="﻿Write-Output '中文'\n"
        )
        assert not result.error

        raw = (Path(test_workspace) / "script_bom.ps1").read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert not raw[3:].startswith(b"\xef\xbb\xbf")
        assert raw.decode("utf-8-sig") == "Write-Output '中文'\n"

    def test_write_txt_keeps_no_bom(self, tools, test_workspace):
        from pathlib import Path
        from core.tools import get_tool_by_name

        write_tool = get_tool_by_name(tools, "write")
        result = write_tool.execute(file_path="note.txt", content="中文\n")
        assert not result.error

        raw = (Path(test_workspace) / "note.txt").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert raw.decode("utf-8") == "中文\n"

    def test_edit_ps1_adds_bom(self, tools, test_workspace):
        from pathlib import Path
        from core.tools import get_tool_by_name

        path = Path(test_workspace) / "edit_script.ps1"
        path.write_bytes("Write-Output '中文'\n".encode("utf-8"))

        edit_tool = get_tool_by_name(tools, "edit")
        result = edit_tool.execute(
            file_path="edit_script.ps1", old_text="中文", new_text="中文编辑"
        )
        assert not result.error

        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert not raw[3:].startswith(b"\xef\xbb\xbf")
        assert raw.decode("utf-8-sig") == "Write-Output '中文编辑'\n"

    def test_edit_ps1_keeps_single_bom(self, tools, test_workspace):
        from pathlib import Path
        from core.tools import get_tool_by_name

        path = Path(test_workspace) / "edit_script_bom.ps1"
        path.write_bytes(b"\xef\xbb\xbf" + "Write-Output '中文'\n".encode("utf-8"))

        edit_tool = get_tool_by_name(tools, "edit")
        result = edit_tool.execute(
            file_path="edit_script_bom.ps1", old_text="中文", new_text="中文编辑"
        )
        assert not result.error

        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert not raw[3:].startswith(b"\xef\xbb\xbf")
        assert raw.decode("utf-8-sig") == "Write-Output '中文编辑'\n"


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


class TestEditEncodingDetection:
    """edit 工具的编码探测：UTF-8 优先，非 UTF-8 按 gb18030 兜底并原样写回。

    回归背景：此前严格按 utf-8 读取，GBK 中文源码文件直接报
    "Error editing file: 'utf-8' codec can't decode..."，无法编辑。
    """

    def _edit(self, tools):
        from core.tools import get_tool_by_name
        return get_tool_by_name(tools, "edit")

    def test_gbk_file_edits_and_stays_gbk(self, tools, test_workspace):
        from pathlib import Path

        path = Path(test_workspace) / "gbk.txt"
        path.write_bytes("中文内容 old\n".encode("gb18030"))

        result = self._edit(tools).execute(
            file_path="gbk.txt", old_text="old", new_text="new"
        )

        assert not result.error
        raw = path.read_bytes()
        assert raw.decode("gb18030") == "中文内容 new\n"
        # 未被整体转码为 UTF-8
        try:
            raw.decode("utf-8")
            utf8_ok = True
        except UnicodeDecodeError:
            utf8_ok = False
        assert not utf8_ok

    def test_gbk_crlf_preserved(self, tools, test_workspace):
        from pathlib import Path

        path = Path(test_workspace) / "gbk_crlf.txt"
        path.write_bytes("第一行\r\nold 行\r\n".encode("gb18030"))

        result = self._edit(tools).execute(
            file_path="gbk_crlf.txt", old_text="old", new_text="new"
        )

        assert not result.error
        assert path.read_bytes().decode("gb18030") == "第一行\r\nnew 行\r\n"

    def test_undecodable_file_reports_clear_error(self, tools, test_workspace):
        from pathlib import Path

        path = Path(test_workspace) / "bad.bin"
        path.write_bytes(b"\xff\xfe\xff\xfe")

        result = self._edit(tools).execute(
            file_path="bad.bin", old_text="a", new_text="b"
        )

        assert result.error
        assert "UTF-8" in result.output and "GB18030" in result.output
        # 原文件不得被改写
        assert path.read_bytes() == b"\xff\xfe\xff\xfe"


class TestTruncatedToolArgs:
    """参数 JSON 被截断（_parse_error）的工具调用一律不执行。

    回归：模型一次 write 超长内容在生成途中被截断时，旧行为先走「缺少必填参数」
    再让模型原样重发；现在的约定是显式报「被截断、未执行」，提示拆小重试，
    绝不把截断的半截内容写进文件（那样文件内容不符合预期）。
    """

    def test_write_truncated_args_not_executed(self, runner, test_workspace):
        result = runner._execute_tool(
            "write",
            {"_raw": '{"file_path": "half.md", "content": "# 标题', "_parse_error": True},
            "toolu_truncated",
        )

        assert result["is_error"]
        assert "截断" in result["content"]
        assert "未执行" in result["content"]
        assert not os.path.exists(os.path.join(test_workspace, "half.md"))

    def test_legacy_raw_wrapper_reports_truncation_not_missing_params(
        self, runner, test_workspace
    ):
        """旧会话落盘的 {"_raw": ...}（无标记）也要报截断，而不是缺必填参数。"""
        from core.llm.types import ToolCallBlock

        block = ToolCallBlock(
            id="toolu_legacy", name="write",
            arguments='{"_raw": "{\\"file_path\\": \\"half.md\\"}"}',
        )
        result = runner._execute_tool(block.name, block.parse_arguments(), block.id)

        assert result["is_error"]
        assert "缺少必填参数" not in result["content"]
        assert "截断" in result["content"]
        assert not os.path.exists(os.path.join(test_workspace, "half.md"))


class TestGlobIgnoredDirs:
    """glob 在被 .gitignore 忽略的目录里应能看到文件"""

    @staticmethod
    def _make_repo(root):
        import shutil
        import subprocess

        import pytest

        if not shutil.which("git"):
            pytest.skip("git not available")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True,
                       capture_output=True)
        (root / ".gitignore").write_text("ignored_dir/\n", encoding="utf-8")
        probe = root / "ignored_dir" / "sub"
        probe.mkdir(parents=True)
        (probe / "a.md").write_text("x", encoding="utf-8")
        return probe

    def test_glob_path_inside_ignored_dir(self, tmp_path):
        """path 直接指向被忽略目录"""
        from core.tools.glob import GlobTool

        self._make_repo(tmp_path)
        result = GlobTool(cwd=str(tmp_path)).execute(
            pattern="*.md", path="ignored_dir/sub")
        assert not result.error
        assert "a.md" in result.output

    def test_glob_pattern_names_ignored_dir(self, tmp_path):
        """pattern 里显式点名被忽略目录"""
        from core.tools.glob import GlobTool

        self._make_repo(tmp_path)
        result = GlobTool(cwd=str(tmp_path)).execute(
            pattern="ignored_dir/sub/*.md")
        assert "a.md" in result.output

    def test_glob_non_ignored_dir_still_filtered(self, tmp_path):
        """.gitignore 内其它被忽略路径仍应被过滤"""
        from core.tools.glob import GlobTool

        self._make_repo(tmp_path)
        (tmp_path / ".gitignore").write_text(
            "ignored_dir/\nother/\n", encoding="utf-8")
        other = tmp_path / "other"
        other.mkdir()
        (other / "b.md").write_text("x", encoding="utf-8")
        result = GlobTool(cwd=str(tmp_path)).execute(pattern="**/*.md")
        assert "b.md" not in result.output

    def test_glob_pagination_reports_offset(self, tmp_path):
        """head_limit + offset 同时生效时，回执应报真实区间"""
        from core.tools.glob import GlobTool

        for i in range(5):
            (tmp_path / f"f{i}.md").write_text("x", encoding="utf-8")
        out = GlobTool(cwd=str(tmp_path)).execute(
            pattern="*.md", head_limit=2, offset=3).output
        assert "4–5 of 5" in out
        assert "showing first" not in out

    def test_glob_pattern_paths_use_forward_slashes(self, tmp_path):
        """pattern 入口返回 / 分隔路径，便于直接拼 bash 命令"""
        from core.tools.glob import GlobTool

        self._make_repo(tmp_path)
        out = GlobTool(cwd=str(tmp_path)).execute(
            pattern="ignored_dir/sub/*.md").output
        assert "ignored_dir/sub/a.md" in out
