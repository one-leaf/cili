"""Tests for web_api.py — access control middleware, API key masking, helpers."""

import json
from unittest.mock import patch, MagicMock

import pytest


class TestMaskSingleModel:
    """_mask_single_model() API key masking."""

    def test_long_key_masked(self):
        from web.web_api import _mask_single_model
        model = {"api_key": "sk-ant-1234567890abcdef", "name": "claude"}
        result = _mask_single_model(model)
        assert "api_key" not in result
        assert result["api_key_masked"] == "sk-a...cdef"
        assert result["name"] == "claude"

    def test_short_key_masked(self):
        from web.web_api import _mask_single_model
        model = {"api_key": "shortkey", "name": "test"}
        result = _mask_single_model(model)
        assert "api_key" not in result
        assert result["api_key_masked"] == "***"

    def test_empty_key_no_masked_field(self):
        from web.web_api import _mask_single_model
        model = {"api_key": "", "name": "test"}
        result = _mask_single_model(model)
        assert "api_key" not in result
        assert "api_key_masked" not in result

    def test_no_api_key_field(self):
        from web.web_api import _mask_single_model
        model = {"name": "test"}
        result = _mask_single_model(model)
        assert "api_key" not in result
        assert "api_key_masked" not in result


class TestMaskApiKey:
    """_mask_api_key() config-level masking."""

    def test_masks_all_models(self):
        from web.web_api import _mask_api_key
        config = {
            "model": {"api_key": "sk-ant-1234567890", "name": "claude"},
            "worker_model": {"api_key": "sk-worker-key-12345", "name": "worker-model"},
            "lite_model": {"api_key": "sk-lite-key-12345", "name": "lite-model"},
        }
        result = _mask_api_key(config)
        for key in ("model", "worker_model", "lite_model"):
            assert "api_key" not in result[key]
            assert "api_key_masked" in result[key]

    def test_no_model_field(self):
        from web.web_api import _mask_api_key
        config = {"system": {"pip_mirror": "https://..."}}
        result = _mask_api_key(config)
        assert result["system"]["pip_mirror"] == "https://..."


class TestLocalhostIPs:
    """Access control localhost IP set."""

    def test_contains_standard_ips(self):
        from web.web_api import _LOCALHOST_IPS
        assert "127.0.0.1" in _LOCALHOST_IPS
        assert "::1" in _LOCALHOST_IPS


class TestRequireWorkspace:
    """_require_workspace() dependency validation."""

    def test_nonexistent_workspace_raises_404(self):
        from fastapi import HTTPException
        from web.web_api import _require_workspace
        with pytest.raises(HTTPException) as exc_info:
            # _require_workspace is an async dependency
            import asyncio
            asyncio.run(_require_workspace("nonexistent-uuid"))
        assert exc_info.value.status_code == 404


class TestValidateWorkspaceUuid:
    """_validate_workspace_uuid() 路径穿越校验。"""

    def test_valid_uuid_passes(self):
        from web.web_api import _validate_workspace_uuid
        _validate_workspace_uuid("abc123")  # 不应抛异常

    def test_path_traversal_rejected(self):
        from fastapi import HTTPException
        from web.web_api import _validate_workspace_uuid
        for evil in ("..", "../..", "a/b", "C:\\evil", "a%2Fb"):
            with pytest.raises(HTTPException) as exc_info:
                _validate_workspace_uuid(evil)
            assert exc_info.value.status_code == 400


class TestListAllWorkspaces:
    """_list_all_workspaces() scans workspace directory."""

    def test_returns_empty_when_no_workspaces(self, tmp_path):
        """Empty directory returns empty list."""
        import web.web_api as api_module
        original = api_module.WORKSPACE_DATA_DIR
        try:
            api_module.WORKSPACE_DATA_DIR = tmp_path
            result = api_module._list_all_workspaces()
            assert result == []
        finally:
            api_module.WORKSPACE_DATA_DIR = original

    def test_skips_hidden_directories(self, tmp_path):
        """Directories starting with '.' are skipped."""
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "visible").mkdir()
        import web.web_api as api_module
        original = api_module.WORKSPACE_DATA_DIR
        try:
            api_module.WORKSPACE_DATA_DIR = tmp_path
            result = api_module._list_all_workspaces()
            # .hidden should not appear (visible has no config, so also skipped)
            assert all(w["uuid"] != ".hidden" for w in result)
        finally:
            api_module.WORKSPACE_DATA_DIR = original


class TestEvictIdleAgent:
    """_evict_idle_agent() LRU eviction logic."""

    def test_no_eviction_when_under_limit(self):
        from web.web_api import _evict_idle_agent, agents, _agent_access, _MAX_AGENTS
        original_agents = dict(agents)
        original_access = dict(_agent_access)
        try:
            agents.clear()
            _agent_access.clear()
            _evict_idle_agent()  # Should not raise
        finally:
            agents.clear()
            agents.update(original_agents)
            _agent_access.clear()
            _agent_access.update(original_access)

    def test_evicts_oldest_idle(self):
        from web.web_api import _evict_idle_agent, agents, _agent_access, _MAX_AGENTS
        original_agents = dict(agents)
        original_access = dict(_agent_access)
        try:
            agents.clear()
            _agent_access.clear()

            mock_agents = {}
            for i in range(_MAX_AGENTS + 3):
                agent = MagicMock()
                agent.is_running.return_value = False
                agent.cleanup.return_value = None
                key = f"test-{i}"
                agents[key] = agent
                _agent_access[key] = 1000 + i

            _evict_idle_agent()

            assert "test-0" not in agents
            assert len(agents) == _MAX_AGENTS + 2
        finally:
            agents.clear()
            agents.update(original_agents)
            _agent_access.clear()
            _agent_access.update(original_access)

    def test_does_not_evict_running_agent(self):
        from web.web_api import _evict_idle_agent, agents, _agent_access, _MAX_AGENTS
        original_agents = dict(agents)
        original_access = dict(_agent_access)
        try:
            agents.clear()
            _agent_access.clear()

            for i in range(_MAX_AGENTS + 2):
                agent = MagicMock()
                agent.is_running.return_value = True
                key = f"test-{i}"
                agents[key] = agent
                _agent_access[key] = 1000 + i

            _evict_idle_agent()

            assert len(agents) == _MAX_AGENTS + 2
        finally:
            agents.clear()
            agents.update(original_agents)
            _agent_access.clear()
            _agent_access.update(original_access)


class TestGlobalEvents:
    """GET /api/events 全局 SSE 事件流测试（直接调端点函数，迭代 body_iterator）。

    不用 TestClient：TestClient + asyncio.to_thread 组合会死锁挂起，且
    TestClient 的 client IP "testclient" 不在 _LOCALHOST_IPS 会 403。
    """

    def test_event_delivery_and_session_filter(self):
        """事件按 session_id 过滤投递；不匹配的事件被过滤"""
        import asyncio
        from web.web_api import stream_global_events
        from core.event_bus import get_event_bus

        async def run():
            resp = await stream_global_events(session_id="sess-a")
            assert resp.status_code == 200
            assert resp.media_type == "text/event-stream"
            agen = resp.body_iterator

            # 先发布不匹配的事件（应被过滤，收不到）
            get_event_bus().publish({
                "type": "text", "workspace_uuid": "ws-x", "session_id": "sess-other",
                "content": "should-not-arrive",
            })
            # 再发布匹配的事件
            get_event_bus().publish({
                "type": "text", "workspace_uuid": "ws-x", "session_id": "sess-a",
                "content": "hello", "exec_id": "exec-1",
            })

            chunk = await agen.__anext__()
            text = chunk if isinstance(chunk, str) else chunk.decode()
            assert "hello" in text
            assert "should-not-arrive" not in text
            await agen.aclose()
            return True

        assert asyncio.run(run())

    def test_unsubscribe_on_close(self):
        """流关闭后退订（finally unsubscribe），新订阅不受影响"""
        import asyncio
        from web.web_api import stream_global_events
        from core.event_bus import get_event_bus

        async def run():
            resp = await stream_global_events(session_id="sess-b")
            agen = resp.body_iterator
            get_event_bus().publish({
                "type": "text", "workspace_uuid": "ws-x", "session_id": "sess-b", "content": "first",
            })
            chunk = await agen.__anext__()
            text = chunk if isinstance(chunk, str) else chunk.decode()
            assert "first" in text
            await agen.aclose()

            # 关闭后再发布，同 session 的新订阅应正常收到
            resp2 = await stream_global_events(session_id="sess-b")
            agen2 = resp2.body_iterator
            get_event_bus().publish({
                "type": "text", "workspace_uuid": "ws-x", "session_id": "sess-b", "content": "second",
            })
            chunk2 = await agen2.__anext__()
            text2 = chunk2 if isinstance(chunk2, str) else chunk2.decode()
            assert "second" in text2
            await agen2.aclose()
            return True

        assert asyncio.run(run())

    def test_invalid_session_id_raises_400(self):
        """非法 session_id 返回 400（防注入）"""
        import asyncio
        import pytest
        from fastapi import HTTPException
        from web.web_api import stream_global_events

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(stream_global_events(session_id="bad/id"))
        assert exc_info.value.status_code == 400

    def test_master_on_tool_output_publishes_to_event_bus(self, monkeypatch, tmp_path):
        """回归：master 的 _on_tool_output 必须发布到事件总线。

        此前 _get_or_create_agent 里 event bus 变量被后面的 get_message_bus() 闭包
        晚绑定遮蔽成 MessageBus，publish 抛 AttributeError 被静默吞掉，
        导致前端收不到 master 执行 bash/python 的 tool_output 推流。
        """
        import asyncio
        import queue
        from web import web_api
        from core.event_bus import get_event_bus

        ws, sid = "ws-regress", "sess-regress-1"
        (tmp_path / "sessions").mkdir(exist_ok=True)
        monkeypatch.setattr(web_api, "_get_workspace_info", lambda uuid: {"directory": str(tmp_path)})

        async def run():
            agent = await web_api._get_or_create_agent(ws, sid)
            q = queue.Queue()
            get_event_bus().subscribe(q, ws, sid)
            try:
                # 直接调用 master 工具输出回调，验证走事件总线而非 MessageBus
                agent._on_tool_output("bash", "行 1\n", 7, "call_regress_1")
                ev = q.get(timeout=1)
                assert ev["type"] == "tool_output"
                assert ev["tool"] == "bash"
                assert ev["content"] == "行 1\n"
                assert ev["offset"] == 7
                assert ev["tool_use_id"] == "call_regress_1"
                assert "exec_id" not in ev  # master 工具不带 exec_id
                return True
            finally:
                get_event_bus().unsubscribe(q)

        assert asyncio.run(run())
