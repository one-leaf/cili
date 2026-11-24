"""send_user_file 工具测试：整批校验、接口档案约束、outbox 登记与重复提交。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.outbox as outbox
from core.interface_profile import InterfaceProfile
from core.tools.send_user_file import SendUserFileTool

WS = "ws-file"


@pytest.fixture(autouse=True)
def _clear_outbox():
    outbox.clear()
    yield
    outbox.clear()


def _tool(tmp_path, *, profile=None):
    tool = SendUserFileTool(cwd=str(tmp_path), workspace_uuid=WS)
    tool.session = SimpleNamespace(session_id="s1")
    if profile is not None:
        tool.set_interface_profile(profile)
    return tool


def _file(tmp_path, name="a.txt", content=b"hello"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


class TestValidation:
    def test_empty_paths_rejected(self, tmp_path):
        result = _tool(tmp_path).execute(paths=[])
        assert result.error and "paths is required" in result.output

    def test_string_path_tolerated(self, tmp_path):
        _file(tmp_path)
        result = _tool(tmp_path).execute(paths="a.txt")
        assert not result.error
        assert "Queued 1 file" in result.output

    def test_outside_workspace_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        result = _tool(tmp_path).execute(paths=[str(outside)])
        assert result.error
        assert "outside the workspace" in result.output

    def test_missing_file_rejected(self, tmp_path):
        result = _tool(tmp_path).execute(paths=["nope.txt"])
        assert result.error and "not found" in result.output

    def test_directory_rejected(self, tmp_path):
        (tmp_path / "dir").mkdir()
        result = _tool(tmp_path).execute(paths=["dir"])
        assert result.error and "not a file" in result.output

    def test_batch_all_or_nothing(self, tmp_path):
        _file(tmp_path, "ok.txt")
        result = _tool(tmp_path).execute(paths=["ok.txt", "missing.txt"])
        assert result.error
        assert outbox.pending_count(f"{WS}:s1") == 0

    def test_profile_size_limit_enforced(self, tmp_path):
        _file(tmp_path, "big.bin", b"x" * (2 * 1024 * 1024))
        profile = InterfaceProfile(name="qq", label="QQ", file_size_limit_mb=1)
        result = _tool(tmp_path, profile=profile).execute(paths=["big.bin"])
        assert result.error and "too large" in result.output

    def test_no_session_rejected(self, tmp_path):
        _file(tmp_path)
        tool = _tool(tmp_path)
        tool.session = None
        result = tool.execute(paths=["a.txt"])
        assert result.error and "cannot determine the current session" in result.output


class TestQueueing:
    def test_queues_and_reports(self, tmp_path):
        _file(tmp_path)
        result = _tool(tmp_path).execute(paths=["a.txt"], note="报告")
        assert not result.error
        assert "Queued 1 file" in result.output and "报告" in result.output
        assert outbox.pending_count(f"{WS}:s1") == 1

    def test_duplicate_is_not_error(self, tmp_path):
        _file(tmp_path)
        tool = _tool(tmp_path)
        tool.execute(paths=["a.txt"])
        result = tool.execute(paths=["a.txt"])
        assert not result.error
        assert "already queued" in result.output

    def test_per_turn_limit_overflow(self, tmp_path):
        _file(tmp_path, "a.txt")
        _file(tmp_path, "b.txt")
        profile = InterfaceProfile(name="qq", label="QQ", max_files_per_turn=1)
        result = _tool(tmp_path, profile=profile).execute(paths=["a.txt", "b.txt"])
        assert result.error and "at most 1 per turn" in result.output
