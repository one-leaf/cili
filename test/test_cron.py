"""Tests for cron scheduler (core/cron.py)."""

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.cron import CronTask, CronScheduler


class TestCronTask:
    """Test CronTask creation and state management."""

    def test_create_task(self, temp_cron_state):
        """任务从配置创建"""
        config = {
            "name": "test-task",
            "description": "测试任务",
            "enabled": True,
            "schedule": {"type": "interval", "minutes": 30},
            "content": "test.py",
            "config": {"max_iterations": 10},
        }
        task = CronTask(config)
        assert task.name == "test-task"
        assert task.description == "测试任务"
        assert task.enabled is True
        assert task.config == {"max_iterations": 10}
        assert task._next_run is None
        assert task._last_run is None
        assert task._run_count == 0

    def test_should_run_first_time(self, temp_cron_state):
        """首次运行：_next_run 为 None 时应触发"""
        config = {
            "name": "test",
            "schedule": {"type": "interval", "minutes": 60},
            "task": "测试",
        }
        task = CronTask(config)
        assert task.should_run(datetime.now()) is True

    def test_should_run_disabled(self, temp_cron_state):
        """禁用的任务不应触发"""
        config = {
            "name": "test",
            "enabled": False,
            "schedule": {"type": "interval", "minutes": 60},
            "task": "测试",
        }
        task = CronTask(config)
        assert task.should_run(datetime.now()) is False

    def test_should_run_after_interval(self, temp_cron_state):
        """到期任务应触发"""
        config = {
            "name": "test",
            "schedule": {"type": "interval", "minutes": 60},
            "task": "测试",
        }
        task = CronTask(config)
        now = datetime.now()
        task._next_run = now - timedelta(minutes=1)  # 已到期
        assert task.should_run(now) is True

    def test_should_not_run_before_interval(self, temp_cron_state):
        """未到期的任务不应触发"""
        config = {
            "name": "test",
            "schedule": {"type": "interval", "minutes": 60},
            "task": "测试",
        }
        task = CronTask(config)
        now = datetime.now()
        task._next_run = now + timedelta(minutes=30)  # 30分钟后
        assert task.should_run(now) is False

    def test_mark_executed(self, temp_cron_state):
        """执行后更新下次运行时间"""
        config = {
            "name": "test",
            "schedule": {"type": "interval", "minutes": 30},
            "task": "测试",
        }
        task = CronTask(config)
        now = datetime(2026, 8, 25, 10, 0, 0)
        task.mark_executed(now)

        assert task._last_run == now
        assert task._run_count == 1
        assert task._next_run == now + timedelta(minutes=30)

    def test_mark_executed_multiple(self, temp_cron_state):
        """多次执行累计计数"""
        config = {
            "name": "test-mark-multi",  # 使用唯一名称避免状态冲突
            "schedule": {"type": "interval", "minutes": 60},
            "task": "测试",
        }
        task = CronTask(config)
        for i in range(3):
            task.mark_executed(datetime(2026, 8, 25, 10 + i, 0, 0))
        assert task._run_count == 3

    def test_to_dict(self, temp_cron_state):
        """序列化为 dict"""
        config = {
            "name": "test-task",
            "description": "测试任务",
            "enabled": True,
            "schedule": {"type": "interval", "minutes": 60},
            "task": "执行测试",
        }
        task = CronTask(config)
        d = task.to_dict()
        assert d["name"] == "test-task"
        assert d["description"] == "测试任务"
        assert d["enabled"] is True
        assert d["last_run"] is None
        assert d["next_run"] is None
        assert d["run_count"] == 0

    def test_to_dict_after_execution(self, temp_cron_state):
        """执行后序列化包含运行信息"""
        config = {
            "name": "test-task",
            "schedule": {"type": "interval", "minutes": 60},
            "task": "测试",
        }
        task = CronTask(config)
        task.mark_executed(datetime(2026, 8, 25, 10, 0, 0))
        d = task.to_dict()
        assert d["run_count"] == 1
        assert d["last_run"] == "2026-08-25T10:00:00"
        assert d["next_run"] == "2026-08-25T11:00:00"


class TestCronScheduler:
    """Test CronScheduler task management."""

    def test_scheduler_creation(self):
        """调度器创建"""
        scheduler = CronScheduler()
        assert scheduler.tasks == []
        assert scheduler._running is False

    def test_load_tasks_from_directory(self, tmp_path):
        """从目录加载任务"""
        # 创建临时任务文件
        task_config = {
            "name": "test-load",
            "description": "加载测试",
            "enabled": True,
            "schedule": {"type": "interval", "minutes": 60},
            "content": {"task": "测试任务", "plan": ["步骤一"]},
        }
        (tmp_path / "test_task.json").write_text(
            json.dumps(task_config, ensure_ascii=False), encoding="utf-8"
        )

        scheduler = CronScheduler()
        # 临时替换 CRON_DIR 和 USER_TASKS_FILE
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        original_user_file = cron_module.USER_TASKS_FILE
        cron_module.CRON_DIR = tmp_path
        cron_module.USER_TASKS_FILE = tmp_path / "nonexistent_user_tasks.json"
        try:
            count = scheduler.load_tasks()
            assert count == 1
            assert len(scheduler.tasks) == 1
            assert scheduler.tasks[0].name == "test-load"
        finally:
            cron_module.CRON_DIR = original_dir
            cron_module.USER_TASKS_FILE = original_user_file

    def test_load_multiple_tasks(self, tmp_path):
        """加载多个任务"""
        for i in range(3):
            config = {
                "name": f"task-{i}",
                "schedule": {"type": "interval", "minutes": 60},
                "content": {"task": f"任务 {i}", "plan": []},
            }
            (tmp_path / f"task_{i}.json").write_text(
                json.dumps(config, ensure_ascii=False), encoding="utf-8"
            )

        scheduler = CronScheduler()
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        original_user_file = cron_module.USER_TASKS_FILE
        cron_module.CRON_DIR = tmp_path
        cron_module.USER_TASKS_FILE = tmp_path / "nonexistent_user_tasks.json"
        try:
            count = scheduler.load_tasks()
            assert count == 3
            names = {t.name for t in scheduler.tasks}
            assert names == {"task-0", "task-1", "task-2"}
        finally:
            cron_module.CRON_DIR = original_dir
            cron_module.USER_TASKS_FILE = original_user_file

    def test_load_invalid_json(self, tmp_path):
        """无效 JSON 文件跳过不崩溃"""
        (tmp_path / "bad.json").write_text("{invalid json", encoding="utf-8")
        good_config = {
            "name": "good",
            "schedule": {"type": "interval", "minutes": 60},
            "content": {"task": "好任务", "plan": []},
        }
        (tmp_path / "good.json").write_text(
            json.dumps(good_config, ensure_ascii=False), encoding="utf-8"
        )

        scheduler = CronScheduler()
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        original_user_file = cron_module.USER_TASKS_FILE
        cron_module.CRON_DIR = tmp_path
        cron_module.USER_TASKS_FILE = tmp_path / "nonexistent_user_tasks.json"
        try:
            count = scheduler.load_tasks()
            assert count == 1
            assert scheduler.tasks[0].name == "good"
        finally:
            cron_module.CRON_DIR = original_dir
            cron_module.USER_TASKS_FILE = original_user_file

    def test_get_task_by_name(self, tmp_path):
        """按名称获取任务"""
        config = {
            "name": "find-me",
            "schedule": {"type": "interval", "minutes": 60},
            "content": {"task": "找到我", "plan": []},
        }
        (tmp_path / "find_me.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )

        scheduler = CronScheduler()
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        original_user_file = cron_module.USER_TASKS_FILE
        cron_module.CRON_DIR = tmp_path
        cron_module.USER_TASKS_FILE = tmp_path / "nonexistent_user_tasks.json"
        try:
            scheduler.load_tasks()
            task = scheduler.get_task("find-me")
            assert task is not None
            assert task.name == "find-me"
            assert scheduler.get_task("nonexistent") is None
        finally:
            cron_module.CRON_DIR = original_dir
            cron_module.USER_TASKS_FILE = original_user_file

    def test_list_tasks(self, tmp_path):
        """列出所有任务状态"""
        for i in range(2):
            config = {
                "name": f"task-{i}",
                "schedule": {"type": "interval", "minutes": 60},
                "content": {"task": f"任务 {i}", "plan": []},
            }
            (tmp_path / f"task_{i}.json").write_text(
                json.dumps(config, ensure_ascii=False), encoding="utf-8"
            )

        scheduler = CronScheduler()
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        original_user_file = cron_module.USER_TASKS_FILE
        cron_module.CRON_DIR = tmp_path
        cron_module.USER_TASKS_FILE = tmp_path / "nonexistent_user_tasks.json"
        try:
            scheduler.load_tasks()
            task_list = scheduler.list_tasks()
            assert len(task_list) == 2
            assert all(isinstance(t, dict) for t in task_list)
            names = {t["name"] for t in task_list}
            assert names == {"task-0", "task-1"}
        finally:
            cron_module.CRON_DIR = original_dir
            cron_module.USER_TASKS_FILE = original_user_file

    def test_scheduler_start_stop(self, tmp_path):
        """调度器启动和停止"""
        config = {
            "name": "start-stop-test",
            "schedule": {"type": "interval", "minutes": 60},
            "task": "测试",
        }
        (tmp_path / "test.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )

        scheduler = CronScheduler()
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        cron_module.CRON_DIR = tmp_path
        try:
            scheduler.load_tasks()
            scheduler._running = True
            assert scheduler._running is True
            scheduler.stop()
            assert scheduler._running is False
        finally:
            cron_module.CRON_DIR = original_dir

    def test_no_tasks_no_start(self, tmp_path):
        """没有任务时不启动线程"""
        scheduler = CronScheduler()
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        original_user_file = cron_module.USER_TASKS_FILE
        cron_module.CRON_DIR = tmp_path  # empty dir, no tasks
        cron_module.USER_TASKS_FILE = tmp_path / "nonexistent_user_tasks.json"
        try:
            scheduler.start()
            # 没有任务，_running 仍为 False
            assert scheduler._running is False
        finally:
            cron_module.CRON_DIR = original_dir
            cron_module.USER_TASKS_FILE = original_user_file


class TestCronCondition:
    """Test execution condition checking via get_tasks()."""

    def test_get_tasks_no_task_fn(self, temp_cron_state):
        """无 task_fn 时返回空列表"""
        config = {"name": "test", "schedule": {"type": "interval", "minutes": 60}, "content": "test.py"}
        task = CronTask(config)
        assert task.get_tasks() == []

    def test_get_tasks_returns_tasks(self, temp_cron_state):
        """task_fn 返回任务列表时正常返回"""
        config = {"name": "test", "schedule": {"type": "interval", "minutes": 60}, "content": "test.py"}
        tasks = [{"task": "执行任务1", "plan": ["步骤一"]}, {"task": "执行任务2", "plan": ["步骤二"]}]
        task = CronTask(config, task_fn=lambda: tasks)
        result = task.get_tasks()
        assert len(result) == 2
        assert result[0]["task"] == "执行任务1"
        assert result[1]["task"] == "执行任务2"

    def test_get_tasks_returns_single_task(self, temp_cron_state):
        """task_fn 返回单个任务时转换为列表"""
        config = {"name": "test", "schedule": {"type": "interval", "minutes": 60}, "content": "test.py"}
        task = CronTask(config, task_fn=lambda: {"task": "单个任务", "plan": ["步骤一"]})
        result = task.get_tasks()
        assert len(result) == 1
        assert result[0]["task"] == "单个任务"

    def test_get_tasks_returns_empty_to_skip(self, temp_cron_state):
        """task_fn 返回空列表表示跳过"""
        config = {"name": "test", "schedule": {"type": "interval", "minutes": 60}, "content": "test.py"}
        task = CronTask(config, task_fn=lambda: [])
        assert task.get_tasks() == []

    def test_get_tasks_exception(self, temp_cron_state):
        """task_fn 抛出异常时返回空列表"""
        config = {"name": "test", "schedule": {"type": "interval", "minutes": 60}, "content": "test.py"}

        def bad_task_fn():
            raise RuntimeError("boom")

        task = CronTask(config, task_fn=bad_task_fn)
        assert task.get_tasks() == []

    def test_load_task_fn_from_content(self, tmp_path):
        """从 content 指定的 Python 文件加载 get_tasks 函数"""
        task_config = {
            "name": "content-test",
            "schedule": {"type": "interval", "minutes": 60},
            "content": "content_test.py",
        }
        (tmp_path / "content_test.json").write_text(
            json.dumps(task_config, ensure_ascii=False), encoding="utf-8"
        )
        (tmp_path / "content_test.py").write_text(
            "def get_tasks():\n    return [{'task': '任务内容', 'plan': ['步骤一']}]\n", encoding="utf-8"
        )

        scheduler = CronScheduler()
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        original_user_file = cron_module.USER_TASKS_FILE
        cron_module.CRON_DIR = tmp_path
        cron_module.USER_TASKS_FILE = tmp_path / "nonexistent_user_tasks.json"
        try:
            scheduler.load_tasks()
            assert len(scheduler.tasks) == 1
            assert scheduler.tasks[0]._task_fn is not None
            tasks = scheduler.tasks[0].get_tasks()
            assert len(tasks) == 1
            assert tasks[0]["task"] == "任务内容"
            assert tasks[0]["plan"] == ["步骤一"]
        finally:
            cron_module.CRON_DIR = original_dir
            cron_module.USER_TASKS_FILE = original_user_file

    def test_load_no_content_field(self, tmp_path):
        """没有 content 字段时无 task_fn"""
        task_config = {
            "name": "no-content",
            "schedule": {"type": "interval", "minutes": 60},
        }
        (tmp_path / "no_content.json").write_text(
            json.dumps(task_config, ensure_ascii=False), encoding="utf-8"
        )

        scheduler = CronScheduler()
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        cron_module.CRON_DIR = tmp_path
        try:
            scheduler.load_tasks()
            assert scheduler.tasks[0]._task_fn is None
            assert scheduler.tasks[0].get_tasks() == []
        finally:
            cron_module.CRON_DIR = original_dir

    def test_load_content_file_missing(self, tmp_path):
        """content 指向的文件不存在时无 task_fn"""
        task_config = {
            "name": "missing-file",
            "schedule": {"type": "interval", "minutes": 60},
            "content": "nonexistent.py",
        }
        (tmp_path / "missing_file.json").write_text(
            json.dumps(task_config, ensure_ascii=False), encoding="utf-8"
        )

        scheduler = CronScheduler()
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        cron_module.CRON_DIR = tmp_path
        try:
            scheduler.load_tasks()
            assert scheduler.tasks[0]._task_fn is None
        finally:
            cron_module.CRON_DIR = original_dir

    def test_load_content_as_inline_dict(self, tmp_path):
        """content 为内联 dict 时直接提取 task/plan"""
        task_config = {
            "name": "inline-task",
            "schedule": {"type": "interval", "minutes": 60},
            "content": {
                "task": "执行内联任务",
                "plan": ["步骤一", "步骤二"],
            },
        }
        (tmp_path / "inline_task.json").write_text(
            json.dumps(task_config, ensure_ascii=False), encoding="utf-8"
        )

        scheduler = CronScheduler()
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        cron_module.CRON_DIR = tmp_path
        try:
            scheduler.load_tasks()
            assert scheduler.tasks[0]._task_fn is not None
            tasks = scheduler.tasks[0].get_tasks()
            assert len(tasks) == 1
            assert tasks[0]["task"] == "执行内联任务"
            assert tasks[0]["plan"] == ["步骤一", "步骤二"]
        finally:
            cron_module.CRON_DIR = original_dir

    def test_load_content_dict_empty_task(self, tmp_path):
        """content dict 的 task 为空时无 task_fn"""
        task_config = {
            "name": "empty-inline",
            "schedule": {"type": "interval", "minutes": 60},
            "content": {
                "task": "",
                "plan": ["步骤一"],
            },
        }
        (tmp_path / "empty_inline.json").write_text(
            json.dumps(task_config, ensure_ascii=False), encoding="utf-8"
        )

        scheduler = CronScheduler()
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        cron_module.CRON_DIR = tmp_path
        try:
            scheduler.load_tasks()
            assert scheduler.tasks[0]._task_fn is None
        finally:
            cron_module.CRON_DIR = original_dir

    def test_load_content_as_inline_list(self, tmp_path):
        """content 为内联 list 时提取多个任务"""
        task_config = {
            "name": "multi-tasks",
            "schedule": {"type": "interval", "minutes": 60},
            "content": [
                {"task": "任务1", "plan": ["步骤A"]},
                {"task": "任务2", "plan": ["步骤B", "步骤C"]},
            ],
        }
        (tmp_path / "multi_tasks.json").write_text(
            json.dumps(task_config, ensure_ascii=False), encoding="utf-8"
        )

        scheduler = CronScheduler()
        import core.cron as cron_module
        original_dir = cron_module.CRON_DIR
        cron_module.CRON_DIR = tmp_path
        try:
            scheduler.load_tasks()
            assert scheduler.tasks[0]._task_fn is not None
            tasks = scheduler.tasks[0].get_tasks()
            assert len(tasks) == 2
            assert tasks[0]["task"] == "任务1"
            assert tasks[1]["task"] == "任务2"
        finally:
            cron_module.CRON_DIR = original_dir


class TestExtractUserInfoConfig:
    """测试 extract_user_info.json 配置加载"""

    def test_extract_user_info_config_loads(self):
        """extract_user_info.json 能正确加载"""
        config_path = Path(__file__).parent.parent / "core" / "cron.d" / "extract_user_info.json"
        if not config_path.exists():
            pytest.skip("extract_user_info.json not found")

        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        # 验证必需字段
        assert "name" in config
        assert "schedule" in config
        assert "content" in config  # 新设计：使用 content 内联 task/plan
        assert config["enabled"] is True
        assert config["schedule"]["type"] == "cron"
        assert config["schedule"]["expr"] == "0 2 * * *"
        assert config.get("config", {}).get("max_iterations", 0) > 0

        # 验证 content 为 dict，包含 task 和 plan
        content = config["content"]
        assert isinstance(content, dict)
        assert content.get("task", "") != ""
        assert isinstance(content.get("plan", []), list)
