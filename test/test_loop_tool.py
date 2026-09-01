"""Tests for loop tool (core/tools/shared/loop.py)."""

import json
import tempfile
from pathlib import Path

import pytest


class TestLoopTool:
    """Test LoopTool basic functionality."""

    @pytest.fixture
    def temp_loop_state(self):
        """临时替换 loop 状态目录"""
        import core.tools.shared.loop as loop_module
        original_dir = loop_module.LOOP_STATE_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_module.LOOP_STATE_DIR = Path(temp_dir)
            yield temp_dir
            loop_module.LOOP_STATE_DIR = original_dir

    def test_sync_new_items(self, temp_loop_state):
        """sync 添加新项为 pending"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test")
        result = tool.execute(action="sync", task_id="test-sync", items=["file1.md", "file2.md"])
        data = json.loads(result.output)

        assert data["added"] == 2
        assert data["total"] == 2
        assert data["pending"] == 2
        assert data["done"] == 0
        assert data["failed"] == 0

    def test_sync_idempotent(self, temp_loop_state):
        """sync 幂等：已存在的项不重复添加"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test")

        # 第一次 sync
        tool.execute(action="sync", task_id="test-idem", items=["file1.md", "file2.md"])

        # 第二次 sync 相同列表
        result = tool.execute(action="sync", task_id="test-idem", items=["file1.md", "file2.md"])
        data = json.loads(result.output)

        assert data["added"] == 0  # 无新增
        assert data["total"] == 2

    def test_sync_adds_only_new_items(self, temp_loop_state):
        """sync 只追加新项"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test")

        # 第一次 sync
        tool.execute(action="sync", task_id="test-add-new", items=["file1.md", "file2.md"])

        # 第二次 sync 包含新项
        result = tool.execute(action="sync", task_id="test-add-new", items=["file1.md", "file2.md", "file3.md"])
        data = json.loads(result.output)

        assert data["added"] == 1
        assert data["total"] == 3

    def test_next_returns_first_pending(self, temp_loop_state):
        """next 返回第一个 pending 项"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test")
        tool.execute(action="sync", task_id="test-next", items=["file1.md", "file2.md"])

        result = tool.execute(action="next", task_id="test-next")
        data = json.loads(result.output)

        assert data["item"] == "file1.md"

    def test_next_returns_null_when_no_pending(self, temp_loop_state):
        """无 pending 项时 next 返回 null"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test")
        tool.execute(action="sync", task_id="test-next-null", items=["file1.md"])
        tool.execute(action="done", task_id="test-next-null", item="file1.md")

        result = tool.execute(action="next", task_id="test-next-null")
        data = json.loads(result.output)

        assert data["item"] is None

    def test_done_marks_item_completed(self, temp_loop_state):
        """done 标记项为已完成"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test")
        tool.execute(action="sync", task_id="test-done", items=["file1.md", "file2.md"])

        result = tool.execute(action="done", task_id="test-done", item="file1.md")
        data = json.loads(result.output)

        assert data["done"] == 1
        assert data["pending"] == 1
        assert data["failed"] == 0

    def test_done_error_missing_item(self, temp_loop_state):
        """done 缺少 item 参数时返回错误"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test")
        result = tool.execute(action="done", task_id="test-done-missing")

        assert result.is_error

    def test_done_error_item_not_found(self, temp_loop_state):
        """done 项不存在时返回错误"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test")
        tool.execute(action="sync", task_id="test-done-notfound", items=["file1.md"])

        result = tool.execute(action="done", task_id="test-done-notfound", item="nonexistent.md")
        assert result.is_error

    def test_fail_marks_item_failed(self, temp_loop_state):
        """fail 标记项为失败"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test")
        tool.execute(action="sync", task_id="test-fail", items=["file1.md", "file2.md"])

        result = tool.execute(action="fail", task_id="test-fail", item="file1.md", error="编码错误")
        data = json.loads(result.output)

        assert data["done"] == 0
        assert data["pending"] == 1
        assert data["failed"] == 1

    def test_status_returns_statistics(self, temp_loop_state):
        """status 返回进度统计"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test")
        tool.execute(action="sync", task_id="test-status", items=["file1.md", "file2.md", "file3.md"])
        tool.execute(action="done", task_id="test-status", item="file1.md")
        tool.execute(action="fail", task_id="test-status", item="file2.md", error="test error")

        result = tool.execute(action="status", task_id="test-status")
        data = json.loads(result.output)

        assert data["total"] == 3
        assert data["done"] == 1
        assert data["pending"] == 1
        assert data["failed"] == 1

    def test_task_id_required_without_cron_context(self, temp_loop_state):
        """无 cron_task_id 时必须提供 task_id"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test")  # 无 cron_task_id
        result = tool.execute(action="status")  # 无 task_id

        assert result.is_error

    def test_cron_task_id_auto_bind(self, temp_loop_state):
        """有 cron_task_id 时自动绑定"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test", cron_task_id="my-cron-task")
        tool.execute(action="sync", items=["file1.md"])

        result = tool.execute(action="status")
        data = json.loads(result.output)

        assert data["total"] == 1


class TestLoopToolCronIntegration:
    """Test LoopTool cron integration."""

    @pytest.fixture
    def temp_dirs(self):
        """临时替换 loop 和 cron 状态目录"""
        import core.tools.shared.loop as loop_module
        import core.cron as cron_module

        original_loop_dir = loop_module.LOOP_STATE_DIR
        original_cron_dir = cron_module.CRON_STATE_DIR

        with tempfile.TemporaryDirectory() as temp_dir:
            loop_module.LOOP_STATE_DIR = Path(temp_dir) / "loop"
            cron_module.CRON_STATE_DIR = Path(temp_dir) / "cron"
            yield temp_dir
            loop_module.LOOP_STATE_DIR = original_loop_dir
            cron_module.CRON_STATE_DIR = original_cron_dir

    def test_sync_updates_cron_remaining(self, temp_dirs):
        """sync 时同步 cron remaining 计数器"""
        import core.cron as cron_module
        from core.tools.shared.loop import LoopTool

        # 创建 cron state 文件
        cron_state_path = cron_module.CRON_STATE_DIR / "my-task.json"
        cron_state_path.parent.mkdir(parents=True, exist_ok=True)
        cron_state_path.write_text(json.dumps({"remaining": 9999}))

        # 创建带 cron_task_id 的 tool
        tool = LoopTool(cwd=".", workspace_uuid="test", cron_task_id="my-task")
        tool.execute(action="sync", items=["file1.md", "file2.md", "file3.md"])

        # 检查 cron remaining 已更新
        with open(cron_state_path, "r", encoding="utf-8") as f:
            cron_state = json.load(f)

        # remaining = pending_count + 1 = 3 + 1 = 4
        assert cron_state["remaining"] == 4

    def test_sync_after_done_updates_cron_remaining(self, temp_dirs):
        """done 后 sync 更新 cron remaining"""
        import core.cron as cron_module
        from core.tools.shared.loop import LoopTool

        # 创建 cron state 文件
        cron_state_path = cron_module.CRON_STATE_DIR / "my-task-2.json"
        cron_state_path.parent.mkdir(parents=True, exist_ok=True)
        cron_state_path.write_text(json.dumps({"remaining": 100}))

        tool = LoopTool(cwd=".", workspace_uuid="test", cron_task_id="my-task-2")

        # sync 3 项
        tool.execute(action="sync", items=["file1.md", "file2.md", "file3.md"])

        # done 1 项
        tool.execute(action="done", item="file1.md")

        # 重新 sync（模拟下次 cron 触发）
        tool.execute(action="sync", items=["file1.md", "file2.md", "file3.md"])

        # 检查 cron remaining 已更新
        with open(cron_state_path, "r", encoding="utf-8") as f:
            cron_state = json.load(f)

        # pending = 2, remaining = 2 + 1 = 3
        assert cron_state["remaining"] == 3

    def test_sync_no_cron_state_file(self, temp_dirs):
        """cron state 文件不存在时不报错"""
        from core.tools.shared.loop import LoopTool

        tool = LoopTool(cwd=".", workspace_uuid="test", cron_task_id="nonexistent-task")
        result = tool.execute(action="sync", items=["file1.md"])
        data = json.loads(result.output)

        # 正常返回，不报错
        assert data["added"] == 1
