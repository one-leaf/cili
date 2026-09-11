"""Tests for core/tools/todo.py — TodoWrite 工具（A45：零覆盖补强）。

覆盖：整表替换语义、三态校验、重复 content 拒绝、每会话独立存储、
验证提示（verification nudge）、metadata 旧格式迁移回退。
"""

import pytest

from core.tools.todo import (
    TodoWriteTool,
    read_todos,
    write_todos,
    get_todos_from_session,
    get_todo_file_path,
)


class _FakeSession:
    """最小 session_manager 桩：只暴露 todo 用到的字段。"""

    def __init__(self, session_id: str = "sess-test"):
        self.session_id = session_id
        self.metadata: dict = {}


@pytest.fixture
def todo_dir(tmp_path, monkeypatch):
    """把 TODO_DIR 指到临时目录，避免污染项目 data。"""
    import core.tools.todo as todo_mod
    monkeypatch.setattr(todo_mod, "TODO_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def tool():
    return TodoWriteTool(cwd=".", workspace_uuid="test-ws", session_manager=_FakeSession())


class TestValidation:
    def test_todos_must_be_array(self, tool):
        result = tool.execute(todos="not a list")
        assert result.error
        assert "array" in result.output

    def test_item_must_be_object(self, tool):
        result = tool.execute(todos=["just a string"])
        assert result.error
        assert "must be an object" in result.output

    def test_content_non_empty(self, tool):
        result = tool.execute(todos=[{"content": "   ", "status": "pending"}])
        assert result.error
        assert "non-empty" in result.output

    def test_invalid_status(self, tool):
        result = tool.execute(todos=[{"content": "写代码", "status": "done"}])
        assert result.error
        assert "status" in result.output

    def test_duplicate_content_rejected(self, tool):
        result = tool.execute(todos=[
            {"content": "跑测试", "status": "pending"},
            {"content": "跑测试", "status": "pending"},
        ])
        assert result.error
        assert "duplicate" in result.output.lower()


class TestStorage:
    def test_write_read_roundtrip(self, tool, todo_dir):
        result = tool.execute(todos=[{"content": "重构模块", "status": "in_progress"}])
        assert not result.error
        stored = read_todos("sess-test")
        assert stored == [{"content": "重构模块", "status": "in_progress"}]

    def test_whole_list_replacement(self, tool, todo_dir):
        """整表替换：第二次调用完全覆盖第一次，不做增量合并。"""
        tool.execute(todos=[{"content": "任务A", "status": "pending"}])
        tool.execute(todos=[{"content": "任务B", "status": "completed"}])
        stored = read_todos("sess-test")
        assert [t["content"] for t in stored] == ["任务B"]

    def test_counts_in_meta(self, tool, todo_dir):
        result = tool.execute(todos=[
            {"content": "a", "status": "pending"},
            {"content": "b", "status": "in_progress"},
            {"content": "c", "status": "completed"},
        ])
        assert result.meta["counts"] == {
            "total": 3, "pending": 1, "in_progress": 1, "completed": 1,
        }

    def test_per_session_isolation(self, todo_dir):
        """不同 session 写入互不影响，各存各的文件。"""
        a = TodoWriteTool(cwd=".", workspace_uuid="test-ws",
                          session_manager=_FakeSession("sess-a"))
        b = TodoWriteTool(cwd=".", workspace_uuid="test-ws",
                          session_manager=_FakeSession("sess-b"))
        a.execute(todos=[{"content": "a 的任务", "status": "pending"}])
        b.execute(todos=[{"content": "b 的任务", "status": "pending"}])
        b.execute(todos=[{"content": "b 的任务2", "status": "pending"}])
        # a 只写自己的文件；b 第二次调用整表替换了自己的列表
        assert [t["content"] for t in read_todos("sess-a")] == ["a 的任务"]
        assert [t["content"] for t in read_todos("sess-b")] == ["b 的任务2"]

    def test_removes_legacy_metadata_todos(self, tool, todo_dir):
        """迁移旧格式：metadata.todos 被清掉，不再双写。"""
        tool.session_manager.metadata["todos"] = [{"task": "旧任务", "status": "pending"}]
        tool.execute(todos=[{"content": "新任务", "status": "pending"}])
        assert "todos" not in tool.session_manager.metadata

    def test_get_todo_file_path(self, todo_dir):
        assert get_todo_file_path("abc").name == "abc.json"


class TestVerificationNudge:
    def test_nudge_when_3plus_completed_no_verify(self, tool):
        """3+ 全 completed 且无 verify/test 关键词，此前有未完成任务 → 提示。"""
        old = [{"content": "x", "status": "in_progress"}]
        new = [
            {"content": "写报告", "status": "completed"},
            {"content": "改配置", "status": "completed"},
            {"content": "发邮件", "status": "completed"},
        ]
        assert tool._check_verification_nudge(old, new) is True

    def test_no_nudge_under_3_tasks(self, tool):
        old = [{"content": "x", "status": "in_progress"}]
        new = [{"content": "写报告", "status": "completed"}]
        assert tool._check_verification_nudge(old, new) is False

    def test_no_nudge_with_test_keyword(self, tool):
        old = [{"content": "x", "status": "in_progress"}]
        new = [
            {"content": "write code", "status": "completed"},
            {"content": "run tests", "status": "completed"},
            {"content": "refactor", "status": "completed"},
        ]
        assert tool._check_verification_nudge(old, new) is False

    def test_no_nudge_when_previously_all_done(self, tool):
        old = [{"content": "a", "status": "completed"}]
        new = [
            {"content": "a", "status": "completed"},
            {"content": "b", "status": "completed"},
            {"content": "c", "status": "completed"},
        ]
        assert tool._check_verification_nudge(old, new) is False


class TestGetTodosFromSession:
    def test_new_format_file(self, todo_dir):
        write_todos("sess-1", [{"content": "文件里的", "status": "pending"}])
        sm = _FakeSession("sess-1")
        assert get_todos_from_session(sm) == [{"content": "文件里的", "status": "pending"}]

    def test_migrates_metadata_fallback(self, todo_dir):
        """旧格式：metadata.todos 有值且无独立文件 → 迁到文件并返回。"""
        sm = _FakeSession("sess-2")
        sm.metadata["todos"] = [{"task": "旧格式", "status": "pending"}]
        todos = get_todos_from_session(sm)
        assert todos == [{"task": "旧格式", "status": "pending"}]
        # 已迁移到独立文件，metadata 清空
        assert "todos" not in sm.metadata
        assert read_todos("sess-2") == [{"task": "旧格式", "status": "pending"}]

    def test_no_session_returns_none(self):
        assert get_todos_from_session(None) is None
