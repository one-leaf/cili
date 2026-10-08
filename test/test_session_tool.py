"""Tests for core/tools/session_tool.py."""

import json
from unittest.mock import MagicMock, patch

import pytest

from core.tools.session_tool import SessionTool
from core.tools.base import ToolResult


@pytest.fixture
def session_tool(test_workspace):
    tool = SessionTool(cwd=test_workspace, workspace_uuid="test-workspace")
    tool.session = MagicMock()
    tool.session.agent_logs._generate_exec_id.return_value = "exec_123"
    return tool


class TestSessionToolExecute:
    """SessionTool.execute() with mocked SessionRunner."""

    def test_empty_task_error(self, session_tool):
        """Empty task returns error."""
        result = session_tool.execute(task="")
        assert result.error is True
        assert "required" in result.output or "empty" in result.output

    def test_whitespace_only_task_error(self, session_tool):
        """Whitespace-only task returns error."""
        result = session_tool.execute(task="   \n  ")
        assert result.error is True

    def test_returns_result_synchronously(self, session_tool):
        """Returns complete result synchronously (no placeholder)."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "Task completed successfully",
            "iterations": 10,
            "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            result = session_tool.execute(task="test task")

        # Should return completed result directly (no placeholder)
        assert result.completed is True
        assert result.meta.get("exec_id") == "exec_123"
        # Result should be JSON with the agent result
        result_data = json.loads(result.output)
        assert result_data["status"] == "completed"
        assert result_data["summary"] == "Task completed successfully"

        # Pending entry should be cleaned up
        entry = session_tool.get_pending_session("exec_123")
        assert entry is None

    def test_agent_failure_returned_in_result(self, session_tool):
        """SessionRunner error is returned in the ToolResult."""
        mock_agent = MagicMock()
        mock_agent.run.side_effect = Exception("SessionRunner crashed")
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            result = session_tool.execute(task="test")

        # Should return error result directly
        assert result.completed is True
        result_data = json.loads(result.output)
        assert result_data["status"] == "error"
        assert "crashed" in result_data["summary"].lower() or "SessionRunner crashed" in result_data["summary"]

        # Pending entry should be cleaned up
        entry = session_tool.get_pending_session("exec_123")
        assert entry is None

    def test_closes_agent_after_run(self, session_tool):
        """SessionRunner.close() is called after run."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            session_tool.execute(task="test")

        # execute() waits synchronously, no need to wait for background thread
        mock_agent.close.assert_called_once()

    def test_closes_agent_on_error(self, session_tool):
        """SessionRunner.close() is called even when run raises."""
        mock_agent = MagicMock()
        mock_agent.run.side_effect = Exception("error")
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            session_tool.execute(task="test")

        # execute() waits synchronously, no need to wait for background thread
        mock_agent.close.assert_called_once()

    def test_forwards_usage_to_session(self, session_tool):
        """SessionRunner usage is forwarded to session manager."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 5,
            "usage": {
                "input_tokens": 500,
                "output_tokens": 200,
                "cache_read_tokens": 50,
                "cache_creation_tokens": 10,
            },
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            session_tool.execute(task="test")

        # execute() waits synchronously, usage is forwarded before return
        session_tool.session.update_usage.assert_called_once()
        call_kwargs = session_tool.session.update_usage.call_args.kwargs
        assert call_kwargs["input_tokens"] == 500
        assert call_kwargs["output_tokens"] == 200

    def test_fires_start_callback(self, session_tool):
        """on_session_start callback is fired before run."""
        session_tool.on_session_start = MagicMock()

        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_agent.close.return_value = None

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            session_tool.execute(task="test task description")

        session_tool.on_session_start.assert_called_once()
        call_args = session_tool.on_session_start.call_args.args
        assert call_args[0] == "exec_123"  # exec_id
        assert "test task" in call_args[1]  # task summary (truncated)

    def test_plan_passed_to_agent(self, session_tool):
        """Plan parameter is forwarded to SessionRunner."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_agent.close.return_value = None

        plan = ["step 1", "step 2", "step 3"]
        with patch("core.session_runner.SessionRunner", return_value=mock_agent) as MockSessionRunner:
            session_tool.execute(task="test", plan=plan)

        # Verify SessionRunner was created with plan
        call_kwargs = MockSessionRunner.call_args.kwargs
        assert call_kwargs["plan"] == plan

    def test_saves_session_after_run(self, session_tool):
        """Session is saved after SessionRunner completes."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []

        with patch("core.session_runner.SessionRunner", return_value=mock_agent):
            session_tool.execute(task="test")

        # execute() waits synchronously, session is saved before return
        session_tool.session.save.assert_called()


class TestSessionToolParameters:
    """SessionTool parameter schema."""

    def test_required_task(self):
        tool = SessionTool()
        # task is no longer required (action='read'/'kill'/'list' are alternatives to 'start')
        assert "task" in tool.parameters["properties"]

    def test_optional_parameters(self):
        tool = SessionTool()
        props = tool.parameters["properties"]
        assert "plan" in props
        assert "run_in_background" in props
        assert "action" in props
        assert "read" in props["action"]["enum"]
        assert "kill" in props["action"]["enum"]
        assert "list" in props["action"]["enum"]
        assert "start" in props["action"]["enum"]


class TestDelegationDepthLimit:
    """委派深度限制：master(0) 可委派 worker/lite；depth1 子代理仅可委派 lite；depth≥2 禁止再委派。"""

    def _mock_agent_run(self):
        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "completed", "summary": "ok", "iterations": 1, "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []
        return mock_agent

    def test_depth_1_worker_blocked(self, session_tool):
        """depth=1 的子代理委派 worker → 报错，不构造子 SessionRunner（depth1 仅可委派 lite）。"""
        session_tool.delegation_depth = 1
        with patch("core.session_runner.SessionRunner") as MockSessionRunner:
            result = session_tool.execute(task="delegate me")
        assert result.error is True
        assert "lite" in result.output.lower()
        MockSessionRunner.assert_not_called()

    def test_depth_1_lite_allowed(self, session_tool):
        """depth=1 的 worker 委派 lite → 允许，子 SessionRunner 收到 depth=2 且 role=lite。"""
        session_tool.delegation_depth = 1
        mock_agent = self._mock_agent_run()
        with patch("core.session_runner.SessionRunner", return_value=mock_agent) as MockSessionRunner:
            result = session_tool.execute(task="delegate me", agent_type="lite")
        assert result.error is not True
        assert MockSessionRunner.call_args.kwargs["delegation_depth"] == 2
        assert MockSessionRunner.call_args.kwargs["role"] == "lite"

    def test_depth_2_blocked(self, session_tool):
        """depth=2 的子代理（lite）再委派 → 报错，不构造子 SessionRunner。"""
        session_tool.delegation_depth = 2
        with patch("core.session_runner.SessionRunner") as MockSessionRunner:
            result = session_tool.execute(task="delegate me", agent_type="lite")
        assert result.error is True
        assert "depth" in result.output.lower()
        MockSessionRunner.assert_not_called()

    def test_depth_2_background_blocked(self, session_tool):
        """depth=2 background 委派同样受限制。"""
        session_tool.delegation_depth = 2
        with patch("core.session_runner.SessionRunner") as MockSessionRunner:
            result = session_tool.execute(task="delegate me", run_in_background=True, agent_type="lite")
        assert result.error is True
        MockSessionRunner.assert_not_called()

    def test_depth_1_background_lite_allowed(self, session_tool):
        """depth=1 background 委派 lite → 允许，子 SessionRunner 收到 depth=2。"""
        session_tool.delegation_depth = 1
        mock_agent = self._mock_agent_run()
        with patch("core.session_runner.SessionRunner", return_value=mock_agent) as MockSessionRunner:
            result = session_tool.execute(task="delegate me", run_in_background=True, agent_type="lite")
        assert result.error is not True
        assert MockSessionRunner.call_args.kwargs["delegation_depth"] == 2

    def test_depth_0_forwards_depth_1(self, session_tool):
        """master（depth=0）正常委派 worker，子 SessionRunner 收到 depth=1。"""
        mock_agent = self._mock_agent_run()
        with patch("core.session_runner.SessionRunner", return_value=mock_agent) as MockSessionRunner:
            result = session_tool.execute(task="test")
        assert result.error is not True
        assert MockSessionRunner.call_args.kwargs["delegation_depth"] == 1

    def test_depth_0_lite_allowed(self, session_tool):
        """master（depth=0）可委派 lite，子 SessionRunner 收到 depth=1 且 role=lite。"""
        mock_agent = self._mock_agent_run()
        with patch("core.session_runner.SessionRunner", return_value=mock_agent) as MockSessionRunner:
            result = session_tool.execute(task="test", agent_type="lite")
        assert result.error is not True
        assert MockSessionRunner.call_args.kwargs["delegation_depth"] == 1
        assert MockSessionRunner.call_args.kwargs["role"] == "lite"


class TestBackgroundSessionRunnerConcurrency:
    """后台子代理并发上限（config.system.max_concurrent_agents）。"""

    @pytest.fixture(autouse=True)
    def _clean_active(self, monkeypatch):
        """每个用例隔离全局活跃列表。"""
        from core.tools import background as base_mod
        monkeypatch.setattr(base_mod, "_active_background_runners", [])
        yield
        with base_mod._background_runners_cond:
            base_mod._active_background_runners.clear()
            base_mod._background_runners_cond.notify_all()

    def test_acquire_below_limit(self, session_tool):
        """低于上限立即获得槽位并加入活跃列表。"""
        from core.tools import background as base_mod
        agent = MagicMock()
        err = session_tool._acquire_background_runner_slot(agent)
        assert err is None
        assert agent in base_mod._active_background_runners

    def test_blocks_until_slot_freed(self, session_tool):
        """达到上限时阻塞，释放后获得槽位。"""
        from core.tools import background as base_mod
        holder = MagicMock()
        base_mod._active_background_runners.append(holder)
        config = MagicMock()
        config.system.max_concurrent_agents = 1
        session_tool.config = config

        results = {}

        def try_acquire():
            results["err"] = session_tool._acquire_background_runner_slot(MagicMock())

        import threading
        import time
        t = threading.Thread(target=try_acquire, daemon=True)
        t.start()
        time.sleep(0.3)
        assert t.is_alive(), "达到上限时应阻塞等待"

        with base_mod._background_runners_cond:
            base_mod._active_background_runners.remove(holder)
            base_mod._background_runners_cond.notify_all()
        t.join(timeout=5)
        assert not t.is_alive()
        assert results["err"] is None
        assert len(base_mod._active_background_runners) == 1

    def test_stop_check_returns_error(self, session_tool):
        """任务停止时立即返回错误，不阻塞。"""
        from core.tools import background as base_mod
        base_mod._active_background_runners.append(MagicMock())
        config = MagicMock()
        config.system.max_concurrent_agents = 1
        session_tool.config = config
        session_tool.stop_check = lambda: True

        err = session_tool._acquire_background_runner_slot(MagicMock())
        assert err is not None
        assert err.error is True
        assert "上限" in err.output

    def test_timeout_returns_error(self, session_tool, monkeypatch):
        """等待超时返回错误。"""
        from core.tools import background as base_mod
        base_mod._active_background_runners.append(MagicMock())
        config = MagicMock()
        config.system.max_concurrent_agents = 1
        session_tool.config = config
        session_tool.stop_check = None

        counter = {"n": 0}

        def fake_time():
            counter["n"] += 1
            return 99999 if counter["n"] > 1 else 0  # deadline=3600，随后时间越过 deadline

        monkeypatch.setattr(base_mod.time, "time", fake_time)
        err = session_tool._acquire_background_runner_slot(MagicMock())
        assert err is not None
        assert err.error is True
        assert "超时" in err.output


class TestSessionRunnerLifecycleBroadcast:
    """agent_start/agent_complete 生命周期事件广播到全局事件总线"""

    def test_sync_start_and_complete_published(self, session_tool):
        """同步执行：agent_start 在 run 前发布、agent_complete 在完成后发布，payload 正确"""
        session_tool.session.session_id = "sess-1"

        mock_agent = MagicMock()
        mock_agent.run.return_value = {"status": "completed", "summary": "ok", "iterations": 1, "usage": {}}
        mock_agent.close.return_value = None
        mock_agent.messages = []

        published = []
        mock_bus = MagicMock()
        mock_bus.publish.side_effect = published.append

        with patch("core.session_runner.SessionRunner", return_value=mock_agent), \
             patch("core.event_bus.get_event_bus", return_value=mock_bus):
            session_tool.execute(task="test task")

        types = [e["type"] for e in published]
        assert types[0] == "session_start"
        assert "session_complete" in types
        start = published[0]
        assert start["exec_id"] == "exec_123"
        assert start["session_id"] == "sess-1"
        assert start["workspace_uuid"] == "test-workspace"
        assert "task_summary" in start
        complete = next(e for e in published if e["type"] == "session_complete")
        assert complete["status"] == "completed"
        assert complete["exec_id"] == "exec_123"
        assert complete["session_id"] == "sess-1"

    def test_background_complete_republished(self, session_tool, monkeypatch):
        """后台模式：线程结束后补发 agent_complete（修复原先缺失的缺陷）"""
        import time
        from core.tools import background as base_mod

        session_tool.session.session_id = "sess-bg"
        monkeypatch.setattr(base_mod, "_active_background_runners", [])

        mock_agent = MagicMock()
        mock_agent.run.return_value = {"status": "completed", "summary": "bg done", "iterations": 1, "usage": {}}
        mock_agent.close.return_value = None
        mock_agent.messages = []
        mock_agent.task = "test task"
        mock_agent.max_iterations = 100

        published = []
        mock_bus = MagicMock()
        mock_bus.publish.side_effect = published.append

        registered = []

        # 记录注册的后台任务，测试结束后移除，避免污染 BackgroundTaskManager
        orig_register = base_mod.BackgroundTaskManager.register
        monkeypatch.setattr(
            base_mod.BackgroundTaskManager, "register",
            lambda task: (registered.append(task), orig_register(task))[1],
        )

        with patch("core.event_bus.get_event_bus", return_value=mock_bus):
            session_tool._start_background_runner(
                runner=mock_agent,
                session=session_tool.session,
                exec_id="exec_123",
                task_summary="test task",
            )

        # 等待后台线程结束并补发 agent_complete
        deadline = time.time() + 5
        while time.time() < deadline and not any(e["type"] == "session_complete" for e in published):
            time.sleep(0.01)

        completes = [e for e in published if e["type"] == "session_complete"]
        assert completes, "后台模式应补发 agent_complete"
        assert completes[0]["exec_id"] == "exec_123"
        assert completes[0]["status"] == "completed"
        assert completes[0]["session_id"] == "sess-bg"

        # 清理注册的后台任务
        for task in registered:
            base_mod.BackgroundTaskManager.remove(task.task_id)
