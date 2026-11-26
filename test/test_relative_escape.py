# -*- coding: utf-8 -*-
"""相对路径越界与临时目录默认落点测试。"""

import os

from core.security.shell_targets import find_relative_escapes
from core.tools.base import ToolResult
from core.tools.bash import BashTool
from core.tools.browser import BrowserTool
from core.tools.edit import EditTool
from core.tools.glob import GlobTool
from core.tools.grep import GrepTool
from core.tools.read import ReadTool
from core.tools.write import WriteTool


def _outside_file(test_workspace: str, name: str = "cili_parent_probe.txt") -> str:
    return os.path.join(os.path.dirname(test_workspace), name)


class TestFileToolEscape:
    def test_read_rejects_parent_escape(self, test_workspace):
        outside = _outside_file(test_workspace)
        with open(outside, "w", encoding="utf-8") as f:
            f.write("secret")
        try:
            tool = ReadTool(cwd=test_workspace, workspace_uuid="test-workspace")
            result = tool.execute(file_path="../cili_parent_probe.txt")
            assert result.error
            assert "工作区边界" in result.output
        finally:
            os.unlink(outside)

    def test_read_allows_dotdot_inside_workspace(self, test_workspace):
        os.makedirs(os.path.join(test_workspace, "sub"), exist_ok=True)
        target = os.path.join(test_workspace, "inside.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("inside")
        tool = ReadTool(cwd=test_workspace, workspace_uuid="test-workspace")
        result = tool.execute(file_path="sub/../inside.txt")
        assert not result.error
        assert "inside" in result.output

    def test_write_and_edit_reject_parent_escape(self, test_workspace):
        outside = _outside_file(test_workspace, "cili_write_probe.txt")
        try:
            write = WriteTool(cwd=test_workspace, workspace_uuid="test-workspace")
            result = write.execute(file_path="../cili_write_probe.txt", content="x")
            assert result.error
            assert "工作区边界" in result.output
            assert not os.path.exists(outside)

            edit = EditTool(cwd=test_workspace, workspace_uuid="test-workspace")
            result = edit.execute(
                file_path="../cili_write_probe.txt", old_text="a", new_text="b")
            assert result.error
            assert "工作区边界" in result.output
        finally:
            if os.path.exists(outside):
                os.unlink(outside)

    def test_glob_and_grep_reject_parent_escape(self, test_workspace):
        glob_tool = GlobTool(cwd=test_workspace, workspace_uuid="test-workspace")
        result = glob_tool.execute(pattern="*", path="../")
        assert result.error
        assert "工作区边界" in result.output

        grep_tool = GrepTool(cwd=test_workspace, workspace_uuid="test-workspace")
        result = grep_tool.execute(pattern="x", path="../")
        assert result.error
        assert "工作区边界" in result.output


class TestShellEscape:
    def test_scanner_rejects_read_escape(self, tmp_path):
        assert find_relative_escapes(
            "cat ../secret", "bash", str(tmp_path), str(tmp_path)) == ["../secret"]

    def test_scanner_tracks_cd(self, tmp_path):
        os.makedirs(tmp_path / "sub")
        assert find_relative_escapes(
            "cd sub && cat ../file", "bash", str(tmp_path), str(tmp_path)) == []
        assert find_relative_escapes(
            "cd .. && ls", "bash", str(tmp_path), str(tmp_path)) == [".."]

    def test_scanner_skips_literals_and_regex(self, tmp_path):
        assert find_relative_escapes(
            "echo ../x", "bash", str(tmp_path), str(tmp_path)) == []
        assert find_relative_escapes(
            "grep .. file", "bash", str(tmp_path), str(tmp_path)) == []
        assert find_relative_escapes(
            "grep -r pattern ../dir", "bash", str(tmp_path), str(tmp_path)) == ["../dir"]

    def test_bash_rejects_read_escape_before_execution(self, test_workspace):
        outside = _outside_file(test_workspace)
        with open(outside, "w", encoding="utf-8") as f:
            f.write("secret")
        try:
            tool = BashTool(cwd=test_workspace, workspace_uuid="test-workspace")
            result = tool.execute(command="cat ../cili_parent_probe.txt")
            assert result.error
            assert "工作区边界" in result.output
            assert "secret" not in result.output
        finally:
            os.unlink(outside)


class TestBrowserDownloadDefault:
    def test_default_save_dir_is_workspace_tmp(self, test_workspace, monkeypatch):
        calls = {}

        class _FakeService:
            def is_running(self):
                return True

            def download(self, **kwargs):
                calls.update(kwargs)
                return ToolResult("ok")

        monkeypatch.setattr("core.browser_service.get_service", lambda: _FakeService())
        tool = BrowserTool(cwd=test_workspace, workspace_uuid="test-workspace")
        result = tool.execute(action="download", url="https://example.com/file.bin")
        assert not result.error
        assert calls["save_path"] is None
        assert calls["save_dir"] == os.path.join(
            test_workspace, ".cili", "tmp", "downloads")
        assert os.path.isdir(calls["save_dir"])

    def test_save_path_escape_rejected(self, test_workspace, monkeypatch):
        calls = {}

        class _FakeService:
            def is_running(self):
                return True

            def download(self, **kwargs):
                calls.update(kwargs)
                return ToolResult("ok")

        monkeypatch.setattr("core.browser_service.get_service", lambda: _FakeService())
        tool = BrowserTool(cwd=test_workspace, workspace_uuid="test-workspace")
        result = tool.execute(
            action="download", url="https://example.com/file.bin",
            save_path="../file.bin")
        assert result.error
        assert "工作区边界" in result.output
        assert calls == {}

    def test_screenshot_and_pdf_paths_reject_escape(self, test_workspace, monkeypatch):
        class _FakeService:
            def is_running(self):
                return True

            def screenshot(self, *args, **kwargs):
                raise AssertionError("should not reach screenshot")

            def save_pdf(self, *args, **kwargs):
                raise AssertionError("should not reach save_pdf")

        monkeypatch.setattr("core.browser_service.get_service", lambda: _FakeService())
        tool = BrowserTool(cwd=test_workspace, workspace_uuid="test-workspace")
        for action, key in (("screenshot", "screenshot_path"), ("save_pdf", "pdf_path")):
            result = tool.execute(action=action, **{key: "../escape.bin"})
            assert result.error, action
            assert "工作区边界" in result.output
