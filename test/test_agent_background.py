"""AgentTool 后台/事件生命周期：并发后台 runner、广播、mailbox。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestBackgroundSessionRunnerConcurrency:
    """后台子代理并发上限（config.system.max_concurrent_agents）。"""

    @pytest.fixture(autouse=True)
    def _clean_active(self, monkeypatch):
        """每个用例隔离全局活跃列表。"""
        from core.tools import agent_background as base_mod
        monkeypatch.setattr(base_mod, "_active_background_runners", [])
        yield
        with base_mod._background_runners_cond:
            base_mod._active_background_runners.clear()
            base_mod._background_runners_cond.notify_all()

    def test_acquire_below_limit(self, agent_tool):
        """低于上限立即获得槽位并加入活跃列表。"""
        from core.tools import agent_background as base_mod
        agent = MagicMock()
        err = agent_tool._acquire_background_runner_slot(agent)
        assert err is None
        assert agent in base_mod._active_background_runners

    def test_blocks_until_slot_freed(self, agent_tool):
        """达到上限时阻塞，释放后获得槽位。"""
        from core.tools import agent_background as base_mod
        holder = MagicMock()
        base_mod._active_background_runners.append(holder)
        config = MagicMock()
        config.system.max_concurrent_agents = 1
        agent_tool.config = config

        results = {}

        def try_acquire():
            results["err"] = agent_tool._acquire_background_runner_slot(MagicMock())

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

    def test_stop_check_returns_error(self, agent_tool):
        """任务停止时立即返回错误，不阻塞。"""
        from core.tools import agent_background as base_mod
        base_mod._active_background_runners.append(MagicMock())
        config = MagicMock()
        config.system.max_concurrent_agents = 1
        agent_tool.config = config
        agent_tool.stop_check = lambda: True

        err = agent_tool._acquire_background_runner_slot(MagicMock())
        assert err is not None
        assert err.error is True
        assert "上限" in err.output

    def test_timeout_returns_error(self, agent_tool, monkeypatch):
        """等待超时返回错误。"""
        from core.tools import agent_background as base_mod
        base_mod._active_background_runners.append(MagicMock())
        config = MagicMock()
        config.system.max_concurrent_agents = 1
        agent_tool.config = config
        agent_tool.stop_check = None

        counter = {"n": 0}

        def fake_time():
            counter["n"] += 1
            return 99999 if counter["n"] > 1 else 0  # deadline=3600，随后时间越过 deadline

        monkeypatch.setattr(base_mod.time, "time", fake_time)
        err = agent_tool._acquire_background_runner_slot(MagicMock())
        assert err is not None
        assert err.error is True
        assert "超时" in err.output


class TestSessionRunnerLifecycleBroadcast:
    """agent_start/agent_complete 生命周期事件广播到全局事件总线"""

    def test_sync_start_and_complete_published(self, agent_tool):
        """同步执行：agent_start 在 run 前发布、agent_complete 在完成后发布，payload 正确"""
        agent_tool.session.session_id = "sess-1"

        mock_agent = MagicMock()
        mock_agent.run.return_value = {"status": "completed", "summary": "ok", "iterations": 1, "usage": {}}
        mock_agent.close.return_value = None
        mock_agent.messages = []

        published = []
        mock_bus = MagicMock()
        mock_bus.publish.side_effect = published.append

        with patch("core.session_runner.SessionRunner", return_value=mock_agent), \
             patch("core.event_bus.get_event_bus", return_value=mock_bus):
            agent_tool.execute(task="test task")

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

    def test_orphan_handoff_reported_as_timeout(self, agent_tool):
        """handoff 但没有续任文档：任务未完成，终态按 timeout 广播（不得显示完成）。"""
        agent_tool.session.session_id = "sess-h"

        mock_agent = MagicMock()
        mock_agent.run.return_value = {
            "status": "handoff", "summary": "迭代耗尽", "iterations": 1, "usage": {},
        }
        mock_agent.close.return_value = None
        mock_agent.messages = []

        published = []
        mock_bus = MagicMock()
        mock_bus.publish.side_effect = published.append

        with patch("core.session_runner.SessionRunner", return_value=mock_agent),              patch("core.event_bus.get_event_bus", return_value=mock_bus):
            agent_tool.execute(task="test task")

        complete = next(e for e in published if e["type"] == "session_complete")
        assert complete["status"] == "timeout"

    def test_background_complete_republished(self, agent_tool, monkeypatch):
        """后台模式：线程结束后补发 agent_complete（修复原先缺失的缺陷）"""
        import time

        from core.tools import agent_background as base_mod

        agent_tool.session.session_id = "sess-bg"
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
            agent_tool._start_background_runner(
                runner=mock_agent,
                session=agent_tool.session,
                exec_id="exec_123",
                task_summary="test task",
            )

            # 等待后台线程结束并补发 agent_complete（patch 需保持到线程结束）
            deadline = time.time() + 5
            while time.time() < deadline and not any(
                e["type"] == "session_complete" for e in published
            ):
                time.sleep(0.01)

        completes = [e for e in published if e["type"] == "session_complete"]
        assert completes, "后台模式应补发 agent_complete"
        assert completes[0]["exec_id"] == "exec_123"
        assert completes[0]["status"] == "completed"
        assert completes[0]["session_id"] == "sess-bg"

        # 清理注册的后台任务
        for task in registered:
            base_mod.BackgroundTaskManager.remove(task.task_id)



class TestBackgroundMailboxLifecycle:
    """后台子代理的邮箱生命周期：Task ID 别名可寻址，终态关闭队列。"""

    def test_task_id_alias_registered_and_queue_closed(self, agent_tool, monkeypatch):
        import threading
        import time

        from core.agent_mailbox import get_agent_mailbox
        from core.tools import agent_background as base_mod

        monkeypatch.setattr(base_mod, "_active_background_runners", [])

        release = threading.Event()
        mock_agent = MagicMock()

        def _run():
            release.wait(timeout=5)
            return {"status": "completed", "summary": "ok", "stats": {}}

        mock_agent.run.side_effect = _run
        mock_agent.close.return_value = None
        mock_agent.messages = []
        mock_agent.task = "bg task"
        mock_agent.max_iterations = 10

        result = agent_tool._start_background_runner(
            runner=mock_agent,
            session=agent_tool.session,
            exec_id="exec_bg1",
            task_summary="bg task",
        )
        task_id = result.meta["task_id"]
        mbus = get_agent_mailbox()

        # spawn 结果暴露的是 Task ID（agent-N）：必须可直接寻址
        assert mbus._agent_registry.get(task_id) == "exec_bg1"
        assert mbus.send_to_agent("root", task_id, "hi") is True

        release.set()
        deadline = time.time() + 5
        while mbus._agent_registry.get(task_id) and time.time() < deadline:
            time.sleep(0.01)

        # 终态：名字注销 + 队列关闭，按 exec_id 的 send 不再假成功
        assert mbus._agent_registry.get(task_id) is None
        assert mbus.send("root", "exec_bg1", "late") is False
