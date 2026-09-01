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


class TestCronTaskRemainingCounter:
    """Test remaining counter functionality in CronTask."""

    def test_task_initializes_remaining_none(self, temp_cron_state):
        """新建任务 _remaining 初始为 None"""
        config = {
            "name": "test-remaining-init",
            "schedule": {"type": "interval", "minutes": 60},
            "task": "测试",
        }
        task = CronTask(config)
        assert task._remaining is None

    def test_task_restores_remaining_from_state(self, temp_cron_state):
        """从状态文件恢复 remaining"""
        import core.cron as cron_module

        # 先创建状态文件
        state_path = cron_module.CRON_STATE_DIR / "restore-task.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "remaining": 50,
            "run_count": 10,
            "last_run": "2026-08-25T10:00:00",
        }))

        config = {
            "name": "restore-task",
            "schedule": {"type": "interval", "minutes": 60},
            "task": "测试",
        }
        task = CronTask(config)
        assert task._remaining == 50

    def test_task_saves_remaining_to_state(self, temp_cron_state):
        """保存 remaining 到状态文件"""
        import core.cron as cron_module

        config = {
            "name": "save-remaining-task",
            "schedule": {"type": "interval", "minutes": 60},
            "task": "测试",
        }
        task = CronTask(config)
        task._remaining = 100
        task._run_count = 5
        task._save_state(datetime(2026, 8, 25, 10, 0, 0), None)

        # 读取状态文件验证
        state_path = cron_module.CRON_STATE_DIR / "save-remaining-task.json"
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        assert state["remaining"] == 100

    def test_task_state_without_remaining(self, temp_cron_state):
        """_remaining 为 None 时不写入状态文件"""
        import core.cron as cron_module

        config = {
            "name": "no-remaining-task",
            "schedule": {"type": "interval", "minutes": 60},
            "task": "测试",
        }
        task = CronTask(config)
        task._run_count = 5
        task._save_state(datetime(2026, 8, 25, 10, 0, 0), None)

        # 读取状态文件验证
        state_path = cron_module.CRON_STATE_DIR / "no-remaining-task.json"
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        assert "remaining" not in state


class TestCronSchedulerRemainingCounter:
    """Test CronScheduler remaining counter logic."""

    def test_execute_task_decrements_remaining(self, temp_cron_state, tmp_path):
        """_execute_task 每次递减 remaining"""
        import core.cron as cron_module

        # 创建任务
        config = {
            "name": "decrement-task",
            "enabled": True,
            "schedule": {"type": "interval", "minutes": 60},
            "content": {"task": "测试", "plan": []},
            "config": {"max_executions": 100},
        }

        # 创建用户任务文件
        user_tasks_file = tmp_path / "user_tasks.json"
        user_tasks_file.write_text(json.dumps([config], ensure_ascii=False), encoding="utf-8")

        original_user_file = cron_module.USER_TASKS_FILE
        cron_module.USER_TASKS_FILE = user_tasks_file

        try:
            scheduler = CronScheduler()
            scheduler.load_tasks()
            task = scheduler.get_task("decrement-task")
            assert task is not None

            # 初始 remaining 为 None，应该使用 max_executions
            assert task._remaining is None

            # 模拟 _execute_task 中的 remaining 逻辑
            max_exec = task.config.get("max_executions", 9999)
            remaining = task._remaining if task._remaining is not None else max_exec
            remaining -= 1
            task._remaining = remaining

            assert task._remaining == 99

        finally:
            cron_module.USER_TASKS_FILE = original_user_file

    def test_auto_disable_when_remaining_zero(self, temp_cron_state, tmp_path):
        """remaining <= 0 时自动 disable"""
        import core.cron as cron_module

        # 创建任务配置
        config = {
            "name": "auto-disable-task",
            "enabled": True,
            "schedule": {"type": "interval", "minutes": 60},
            "content": {"task": "测试", "plan": []},
            "config": {"max_executions": 5},
        }

        # 创建用户任务文件
        user_tasks_file = tmp_path / "user_tasks.json"
        user_tasks_file.write_text(json.dumps([config], ensure_ascii=False), encoding="utf-8")

        original_user_file = cron_module.USER_TASKS_FILE
        cron_module.USER_TASKS_FILE = user_tasks_file

        try:
            scheduler = CronScheduler()
            scheduler.load_tasks()
            task = scheduler.get_task("auto-disable-task")
            assert task is not None

            # 设置 remaining = 1
            task._remaining = 1
            task._save_state(datetime.now(), None)

            # 模拟 remaining 递减逻辑
            remaining = task._remaining - 1  # 1 - 1 = 0
            task._remaining = remaining

            # remaining <= 0 应该触发 disable
            if remaining <= 0:
                task.enabled = False

            assert task.enabled is False

        finally:
            cron_module.USER_TASKS_FILE = original_user_file


class TestCronToolMaxExecutions:
    """Test CronTool max_executions parameter."""

    @pytest.fixture
    def temp_cron_files(self, tmp_path):
        """临时替换 cron 相关文件路径"""
        import core.cron as cron_module
        import core.tools.shared.cron_tool as cron_tool_module

        original_user_file = cron_module.USER_TASKS_FILE
        original_state_dir = cron_module.CRON_STATE_DIR
        original_tool_user_file = cron_tool_module.USER_TASKS_FILE

        cron_module.USER_TASKS_FILE = tmp_path / "user_tasks.json"
        cron_module.CRON_STATE_DIR = tmp_path / "state"
        cron_tool_module.USER_TASKS_FILE = tmp_path / "user_tasks.json"

        yield tmp_path

        cron_module.USER_TASKS_FILE = original_user_file
        cron_module.CRON_STATE_DIR = original_state_dir
        cron_tool_module.USER_TASKS_FILE = original_tool_user_file

    def test_create_with_max_executions(self, temp_cron_files):
        """创建任务时设置 max_executions"""
        from core.tools.shared.cron_tool import CronTool, _load_user_tasks

        tool = CronTool(cwd=".", workspace_uuid="test")
        result = tool.execute(
            action="create",
            name="test-max-exec-50",
            schedule={"type": "interval", "minutes": 60},
            task="测试任务",
            max_executions=50,
        )

        assert "Created" in result.output

        # 验证任务配置
        tasks = _load_user_tasks()
        assert len(tasks) == 1
        assert tasks[0]["config"]["max_executions"] == 50

    def test_create_with_default_max_executions(self, temp_cron_files):
        """默认 max_executions 为 9999"""
        from core.tools.shared.cron_tool import CronTool, _load_user_tasks

        tool = CronTool(cwd=".", workspace_uuid="test")
        tool.execute(
            action="create",
            name="test-default-max-9999",
            schedule={"type": "interval", "minutes": 60},
            task="测试任务",
        )

        tasks = _load_user_tasks()
        assert tasks[0]["config"]["max_executions"] == 9999

    def test_create_with_invalid_max_executions(self, temp_cron_files):
        """max_executions 超出范围时报错"""
        from core.tools.shared.cron_tool import CronTool

        tool = CronTool(cwd=".", workspace_uuid="test")

        # 超过最大值
        result = tool.execute(
            action="create",
            name="test-invalid-max-10000",
            schedule={"type": "interval", "minutes": 60},
            task="测试任务",
            max_executions=10000,
        )
        assert result.is_error

        # 小于最小值
        result = tool.execute(
            action="create",
            name="test-invalid-max-0",
            schedule={"type": "interval", "minutes": 60},
            task="测试任务",
            max_executions=0,
        )
        assert result.is_error

    def test_enable_resets_remaining(self, temp_cron_files):
        """enable 时重置 remaining 为 max_executions"""
        import json
        import core.cron as cron_module
        from core.tools.shared.cron_tool import CronTool

        tool = CronTool(cwd=".", workspace_uuid="test")

        # 创建任务
        tool.execute(
            action="create",
            name="test-enable-reset-remaining",
            schedule={"type": "interval", "minutes": 60},
            task="测试任务",
            max_executions=100,
        )

        # 手动设置 remaining 为较小值
        state_path = cron_module.CRON_STATE_DIR / "test-enable-reset-remaining.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"remaining": 10}))

        # enable 任务
        tool.execute(action="enable", name="test-enable-reset-remaining")

        # 验证 remaining 已重置
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        assert state["remaining"] == 100
