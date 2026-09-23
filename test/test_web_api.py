"""Tests for web_api.py — access control middleware, API key masking, helpers."""

import json
from unittest.mock import patch, MagicMock

import pytest


class TestMaskSingleModel:
    """_mask_single_model() API key masking."""

    def test_long_key_masked(self):
        from web.routes_config import _mask_single_model
        model = {"api_key": "sk-ant-1234567890abcdef", "name": "claude"}
        result = _mask_single_model(model)
        assert "api_key" not in result
        assert result["api_key_masked"] == "sk-a...cdef"
        assert result["name"] == "claude"

    def test_short_key_masked(self):
        from web.routes_config import _mask_single_model
        model = {"api_key": "shortkey", "name": "test"}
        result = _mask_single_model(model)
        assert "api_key" not in result
        assert result["api_key_masked"] == "***"

    def test_empty_key_no_masked_field(self):
        from web.routes_config import _mask_single_model
        model = {"api_key": "", "name": "test"}
        result = _mask_single_model(model)
        assert "api_key" not in result
        assert "api_key_masked" not in result

    def test_no_api_key_field(self):
        from web.routes_config import _mask_single_model
        model = {"name": "test"}
        result = _mask_single_model(model)
        assert "api_key" not in result
        assert "api_key_masked" not in result


class TestMaskApiKey:
    """_mask_api_key() config-level masking."""

    def test_masks_all_models(self):
        from web.routes_config import _mask_api_key
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
        from web.routes_config import _mask_api_key
        config = {"system": {"pip_mirror": "https://..."}}
        result = _mask_api_key(config)
        assert result["system"]["pip_mirror"] == "https://..."


class TestLocalhostIPs:
    """Access control localhost IP set."""

    def test_contains_standard_ips(self):
        from web.deps import _LOCALHOST_IPS
        assert "127.0.0.1" in _LOCALHOST_IPS
        assert "::1" in _LOCALHOST_IPS


class TestRequireWorkspace:
    """_require_workspace() dependency validation."""

    def test_nonexistent_workspace_raises_404(self):
        from fastapi import HTTPException
        from web.deps import _require_workspace
        with pytest.raises(HTTPException) as exc_info:
            # _require_workspace is an async dependency
            import asyncio
            asyncio.run(_require_workspace("nonexistent-uuid"))
        assert exc_info.value.status_code == 404


class TestValidateWorkspaceUuid:
    """_validate_workspace_uuid() 路径穿越校验。"""

    def test_valid_uuid_passes(self):
        from web.deps import _validate_workspace_uuid
        _validate_workspace_uuid("abc123")  # 不应抛异常

    def test_path_traversal_rejected(self):
        from fastapi import HTTPException
        from web.deps import _validate_workspace_uuid
        for evil in ("..", "../..", "a/b", "C:\\evil", "a%2Fb"):
            with pytest.raises(HTTPException) as exc_info:
                _validate_workspace_uuid(evil)
            assert exc_info.value.status_code == 400


class TestListAllWorkspaces:
    """_list_all_workspaces() reads workspaces.json index."""

    def test_returns_empty_when_no_workspaces(self, tmp_path, monkeypatch):
        """Empty index returns empty list."""
        import core.config as config_mod
        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        import web.deps as api_module
        result = api_module._list_all_workspaces()
        assert result == []

    def test_reads_index_entries(self, tmp_path, monkeypatch):
        """Index entries are returned with their metadata."""
        import core.config as config_mod
        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        config_mod.upsert_workspace_entry({
            "uuid": "ws1",
            "workspace_name": "WS One",
            "directory": str(tmp_path / "ws1"),
            "created_at": "2026-09-21 10:00:00",
        })
        import web.deps as api_module
        result = api_module._list_all_workspaces()
        assert result == [{
            "uuid": "ws1",
            "name": "WS One",
            "directory": str(tmp_path / "ws1"),
            "created_at": "2026-09-21 10:00:00",
            "system": False,
        }]


class TestEvictIdleAgent:
    """_evict_idle_agent() LRU eviction logic."""

    def test_no_eviction_when_under_limit(self):
        from web.deps import _evict_idle_agent, agents, _agent_access, _MAX_AGENTS
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
        from web.deps import _evict_idle_agent, agents, _agent_access, _MAX_AGENTS
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
        from web.deps import _evict_idle_agent, agents, _agent_access, _MAX_AGENTS
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
        from web.routes_chat import stream_global_events
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
        from web.routes_chat import stream_global_events
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
        from web.routes_chat import stream_global_events

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
        from web import deps as web_api
        from core.event_bus import get_event_bus
        from pathlib import Path

        ws, sid = "ws-regress", "sess-regress-1"
        (tmp_path / "sessions").mkdir(exist_ok=True)
        monkeypatch.setattr(web_api, "_get_workspace_info", lambda uuid: {"directory": str(tmp_path)})
        # Agent._init_interactive 内部通过 get_workspace_data_dir 解析数据目录
        monkeypatch.setattr("core.config.get_workspace_data_dir", lambda uuid: tmp_path)
        # 测试结束后清理全局 agents 缓存，避免泄漏
        monkeypatch.setattr(web_api, "agents", {})

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


class TestAskUserDirectInput:
    """ask_user 待回答时，对话框直接输入作为"其他"回复（后端处理，前端零改动）。

    回归：此前该场景会作为新 user message 发送给 LLM 造成双重处理，
    ask_user 卡片也无法正常关闭。
    """

    @staticmethod
    def _placeholder_session(tmp_path):
        """构造带待回答 ask_user 占位符的假 session（工具结果 completed=False）。"""
        from types import SimpleNamespace
        tool_use_id = "call_ask_1"
        messages = [
            {"role": "user", "content": "最初的用户消息", "_meta": {"id": "m1"}},
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": tool_use_id, "name": "ask_user",
                "input": {"questions": [
                    {"question": "你喜欢哪种语言？", "header": "语言",
                     "options": [{"label": "Python", "description": "..."}]},
                    {"question": "多久反馈一次？", "header": "频率",
                     "options": [{"label": "每小时", "description": "..."}]},
                ]},
            }], "_meta": {"id": "m2"}},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tool_use_id,
                "content": "Waiting for user input...", "is_error": False,
                "_meta": {"tool_name": "ask_user", "completed": False},
            }], "_meta": {"id": "m3"}},
        ]
        session_dir = tmp_path / "sessions" / "sess-ask"
        session_dir.mkdir(parents=True, exist_ok=True)
        sm = SimpleNamespace(messages=messages, session_dir=session_dir)
        sm.saved = []
        sm.save = lambda: sm.saved.append(True)
        sm.mark_dirty = lambda: None  # _inject_ask_user_answer 就地改消息后置脏
        return sm, tool_use_id

    def test_find_pending_ask_user(self, tmp_path):
        from web.routes_ask_user import _find_pending_ask_user
        sm, tool_use_id = self._placeholder_session(tmp_path)
        assert _find_pending_ask_user(sm) == tool_use_id

    def test_find_pending_ask_user_none_when_answered(self, tmp_path):
        from web.routes_ask_user import _find_pending_ask_user
        sm, tool_use_id = self._placeholder_session(tmp_path)
        sm.messages[-1]["content"][0]["_meta"]["completed"] = True
        assert _find_pending_ask_user(sm) is None

    def test_build_other_answer_formats_questions(self, tmp_path):
        from web.routes_ask_user import _build_other_answer
        sm, tool_use_id = self._placeholder_session(tmp_path)
        answer = _build_other_answer(sm, tool_use_id, "我选 Python")
        assert answer == "你喜欢哪种语言？ 我选 Python\n多久反馈一次？ 我选 Python"

    def test_inject_ask_user_answer(self, tmp_path):
        from types import SimpleNamespace
        from web.routes_ask_user import _inject_ask_user_answer
        sm, tool_use_id = self._placeholder_session(tmp_path)
        agent = SimpleNamespace(session_manager=sm, approval_store=None)
        ok = _inject_ask_user_answer(agent, tool_use_id, "你喜欢哪种语言？ Python")
        assert ok is True
        placeholder = sm.messages[-1]["content"][0]
        assert placeholder["content"] == "你喜欢哪种语言？ Python"
        assert placeholder["_meta"]["completed"] is True
        # 答案文件已写盘
        out = sm.session_dir / placeholder["_meta"]["output_path"]
        assert out.read_text(encoding="utf-8") == "你喜欢哪种语言？ Python"
        # tool_use 块已标记 answered
        assert sm.messages[1]["content"][0]["_meta"]["answered"] is True
        assert sm.saved

    def test_inject_ask_user_answer_unknown_id_returns_false(self, tmp_path):
        from types import SimpleNamespace
        from web.routes_ask_user import _inject_ask_user_answer
        sm, _ = self._placeholder_session(tmp_path)
        agent = SimpleNamespace(session_manager=sm, approval_store=None)
        assert _inject_ask_user_answer(agent, "call_missing", "x") is False

    # ─── 审批分支三态 ─────────────────────────────────────────────

    @staticmethod
    def _approval_agent(tmp_path):
        """构造带待批准命令的 agent（approval_store.pending 非空）。"""
        from types import SimpleNamespace
        from core.tools.approval import ApprovalStore, approval_decision_id
        sm, tool_use_id = TestAskUserDirectInput._placeholder_session(tmp_path)
        store = ApprovalStore(rules_path=tmp_path / "approvals.json")
        store.set_pending({
            "decision_id": approval_decision_id("rm -rf /tmp/x"),
            "command": "rm -rf /tmp/x",
            "reason": "destructive recursive delete",
        })
        agent = SimpleNamespace(session_manager=sm, approval_store=store)
        return agent, tool_use_id, store

    def test_inject_answer_remember_persists_rule(self, tmp_path):
        from core.tools.approval import REMEMBER_LABEL, approval_decision_id
        from web.routes_ask_user import _inject_ask_user_answer
        agent, tool_use_id, store = self._approval_agent(tmp_path)
        did = approval_decision_id("rm -rf /tmp/x")
        assert _inject_ask_user_answer(agent, tool_use_id, f"批准命令 {REMEMBER_LABEL}") is True
        assert store.is_approved(did)
        assert store.pending is None
        # 规则已落盘（含命令），新实例回灌后仍放行
        path = tmp_path / "approvals.json"
        assert path.exists()
        assert "rm -rf /tmp/x" in path.read_text(encoding="utf-8")
        reloaded = type(store)(rules_path=path)
        assert reloaded.is_approved(did)

    def test_inject_answer_approve_session_only(self, tmp_path):
        from core.tools.approval import APPROVE_LABEL, approval_decision_id
        from web.routes_ask_user import _inject_ask_user_answer
        agent, tool_use_id, store = self._approval_agent(tmp_path)
        did = approval_decision_id("rm -rf /tmp/x")
        assert _inject_ask_user_answer(agent, tool_use_id, f"批准命令 {APPROVE_LABEL}") is True
        assert store.is_approved(did)
        assert store.pending is None
        # 仅会话级：不落盘
        assert not (tmp_path / "approvals.json").exists()

    def test_inject_answer_remember_persists_path_kind(self, tmp_path):
        """路径审批（kind=path:write）经「允许并记住」后，kind 落盘并回灌为路径规则。"""
        from types import SimpleNamespace
        from core.tools.approval import REMEMBER_LABEL, ApprovalStore
        from web.routes_ask_user import _inject_ask_user_answer
        sm, tool_use_id = TestAskUserDirectInput._placeholder_session(tmp_path)
        store = ApprovalStore(rules_path=tmp_path / "approvals.json")
        store.set_pending({
            "decision_id": "pathwrite1234567890ab",
            "command": "C:\\outside\\x.txt",
            "reason": "工作区外写入",
            "kind": "path:write",
        })
        agent = SimpleNamespace(session_manager=sm, approval_store=store)
        assert _inject_ask_user_answer(agent, tool_use_id, f"批准写入 {REMEMBER_LABEL}") is True
        assert store.is_approved("pathwrite1234567890ab")
        assert store.pending is None
        # 规则已落盘（含 kind），路径规则可被 approved_path_rules() 枚举
        path = tmp_path / "approvals.json"
        assert path.exists()
        assert "path:write" in path.read_text(encoding="utf-8")
        rules = store.approved_path_rules()
        assert any(r["kind"] == "path:write" and "outside" in r["command"] for r in rules)
        # 新实例回灌后仍按路径规则放行
        reloaded = ApprovalStore(rules_path=path)
        assert reloaded.is_approved("pathwrite1234567890ab")
        assert reloaded.approved_path_rules()

    def test_inject_answer_reject_does_not_approve(self, tmp_path):
        from core.tools.approval import REJECT_LABEL, approval_decision_id
        from web.routes_ask_user import _inject_ask_user_answer
        agent, tool_use_id, store = self._approval_agent(tmp_path)
        did = approval_decision_id("rm -rf /tmp/x")
        assert _inject_ask_user_answer(agent, tool_use_id, f"批准命令 {REJECT_LABEL}") is True
        assert not store.is_approved(did)
        assert store.pending is None
        assert not (tmp_path / "approvals.json").exists()

    def test_send_message_resumes_ask_user_with_other_answer(self, monkeypatch, tmp_path):
        """send_message 检测到待回答 ask_user 时：注入输入为"其他"答案并 resume，
        不调用 agent.run、不追加新 user message，首事件 tool_result(ask_user) 关闭卡片。"""
        import asyncio
        import json
        from types import SimpleNamespace
        from web import routes_chat as web_api

        sm, tool_use_id = self._placeholder_session(tmp_path)
        agent = SimpleNamespace(
            session_manager=sm,
            approval_store=None,
            workspace_uuid="ws-ask",
            current_session_id="sess-ask",
        )
        agent.is_running = lambda: False
        agent.resumed = []
        agent.resume_after_ask_user = lambda **kw: agent.resumed.append(kw)
        agent.run = lambda **kw: (_ for _ in ()).throw(AssertionError("agent.run 不应被调用"))

        async def fake_get_agent(ws, sid):
            return agent

        monkeypatch.setattr(web_api, "_get_or_create_agent", fake_get_agent)
        monkeypatch.setattr("core.memory_pipeline.memory_enabled", lambda *a: False)

        request = SimpleNamespace(content="我选 Python", images=None)

        async def run():
            resp = await web_api.send_message("ws-ask", "sess-ask", request)
            text = ""
            agen = resp.body_iterator
            while True:
                try:
                    chunk = await agen.__anext__()
                except StopAsyncIteration:
                    break
                text += chunk if isinstance(chunk, str) else chunk.decode()
            return text

        text = asyncio.run(run())

        # resume 被调用，run 未被调用
        assert agent.resumed, "resume_after_ask_user 应被调用"
        # 占位符已注入答案（每个问题都以输入作为"其他"回复）
        placeholder = sm.messages[-1]["content"][0]
        assert placeholder["content"] == "你喜欢哪种语言？ 我选 Python\n多久反馈一次？ 我选 Python"
        # 未追加新 user message（末条仍是占位符所在消息）
        assert sm.messages[-1]["_meta"]["id"] == "m3"
        # 首事件关闭卡片，末事件 done
        events = [json.loads(line[6:]) for line in text.split("\n") if line.startswith("data: ")]
        assert events[0]["type"] == "tool_result"
        assert events[0]["tool"] == "ask_user"
        assert events[0]["tool_use_id"] == tool_use_id
        assert events[-1]["type"] == "done"


class TestListSessionsMetaOnly:
    """list_sessions 双分支只读：新格式纯 meta 读不扫 jsonl；旧格式直读不迁移。"""

    def _make_new_session(self, ws_dir, sid="s1", preview="预览文本", message_count=5):
        sdir = ws_dir / "sessions" / sid
        sdir.mkdir(parents=True)
        body = {
            "session_id": sid,
            "name": "新会话",
            "metadata": {
                "created_at": "2026-01-01 00:00:00",
                "updated_at": "2026-01-02 00:00:00",
            },
        }
        (sdir / "index.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        (sdir / "meta.json").write_text(
            json.dumps({**body, "preview": preview, "message_count": message_count}, ensure_ascii=False),
            encoding="utf-8",
        )
        return sdir

    def test_new_format_pure_meta_read_no_jsonl(self, monkeypatch, tmp_path):
        """meta.json 含 preview/message_count → 纯 meta 读，不触发 jsonl 扫描。"""
        import asyncio
        from web import routes_workspace as web_api

        self._make_new_session(tmp_path)
        calls = []
        monkeypatch.setattr(web_api, "read_jsonl", lambda *a, **k: calls.append(a) or [])
        sessions = asyncio.run(web_api.list_sessions("ws", ws_dir=tmp_path))
        assert calls == [], "纯 meta 读不应扫描 jsonl"
        assert len(sessions["sessions"]) == 1
        s = sessions["sessions"][0]
        assert s["session_id"] == "s1"
        assert s["name"] == "新会话"
        assert s["preview"] == "预览文本"
        assert s["message_count"] == 5

    def test_old_format_reads_index_no_migration(self, tmp_path):
        """旧格式（仅 index.json）直读显示，不生成 meta.json/messages.jsonl。"""
        import asyncio
        from web import routes_workspace as web_api

        sdir = tmp_path / "sessions" / "s2"
        sdir.mkdir(parents=True)
        messages = [
            {"role": "user", "content": "旧会话问题", "_meta": {"id": "m1"}},
            {"role": "assistant", "content": "旧会话回答", "_meta": {"id": "m2"}},
        ]
        (sdir / "index.json").write_text(
            json.dumps(
                {
                    "session_id": "s2",
                    "name": "旧会话",
                    "metadata": {"created_at": "2026-01-01 00:00:00"},
                    "messages": messages,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        sessions = asyncio.run(web_api.list_sessions("ws", ws_dir=tmp_path))
        assert len(sessions["sessions"]) == 1
        s = sessions["sessions"][0]
        assert s["preview"] == "旧会话问题"
        assert s["message_count"] == 2
        # 只读不迁移
        assert not (sdir / "meta.json").exists()
        assert not (sdir / "messages.jsonl").exists()

    def test_missing_meta_fields_falls_back_to_jsonl_scan(self, tmp_path):
        """升级前写的 meta.json 缺 preview/message_count → 一次性回退 jsonl 扫描，不回写。"""
        import asyncio
        from web import routes_workspace as web_api
        from core.session import MESSAGES_FILE

        sdir = self._make_new_session(tmp_path)
        meta = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
        del meta["preview"]
        del meta["message_count"]
        (sdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        with open(sdir / MESSAGES_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "content": "回退预览"}, ensure_ascii=False) + "\n")

        sessions = asyncio.run(web_api.list_sessions("ws", ws_dir=tmp_path))
        assert len(sessions["sessions"]) == 1
        s = sessions["sessions"][0]
        assert s["preview"] == "回退预览"
        assert s["message_count"] == 1
        # 回退只读，不回写 meta.json
        meta_after = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
        assert "preview" not in meta_after
        assert "message_count" not in meta_after
