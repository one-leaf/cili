"""web 会话域测试：runner 回收、全局事件、提问/审批回答、发送消息。"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class TestEvictIdleAgent:
    """SessionRegistry.evict_idle() LRU eviction logic."""

    def _snapshot(self):
        from core.session_registry import registry
        return dict(registry._hosted), dict(registry._access)

    def _restore(self, snapshot):
        from core.session_registry import registry
        hosted, access = snapshot
        registry._hosted.clear()
        registry._hosted.update(hosted)
        registry._access.clear()
        registry._access.update(access)

    def test_no_eviction_when_under_limit(self):
        from core.session_registry import registry
        snap = self._snapshot()
        try:
            registry._hosted.clear()
            registry._access.clear()
            registry.evict_idle()  # Should not raise
        finally:
            self._restore(snap)

    def test_evicts_oldest_idle(self):
        from core.session_registry import HostedSession, registry
        snap = self._snapshot()
        try:
            registry._hosted.clear()
            registry._access.clear()

            for i in range(registry.max_runners + 3):
                agent = MagicMock()
                agent.is_running.return_value = False
                agent.cleanup.return_value = None
                key = f"test-{i}"
                registry._hosted[key] = HostedSession(key=key, runner=agent)
                registry._access[key] = 1000 + i

            registry.evict_idle()

            assert "test-0" not in registry._hosted
            assert "test-2" not in registry._hosted
            # P1-3：evict 循环淘汰直至回落上限（旧语义一次只淘汰一个，
            # 突发负载后会长期超限）
            assert len(registry._hosted) == registry.max_runners
        finally:
            self._restore(snap)

    def test_does_not_evict_running_agent(self):
        from core.session_registry import HostedSession, registry
        snap = self._snapshot()
        try:
            registry._hosted.clear()
            registry._access.clear()

            for i in range(registry.max_runners + 2):
                agent = MagicMock()
                agent.is_running.return_value = True
                key = f"test-{i}"
                registry._hosted[key] = HostedSession(key=key, runner=agent)
                registry._access[key] = 1000 + i

            registry.evict_idle()

            assert len(registry._hosted) == registry.max_runners + 2
        finally:
            self._restore(snap)

    def test_any_running_with_claims(self):
        """any_running：claims 命中即视为忙（覆盖认领到运行之间的窗口）"""
        from core.session_registry import registry
        snap = self._snapshot()
        try:
            registry._hosted.clear()
            registry._access.clear()
            # 注意 any_running 内部要拿 _claims_lock（非重入），外层不能持锁调用
            with registry._claims_lock:
                registry._claims.add("w:s")
            assert registry.any_running() is True
            with registry._claims_lock:
                registry._claims.discard("w:s")
            assert registry.any_running() is False
        finally:
            self._restore(snap)

    def test_any_running_with_runner_state(self):
        """any_running：无 claims 时逐个检查 runner 的 is_running"""
        from core.session_registry import HostedSession, registry
        snap = self._snapshot()
        try:
            registry._hosted.clear()
            registry._access.clear()
            agent = MagicMock()
            agent.is_running.return_value = False
            registry._hosted["k"] = HostedSession(key="k", runner=agent)
            assert registry.any_running() is False
            agent.is_running.return_value = True
            assert registry.any_running() is True
        finally:
            self._restore(snap)


class TestGlobalEvents:
    """GET /api/events 全局 SSE 事件流测试（直接调端点函数，迭代 body_iterator）。

    不用 TestClient：TestClient + asyncio.to_thread 组合会死锁挂起，且
    TestClient 的 client IP "testclient" 不在 LOCALHOST_IPS 会 403。
    """

    def test_event_delivery_and_session_filter(self):
        """事件按 session_id 过滤投递；不匹配的事件被过滤"""
        import asyncio

        from core.event_bus import get_event_bus
        from web.routes_chat import stream_global_events

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

    def test_types_bypass_session_filter(self):
        """types 里的事件类型不受 session_id 过滤（跨会话的工作区级变更）"""
        import asyncio

        from core.event_bus import get_event_bus
        from web.routes_chat import stream_global_events

        async def run():
            resp = await stream_global_events(
                session_id="sess-a", types="workflow_changed,files_changed")
            agen = resp.body_iterator

            get_event_bus().publish({
                "type": "text", "workspace_uuid": "ws-x", "session_id": "sess-other",
                "content": "should-not-arrive",
            })
            get_event_bus().publish({
                "type": "workflow_changed", "workspace_uuid": "ws-x",
                "workflow_id": "wf1", "action": "created",
            })

            chunk = await agen.__anext__()
            text = chunk if isinstance(chunk, str) else chunk.decode()
            assert "workflow_changed" in text
            assert "should-not-arrive" not in text
            await agen.aclose()
            return True

        assert asyncio.run(run())

    def test_unsubscribe_on_close(self):
        """流关闭后退订（finally unsubscribe），新订阅不受影响"""
        import asyncio

        from core.event_bus import get_event_bus
        from web.routes_chat import stream_global_events

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

    def test_master_on_tool_output_publishes_to_event_bus(self, monkeypatch, tmp_path, config):
        """回归：master 的 _on_tool_output 必须发布到事件总线。

        此前 _get_or_create_runner 里 event bus 变量被后面的 get_agent_mailbox() 闭包
        晚绑定遮蔽成 AgentMailbox，publish 抛 AttributeError 被静默吞掉，
        导致前端收不到 master 执行 bash/python 的 tool_output 推流。
        """
        import asyncio
        import queue

        from core.event_bus import get_event_bus
        from web import deps as web_api

        ws, sid = "ws-regress", "sess-regress-1"
        (tmp_path / "sessions").mkdir(exist_ok=True)
        monkeypatch.setattr(web_api, "_get_workspace_info", lambda uuid: {"directory": str(tmp_path)})
        # runner 池建 runner 时会 load_config()；CI 无 setting.json，注入测试配置
        monkeypatch.setattr("core.session_registry.load_config", lambda: config)
        # Agent._init_interactive 内部通过 get_workspace_data_dir 解析数据目录
        monkeypatch.setattr("core.config.get_workspace_data_dir", lambda uuid: tmp_path)
        # 测试结束后清理全局 runner 池，避免泄漏
        from core.session_registry import registry
        monkeypatch.setattr(registry, "_hosted", {})

        async def run():
            agent = await web_api._get_or_create_runner(ws, sid)
            q = queue.Queue()
            get_event_bus().subscribe(q, ws, sid)
            try:
                # 直接调用 master 工具输出回调，验证走事件总线而非 AgentMailbox
                agent.default_sink.on_tool_output("bash", "行 1\n", 7, "call_regress_1")
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
        from core.approval_cards import find_pending_ask_user
        sm, tool_use_id = self._placeholder_session(tmp_path)
        assert find_pending_ask_user(sm) == tool_use_id

    def test_find_pending_ask_user_none_when_answered(self, tmp_path):
        from core.approval_cards import find_pending_ask_user
        sm, tool_use_id = self._placeholder_session(tmp_path)
        sm.messages[-1]["content"][0]["_meta"]["completed"] = True
        assert find_pending_ask_user(sm) is None

    def test_build_other_answer_formats_questions(self, tmp_path):
        from core.approval_cards import build_other_answer
        sm, tool_use_id = self._placeholder_session(tmp_path)
        answer = build_other_answer(sm, tool_use_id, "我选 Python")
        assert answer == "你喜欢哪种语言？ 我选 Python\n多久反馈一次？ 我选 Python"

    def test_inject_ask_user_answer(self, tmp_path):
        from core.approval_cards import inject_ask_user_answer
        sm, tool_use_id = self._placeholder_session(tmp_path)
        ok = inject_ask_user_answer(sm, tool_use_id, "你喜欢哪种语言？ Python", None)
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
        from core.approval_cards import inject_ask_user_answer
        sm, _ = self._placeholder_session(tmp_path)
        assert inject_ask_user_answer(sm, "call_missing", "x", None) is False

    # ─── 审批分支三态 ─────────────────────────────────────────────

    @staticmethod
    def _as_approval_card(sm, tool_use_id, permission):
        """把占位卡按真实审批卡形状标记（header + kind + 载荷）。

        卡片类型以卡片自身为准（kind/header/载荷），PermissionStore.pending 只是
        残留状态的兜底——所以审批用例必须造出真实审批卡，不能靠 pending 顶替。
        """
        sm.messages[1]["content"][0]["input"]["questions"][0]["header"] = "权限确认"
        sm.messages[2]["content"][0]["_meta"].update({
            "kind": "approval",
            "pending_approval": dict(permission),
        })

    @staticmethod
    def _approval_agent(tmp_path):
        """构造带待批准命令的 agent（permission_store.pending 非空）。"""

        from core.security.permission_rules import PermissionRules
        from core.tools.permission import PermissionStore, permission_decision_id
        sm, tool_use_id = TestAskUserDirectInput._placeholder_session(tmp_path)
        store = PermissionStore()
        store.set_pending({
            "decision_id": permission_decision_id("rm -rf /tmp/x"),
            "command": "rm -rf /tmp/x",
            "reason": "destructive recursive delete",
        })
        TestAskUserDirectInput._as_approval_card(sm, tool_use_id, store.pending)
        rules = PermissionRules(tmp_path / "permission_rules.json")
        agent = SimpleNamespace(session=sm, permission_store=store, permission_rules=rules)
        return agent, tool_use_id, store, rules

    def test_inject_answer_remember_persists_rule(self, tmp_path):
        from core.approval_cards import inject_ask_user_answer
        from core.security.permission_rules import PermissionRules
        from core.tools.permission import REMEMBER_LABEL, permission_decision_id
        agent, tool_use_id, store, rules = self._approval_agent(tmp_path)
        did = permission_decision_id("rm -rf /tmp/x")
        assert inject_ask_user_answer(agent.session, tool_use_id,
                                      f"批准命令 {REMEMBER_LABEL}", store, rules) is True
        assert store.is_approved(did)
        assert store.pending is None
        # 规则已落盘（含命令），新实例回灌后仍放行
        path = tmp_path / "permission_rules.json"
        assert path.exists()
        assert "rm -rf /tmp/x" in path.read_text(encoding="utf-8")
        reloaded = PermissionRules(path)
        assert reloaded.evaluate_command("bash", "rm -rf /tmp/x")[0] == "allow"

    def test_inject_answer_approve_session_only(self, tmp_path):
        from core.approval_cards import inject_ask_user_answer
        from core.tools.permission import APPROVE_LABEL, permission_decision_id
        agent, tool_use_id, store, rules = self._approval_agent(tmp_path)
        did = permission_decision_id("rm -rf /tmp/x")
        assert inject_ask_user_answer(agent.session, tool_use_id,
                                      f"批准命令 {APPROVE_LABEL}", store, rules) is True
        assert store.is_approved(did)
        assert store.pending is None
        # 仅会话级：不落盘
        assert not (tmp_path / "permission_rules.json").exists()

    def test_inject_answer_remember_persists_path_kind(self, tmp_path):
        """路径审批（kind=path:write）经「一直允许」后，kind 落盘并回灌为路径规则。"""
        from core.approval_cards import inject_ask_user_answer
        from core.security.permission_rules import PermissionRules
        from core.tools.permission import REMEMBER_LABEL, PermissionStore
        sm, tool_use_id = TestAskUserDirectInput._placeholder_session(tmp_path)
        store = PermissionStore()
        store.set_pending({
            "decision_id": "pathwrite1234567890ab",
            "command": "C:\\outside\\x.txt",
            "reason": "工作区外写入",
            "kind": "path:write",
        })
        TestAskUserDirectInput._as_approval_card(sm, tool_use_id, store.pending)
        rules = PermissionRules(tmp_path / "permission_rules.json")
        assert inject_ask_user_answer(sm, tool_use_id, f"批准写入 {REMEMBER_LABEL}",
                                      store, rules) is True
        assert store.is_approved("pathwrite1234567890ab")
        assert store.pending is None
        # 规则已落盘（含 kind），新实例回灌后仍按路径规则放行
        path = tmp_path / "permission_rules.json"
        assert path.exists()
        assert "path:write" in path.read_text(encoding="utf-8")
        reloaded = PermissionRules(path)
        assert reloaded.evaluate_path("path:write", "C:\\outside\\x.txt")[0] == "allow"
        assert reloaded.evaluate_path("path:write", "C:\\outside\\other.txt")[0] == "allow"
        assert reloaded.evaluate_path("path:delete", "C:\\outside\\x.txt")[0] is None  # kind 隔离

    def test_inject_answer_reject_does_not_approve(self, tmp_path):
        from core.approval_cards import inject_ask_user_answer
        from core.tools.permission import REJECT_LABEL, permission_decision_id
        agent, tool_use_id, store, rules = self._approval_agent(tmp_path)
        did = permission_decision_id("rm -rf /tmp/x")
        assert inject_ask_user_answer(agent.session, tool_use_id,
                                      f"批准命令 {REJECT_LABEL}", store, rules) is True
        assert not store.is_approved(did)
        assert store.pending is None
        assert not (tmp_path / "permission_rules.json").exists()

    def test_send_message_resumes_ask_user_with_other_answer(self, monkeypatch, tmp_path):
        """send_message 检测到待回答 ask_user 时：注入输入为"其他"答案并 resume，
        不调用 agent.run、不追加新 user message，首事件 tool_result(ask_user) 关闭卡片。"""
        import asyncio
        import json

        from web import routes_chat as web_api

        sm, tool_use_id = self._placeholder_session(tmp_path)
        agent = SimpleNamespace(
            session=sm,
            permission_store=None,
            permission_rules=None,
            workspace_uuid="ws-ask",
            current_session_id="sess-ask",
        )
        agent.is_running = lambda: False
        agent.resumed = []
        agent.resume_after_ask_user = lambda **kw: agent.resumed.append(kw)
        agent.run = lambda **kw: (_ for _ in ()).throw(AssertionError("agent.run 不应被调用"))

        async def fake_get_agent(ws, sid):
            return agent

        monkeypatch.setattr(web_api, "_get_or_create_runner", fake_get_agent)
        monkeypatch.setattr("core.memory_service.memory_enabled", lambda *a: False)

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


class TestSendMessageInterface:
    """interface 字段：run_runner 按 request.interface 调 set_turn_interface（TUI 客户端接入点）。"""

    def test_interface_passed_to_set_turn_interface(self, monkeypatch):
        import asyncio

        from web import routes_chat as web_api

        calls = []
        agent = SimpleNamespace(
            session=SimpleNamespace(messages=[]),
            permission_store=None,
            permission_rules=None,
            workspace_uuid="ws-if",
            current_session_id="sess-if",
        )
        agent.is_running = lambda: False
        agent.set_turn_interface = lambda name: calls.append(name)
        agent.run = lambda **kw: None

        async def fake_get_agent(ws, sid):
            return agent

        monkeypatch.setattr(web_api, "_get_or_create_runner", fake_get_agent)
        monkeypatch.setattr("core.memory_service.memory_enabled", lambda *a: False)

        request = SimpleNamespace(content="hi", images=None, profile="cli")

        async def run():
            resp = await web_api.send_message("ws-if", "sess-if", request)
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

        # 回合内置为 cli，finally 复位为 None
        assert calls == ["cli", None]
        assert '"done"' in text

    def test_interface_defaults_to_web(self, monkeypatch):
        """缺省 interface（旧前端不带该字段）→ 行为不变，仍按 web 驱动。"""
        import asyncio

        from web import routes_chat as web_api

        calls = []
        agent = SimpleNamespace(
            session=SimpleNamespace(messages=[]),
            permission_store=None,
            permission_rules=None,
            workspace_uuid="ws-if",
            current_session_id="sess-if",
        )
        agent.is_running = lambda: False
        agent.set_turn_interface = lambda name: calls.append(name)
        agent.run = lambda **kw: None

        async def fake_get_agent(ws, sid):
            return agent

        monkeypatch.setattr(web_api, "_get_or_create_runner", fake_get_agent)
        monkeypatch.setattr("core.memory_service.memory_enabled", lambda *a: False)

        request = SimpleNamespace(content="hi", images=None)  # 无 interface 属性 → 缺省 web（getattr 约定）

        async def run():
            resp = await web_api.send_message("ws-if", "sess-if", request)
            agen = resp.body_iterator
            async for _ in agen:
                pass

        asyncio.run(run())
        assert calls == ["web", None]


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

        from core.session import MESSAGES_FILE
        from web import routes_workspace as web_api

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


class TestAnswerAskUserApprovalRoute:
    """``/ask-user/answers`` 的结构化决策分支。

    覆盖审批卡四态（approve/remember/deny/abort）、过期结算、卡片类型错配拦截
    与旧客户端 ``answer`` 文本兼容；断言「终止不恢复循环」「过期不恢复循环」。
    """

    @staticmethod
    def _session(tmp_path, *, deadline_ts: float, header: str = "权限确认", payload: bool = True):

        tool_use_id = "call_approval_route"
        permission = {
            "decision_id": "route1234567890ab",
            "command": "rm -rf /tmp/x",
            "tool": "bash",
            "kind": "command",
            "reason": "destructive",
            "timeout_seconds": 60,
            "deadline_ts": deadline_ts,
            "auto_resume": "web",
            "profile": "web",
        }
        meta = {"tool_name": "ask_user", "completed": False, "kind": "approval"}
        if payload:
            meta["pending_approval"] = permission
        messages = [
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": tool_use_id, "name": "ask_user",
                "input": {"questions": [{
                    "question": "检测到需要确认的操作", "header": header,
                    "options": [{"label": "允许本次会话", "description": ""}],
                    "intent": {"kind": "approval"},
                }]},
            }], "_meta": {"id": "m1"}},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tool_use_id,
                "content": "等待用户确认", "is_error": True, "_meta": meta,
            }], "_meta": {"id": "m2"}},
        ]
        session_dir = tmp_path / "sessions" / "sess-approval"
        session_dir.mkdir(parents=True, exist_ok=True)
        sm = SimpleNamespace(messages=messages, session_dir=session_dir)
        sm.saved = []
        sm.save = lambda: sm.saved.append(True)
        sm.mark_dirty = lambda: None
        return sm, tool_use_id, permission

    @staticmethod
    def _runner(sm, permission):

        from core.tools.permission import PermissionStore

        store = PermissionStore()
        if permission is not None:
            store.set_pending(permission)
        runner = SimpleNamespace(
            session=sm,
            permission_store=store,
            permission_rules=None,
            current_session_id="sess-approval",
            workspace_uuid="ws-approval",
            default_sink=None,
            resume_calls=[],
            stopped=False,
        )
        runner.resume_after_ask_user = lambda sink=None: runner.resume_calls.append(sink)

        def _no_run(**_kw):
            raise AssertionError("路由不应走 run()")

        runner.run = _no_run
        runner.stop = lambda: setattr(runner, "stopped", True)
        runner.set_turn_interface = lambda name: setattr(runner, "turn_interface", name)
        return runner, store

    @staticmethod
    def _patch(monkeypatch, runner):

        from web import routes_ask_user as routes

        class _Registry:
            @staticmethod
            def get(key):
                return runner

        released = []
        monkeypatch.setattr(routes, "registry", _Registry())
        monkeypatch.setattr(routes, "_claim_session_run", lambda key: True)
        monkeypatch.setattr(routes, "_release_session_run", lambda key: released.append(key))
        monkeypatch.setattr(routes, "get_teen_mode", lambda: SimpleNamespace(
            guard_workspace=lambda *a, **kw: None))
        return released

    @staticmethod
    def _post(routes, request):
        import asyncio
        import json

        async def run():
            resp = await routes.answer_ask_user("ws-approval", "sess-approval", request)
            text = ""
            agen = resp.body_iterator
            while True:
                try:
                    chunk = await agen.__anext__()
                except StopAsyncIteration:
                    break
                text += chunk if isinstance(chunk, str) else chunk.decode()
            return [json.loads(line[6:]) for line in text.split("\n")
                    if line.startswith("data: ")]

        return asyncio.run(run())

    def test_abort_does_not_resume_and_ends_turn(self, tmp_path, monkeypatch):
        import time

        from web import routes_ask_user as routes

        sm, tool_use_id, permission = self._session(tmp_path, deadline_ts=time.time() + 3600)
        runner, store = self._runner(sm, permission)
        released = self._patch(monkeypatch, runner)

        events = self._post(routes, routes.AnswerAskUserRequest(
            tool_use_id=tool_use_id, decision="abort"))

        assert runner.resume_calls == [], "终止后不应恢复循环（否则模型会继续执行）"
        block = sm.messages[1]["content"][0]
        assert block["_meta"]["outcome"] == "aborted"
        assert block["_meta"]["completed"] is True
        assert not store.is_approved(permission["decision_id"])
        assert store.pending is None
        assert released == ["ws-approval:sess-approval"]
        # 首帧关闭卡片，另有终止提示
        assert events[0]["type"] == "tool_result"
        assert events[0]["tool_use_id"] == tool_use_id
        assert any(e["type"] == "text" and "任务已终止" in e.get("content", "") for e in events)
        assert events[-1]["type"] == "done"

    def test_approve_resumes_once(self, tmp_path, monkeypatch):
        import time

        from web import routes_ask_user as routes

        sm, tool_use_id, permission = self._session(tmp_path, deadline_ts=time.time() + 3600)
        runner, store = self._runner(sm, permission)
        self._patch(monkeypatch, runner)

        self._post(routes, routes.AnswerAskUserRequest(
            tool_use_id=tool_use_id, decision="approve"))

        assert len(runner.resume_calls) == 1
        assert store.is_approved(permission["decision_id"])
        assert sm.messages[1]["content"][0]["_meta"]["outcome"] == "approved"

    def test_legacy_answer_label_still_works(self, tmp_path, monkeypatch):
        import time

        from core.tools.permission import REMEMBER_LABEL
        from web import routes_ask_user as routes

        sm, tool_use_id, permission = self._session(tmp_path, deadline_ts=time.time() + 3600)
        runner, store = self._runner(sm, permission)
        self._patch(monkeypatch, runner)

        # 旧前端只发 answer 文本：按 label 映射为决策（不能当成提问卡自由文本）
        self._post(routes, routes.AnswerAskUserRequest(
            tool_use_id=tool_use_id, answer=REMEMBER_LABEL))

        assert len(runner.resume_calls) == 1
        assert sm.messages[1]["content"][0]["_meta"]["outcome"] == "remembered"

    def test_expired_returns_error_without_resume(self, tmp_path, monkeypatch):
        import time

        from web import routes_ask_user as routes

        sm, tool_use_id, permission = self._session(tmp_path, deadline_ts=time.time() - 5)
        runner, store = self._runner(sm, permission)
        self._patch(monkeypatch, runner)

        events = self._post(routes, routes.AnswerAskUserRequest(
            tool_use_id=tool_use_id, decision="approve"))

        assert runner.resume_calls == []
        assert not store.is_approved(permission["decision_id"])
        assert sm.messages[1]["content"][0]["_meta"]["outcome"] == "expired"
        assert any(e["type"] == "error" and "超时" in e.get("content", "") for e in events)

    def test_decision_on_question_card_rejected(self, tmp_path, monkeypatch):
        from fastapi import HTTPException

        from web import routes_ask_user as routes

        sm, tool_use_id = TestAskUserDirectInput._placeholder_session(tmp_path)
        runner, _store = self._runner(sm, None)
        self._patch(monkeypatch, runner)

        with pytest.raises(HTTPException) as exc:
            self._post(routes, routes.AnswerAskUserRequest(
                tool_use_id=tool_use_id, decision="approve"))
        assert exc.value.status_code == 400

    def test_answers_on_approval_card_rejected(self, tmp_path, monkeypatch):
        import time

        from fastapi import HTTPException

        from web import routes_ask_user as routes

        sm, tool_use_id, permission = self._session(tmp_path, deadline_ts=time.time() + 3600)
        runner, _store = self._runner(sm, permission)
        self._patch(monkeypatch, runner)

        with pytest.raises(HTTPException) as exc:
            self._post(routes, routes.AnswerAskUserRequest(
                tool_use_id=tool_use_id,
                answers=[{"id": "0", "selected": ["允许本次会话"]}]))
        assert exc.value.status_code == 400
        # 拦截后卡片仍待决、未产生副作用
        assert sm.messages[1]["content"][0]["_meta"]["completed"] is False


class TestAnswerAskUserQuestionRoute:
    """提问卡走结构化 ``answers``：结算 → 恢复 → 模型可读文本。"""

    @staticmethod
    def _placeholder(sm, tool_use_id):
        for msg in sm.messages:
            if not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if block.get("type") == "tool_result" and block.get("tool_use_id") == tool_use_id:
                    return block
        raise AssertionError("未找到占位符")

    @staticmethod
    def _tool_use(sm, tool_use_id):
        for msg in sm.messages:
            if not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if block.get("type") == "tool_use" and block.get("id") == tool_use_id:
                    return block
        raise AssertionError("未找到 tool_use")

    def test_structured_answers_resume(self, tmp_path, monkeypatch):
        from web import routes_ask_user as routes

        helper = TestAnswerAskUserApprovalRoute
        sm, tool_use_id = TestAskUserDirectInput._placeholder_session(tmp_path)
        runner, _store = helper._runner(sm, None)
        helper._patch(monkeypatch, runner)

        helper._post(routes, routes.AnswerAskUserRequest(
            tool_use_id=tool_use_id,
            answers=[{"id": "0", "selected": ["Python"], "custom": ""},
                     {"id": "1", "selected": [], "custom": "每周"}]),
        )

        assert len(runner.resume_calls) == 1
        block = self._placeholder(sm, tool_use_id)
        assert block["content"] == "你喜欢哪种语言？ Python\n多久反馈一次？ 每周"
        assert block["_meta"]["outcome"] == "answered"
        assert self._tool_use(sm, tool_use_id)["_meta"]["answered"] is True

    def test_legacy_text_answer_resume(self, tmp_path, monkeypatch):
        """Web 前端实际发的是 ``answer`` 文本（问题 答案 分行），不是 answers。"""
        from web import routes_ask_user as routes

        helper = TestAnswerAskUserApprovalRoute
        sm, tool_use_id = TestAskUserDirectInput._placeholder_session(tmp_path)
        runner, _store = helper._runner(sm, None)
        helper._patch(monkeypatch, runner)

        answer = "你喜欢哪种语言？ Python\n多久反馈一次？ 每周"
        helper._post(routes, routes.AnswerAskUserRequest(
            tool_use_id=tool_use_id, answer=answer))

        assert len(runner.resume_calls) == 1
        assert self._placeholder(sm, tool_use_id)["content"] == answer
