"""Tests for core/tools/shared/temp.py - 临时文件管理工具。"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from core.tools.shared.temp import TempTool


class MockSessionManager:
    """模拟 session_manager，提供 session_id。"""

    def __init__(self, session_id: str = "test-session-123"):
        self.session_id = session_id


@pytest.fixture
def tmp_env(tmp_path):
    """将 CILI_TMP 环境变量指向测试临时目录。"""
    test_tmp = str(tmp_path / "cili_tmp")
    os.makedirs(test_tmp, exist_ok=True)
    old = os.environ.get("CILI_TMP")
    os.environ["CILI_TMP"] = test_tmp
    yield test_tmp
    # 还原
    if old is not None:
        os.environ["CILI_TMP"] = old
    else:
        os.environ.pop("CILI_TMP", None)


class TestTempToolCreateFile:
    """create_file action 测试。"""

    def test_create_file_basic(self, test_workspace, tmp_env):
        """测试创建基本文件。"""
        tool = TempTool(
            cwd=test_workspace,
            workspace_uuid="test-uuid",
            session_manager=MockSessionManager("session-abc"),
        )
        result = tool.execute(action="create_file", name="test.txt", content="hello world")

        assert not result.is_error
        assert "Created temp file" in result.output
        assert "test.txt" in result.output

    def test_create_file_empty_content(self, test_workspace, tmp_env):
        """测试创建空文件。"""
        tool = TempTool(
            cwd=test_workspace,
            workspace_uuid="test-uuid",
            session_manager=MockSessionManager("session-empty"),
        )
        result = tool.execute(action="create_file", name="empty.txt")

        assert not result.is_error
        assert "Created temp file" in result.output

    def test_create_file_requires_name(self, test_workspace, tmp_env):
        """测试 create_file 需要 name 参数。"""
        tool = TempTool(cwd=test_workspace, workspace_uuid="test-uuid")
        result = tool.execute(action="create_file")

        assert result.is_error
        assert "name" in result.output.lower()


class TestTempToolCreateDir:
    """create_dir action 测试。"""

    def test_create_dir_basic(self, test_workspace, tmp_env):
        """测试创建目录。"""
        tool = TempTool(
            cwd=test_workspace,
            workspace_uuid="test-uuid",
            session_manager=MockSessionManager("session-dir"),
        )
        result = tool.execute(action="create_dir", name="downloads")

        assert not result.is_error
        assert "Created temp directory" in result.output

    def test_create_dir_requires_name(self, test_workspace, tmp_env):
        """测试 create_dir 需要 name 参数。"""
        tool = TempTool(cwd=test_workspace, workspace_uuid="test-uuid")
        result = tool.execute(action="create_dir")

        assert result.is_error
        assert "name" in result.output.lower()


class TestTempToolList:
    """list action 测试。"""

    def test_list_empty(self, test_workspace, tmp_env):
        """测试空目录列表。"""
        tool = TempTool(
            cwd=test_workspace,
            workspace_uuid="test-uuid",
            session_manager=MockSessionManager("session-list-empty"),
        )
        result = tool.execute(action="list")

        assert not result.is_error
        assert "No temp files" in result.output

    def test_list_with_files(self, test_workspace, tmp_env):
        """测试有文件时的列表。"""
        tool = TempTool(
            cwd=test_workspace,
            workspace_uuid="test-uuid",
            session_manager=MockSessionManager("session-list-files"),
        )
        # 创建一些文件
        tool.execute(action="create_file", name="file1.txt", content="content1")
        tool.execute(action="create_dir", name="subdir")

        result = tool.execute(action="list")

        assert not result.is_error
        assert "file1.txt" in result.output
        assert "subdir" in result.output
        assert "FILE" in result.output
        assert "DIR" in result.output


class TestTempToolCleanup:
    """cleanup action 测试。"""

    def test_cleanup_removes_directory(self, test_workspace, tmp_env):
        """测试 cleanup 删除整个目录。"""
        tool = TempTool(
            cwd=test_workspace,
            workspace_uuid="test-uuid",
            session_manager=MockSessionManager("session-cleanup"),
        )
        # 创建文件
        tool.execute(action="create_file", name="temp.txt", content="data")

        # 确认文件存在
        list_result = tool.execute(action="list")
        assert "temp.txt" in list_result.output

        # 清理
        cleanup_result = tool.execute(action="cleanup")
        assert not cleanup_result.is_error
        assert "Cleaned up" in cleanup_result.output

        # 确认已删除
        list_result2 = tool.execute(action="list")
        assert "No temp files" in list_result2.output

    def test_cleanup_empty_directory(self, test_workspace, tmp_env):
        """测试清理空的目录（目录会被 _get_temp_dir 自动创建）。"""
        tool = TempTool(
            cwd=test_workspace,
            workspace_uuid="test-uuid",
            session_manager=MockSessionManager("session-cleanup-empty"),
        )
        result = tool.execute(action="cleanup")

        # 目录会被自动创建，然后被清理
        assert not result.is_error
        assert "Cleaned up" in result.output


class TestTempToolNoSessionManager:
    """没有 session_manager 时的行为。"""

    def test_no_session_manager(self, test_workspace, tmp_env):
        """测试没有 session_manager 时使用 no-session。"""
        tool = TempTool(cwd=test_workspace, workspace_uuid="test-uuid", session_manager=None)
        result = tool.execute(action="create_file", name="test.txt", content="data")

        assert not result.is_error
        assert "no-session" in result.output

        # 清理
        tool.execute(action="cleanup")


class TestTempToolIsolation:
    """Session 隔离测试。"""

    def test_different_sessions_isolated(self, test_workspace, tmp_env):
        """测试不同 session 的临时文件互相隔离。"""
        tool1 = TempTool(
            cwd=test_workspace,
            workspace_uuid="test-uuid",
            session_manager=MockSessionManager("session-1"),
        )
        tool2 = TempTool(
            cwd=test_workspace,
            workspace_uuid="test-uuid",
            session_manager=MockSessionManager("session-2"),
        )

        # 在 session-1 创建文件
        tool1.execute(action="create_file", name="file1.txt", content="data1")

        # 在 session-2 创建文件
        tool2.execute(action="create_file", name="file2.txt", content="data2")

        # 验证隔离
        list1 = tool1.execute(action="list")
        list2 = tool2.execute(action="list")

        assert "file1.txt" in list1.output
        assert "file2.txt" not in list1.output
        assert "file2.txt" in list2.output
        assert "file1.txt" not in list2.output

        # 清理
        tool1.execute(action="cleanup")
        tool2.execute(action="cleanup")


class TestTempToolUnifiedDir:
    """统一临时目录测试。"""

    def test_uses_cili_tmp_env(self, test_workspace, tmp_env):
        """测试使用 CILI_TMP 环境变量作为基础目录。"""
        tool = TempTool(
            cwd=test_workspace,
            workspace_uuid="any-uuid",
            session_manager=MockSessionManager("session-env"),
        )
        result = tool.execute(action="create_file", name="test.txt", content="data")
        assert not result.is_error
        # 文件路径应在 CILI_TMP 目录下
        assert tmp_env in result.output

        # 清理
        tool.execute(action="cleanup")

    def test_workspace_uuid_ignored(self, test_workspace, tmp_env):
        """测试不同 workspace_uuid 都使用同一个 data/tmp 目录。"""
        tool1 = TempTool(
            cwd=test_workspace,
            workspace_uuid="uuid-1",
            session_manager=MockSessionManager("session-same"),
        )
        tool2 = TempTool(
            cwd=test_workspace,
            workspace_uuid="uuid-2",
            session_manager=MockSessionManager("session-same"),
        )
        # 两个不同 workspace 的 tool 使用相同的 session 目录
        result1 = tool1.execute(action="create_file", name="a.txt", content="1")
        result2 = tool2.execute(action="create_file", name="b.txt", content="2")
        # 路径中不应包含 workspace uuid
        assert "uuid-1" not in result1.output
        assert "uuid-2" not in result2.output

        # 清理
        tool1.execute(action="cleanup")
