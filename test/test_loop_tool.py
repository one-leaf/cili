"""Tests for loop tool (core/tools/shared/loop.py)."""

import json
import tempfile
from pathlib import Path

import pytest


def _write_items_file(tmp_dir: str, items: list[str]) -> str:
    """Helper: write items to a temp file (one per line) and return the path."""
    path = Path(tmp_dir) / "items.txt"
    path.write_text("\n".join(items), encoding="utf-8")
    return str(path)


class TestLoopTool:
    """Test LoopTool basic functionality."""

    @pytest.fixture
    def temp_loop_state(self):
        """临时替换 loop 状态目录"""
        import core.tools.shared.loop as loop_module
        original_dir = loop_module.LOOP_STATE_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_module.LOOP_STATE_DIR = Path(temp_dir) / "state"
            yield temp_dir
            loop_module.LOOP_STATE_DIR = original_dir

    def test_next_returns_first_item(self, temp_loop_state):
        """next 从文件加载并返回第一个 pending 项（附带进度）"""
        from core.tools.shared.loop import LoopTool

        items_file = _write_items_file(temp_loop_state, ["file1.md", "file2.md"])
        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")
        result = tool.execute(action="next", source_file=items_file)

        # 新格式：多行文本，包含进度和当前项
        assert "进度: 0/2 已完成" in result.output
        assert "2 待处理" in result.output
        assert "当前项: file1.md" in result.output

    def test_next_returns_null_when_no_pending(self, temp_loop_state):
        """无 pending 项时 next 返回完成提示"""
        from core.tools.shared.loop import LoopTool

        items_file = _write_items_file(temp_loop_state, ["file1.md"])
        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")
        tool.execute(action="next", source_file=items_file)  # 加载
        tool.execute(action="done", source_file=items_file, item="file1.md")

        result = tool.execute(action="next", source_file=items_file)

        # 新格式：所有项处理完毕的提示
        assert "所有项已处理完毕" in result.output
        assert "完成: 1" in result.output

    def test_next_auto_syncs_new_items(self, temp_loop_state):
        """next 自动同步文件中的新增项"""
        from core.tools.shared.loop import LoopTool

        items_file = _write_items_file(temp_loop_state, ["file1.md", "file2.md"])
        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")
        tool.execute(action="next", source_file=items_file)  # 加载 2 项

        # 更新文件，新增一项
        _write_items_file(temp_loop_state, ["file1.md", "file2.md", "file3.md"])

        # 再次 next 触发同步
        tool.execute(action="next", source_file=items_file)

        # 检查总数是 3
        result = tool.execute(action="status", source_file=items_file)
        data = json.loads(result.output)
        assert data["total"] == 3

    def test_done_marks_item_completed(self, temp_loop_state):
        """done 标记项为已完成"""
        from core.tools.shared.loop import LoopTool

        items_file = _write_items_file(temp_loop_state, ["file1.md", "file2.md"])
        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")
        tool.execute(action="next", source_file=items_file)

        result = tool.execute(action="done", source_file=items_file, item="file1.md")
        data = json.loads(result.output)

        assert data["done"] == 1
        assert data["pending"] == 1
        assert data["failed"] == 0

    def test_done_error_missing_item(self, temp_loop_state):
        """done 缺少 item 参数时返回错误"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")
        result = tool.execute(action="done", source_file="some_file.txt")

        assert result.is_error

    def test_done_error_item_not_found(self, temp_loop_state):
        """done 项不存在时返回错误"""
        from core.tools.shared.loop import LoopTool

        items_file = _write_items_file(temp_loop_state, ["file1.md"])
        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")
        tool.execute(action="next", source_file=items_file)

        result = tool.execute(action="done", source_file=items_file, item="nonexistent.md")
        assert result.is_error

    def test_fail_marks_item_failed(self, temp_loop_state):
        """fail 标记项为失败"""
        from core.tools.shared.loop import LoopTool

        items_file = _write_items_file(temp_loop_state, ["file1.md", "file2.md"])
        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")
        tool.execute(action="next", source_file=items_file)

        result = tool.execute(action="fail", source_file=items_file, item="file1.md", error="编码错误")
        data = json.loads(result.output)

        assert data["done"] == 0
        assert data["pending"] == 1
        assert data["failed"] == 1

    def test_status_returns_statistics(self, temp_loop_state):
        """status 返回进度统计"""
        from core.tools.shared.loop import LoopTool

        items_file = _write_items_file(temp_loop_state, ["file1.md", "file2.md", "file3.md"])
        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")
        tool.execute(action="next", source_file=items_file)  # 加载
        tool.execute(action="done", source_file=items_file, item="file1.md")
        tool.execute(action="fail", source_file=items_file, item="file2.md", error="test error")

        result = tool.execute(action="status", source_file=items_file)
        data = json.loads(result.output)

        assert data["total"] == 3
        assert data["done"] == 1
        assert data["pending"] == 1
        assert data["failed"] == 1

    def test_source_file_required(self, temp_loop_state):
        """source_file 是必需参数"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")
        result = tool.execute(action="status")

        assert result.is_error

    def test_next_error_file_not_found(self, temp_loop_state):
        """next 文件不存在且无状态时返回错误"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")
        result = tool.execute(action="next", source_file="/nonexistent/file.txt")

        assert result.is_error

    def test_same_file_same_task(self, temp_loop_state):
        """同一文件路径标识同一任务"""
        from core.tools.shared.loop import LoopTool

        items_file = _write_items_file(temp_loop_state, ["file1.md", "file2.md"])
        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")

        tool.execute(action="next", source_file=items_file)
        tool.execute(action="done", source_file=items_file, item="file1.md")

        # 用同一 source_file 查看状态
        result = tool.execute(action="status", source_file=items_file)
        data = json.loads(result.output)

        assert data["total"] == 2
        assert data["done"] == 1
        assert data["pending"] == 1

    def test_different_files_different_tasks(self, temp_loop_state):
        """不同文件路径对应不同任务"""
        from core.tools.shared.loop import LoopTool

        file1 = str(Path(temp_loop_state) / "list1.txt")
        file2 = str(Path(temp_loop_state) / "list2.txt")
        Path(temp_loop_state, "list1.txt").write_text("item_a\n", encoding="utf-8")
        Path(temp_loop_state, "list2.txt").write_text("item_b\n", encoding="utf-8")

        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")
        tool.execute(action="next", source_file=file1)
        tool.execute(action="next", source_file=file2)

        # 两个任务独立
        r1 = tool.execute(action="status", source_file=file1)
        r2 = tool.execute(action="status", source_file=file2)
        d1 = json.loads(r1.output)
        d2 = json.loads(r2.output)

        assert d1["total"] == 1
        assert d2["total"] == 1

    def test_empty_lines_ignored(self, temp_loop_state):
        """空行被忽略"""
        from core.tools.shared.loop import LoopTool

        file_path = Path(temp_loop_state) / "items_blanks.txt"
        file_path.write_text("file1.md\n\n  \nfile2.md\n\n", encoding="utf-8")

        tool = LoopTool(cwd=temp_loop_state, workspace_uuid="test")
        result = tool.execute(action="next", source_file=str(file_path))

        # 新格式：多行文本
        assert "当前项: file1.md" in result.output
        assert "进度: 0/2 已完成" in result.output

        # 检查只有 2 项
        result = tool.execute(action="status", source_file=str(file_path))
        data = json.loads(result.output)
        assert data["total"] == 2
