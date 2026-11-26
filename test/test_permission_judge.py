# -*- coding: utf-8 -*-
"""PermissionJudge 输入上下文测试。"""

import json
from types import SimpleNamespace

from core.permission_settings import PermissionConfig
from core.security.permission_judge import (
    JudgeConfigError,
    JudgeDecision,
    PermissionJudge,
    _path_metadata,
)
from core.session import recent_user_texts
from core.session_runner_runtime.loop import should_persist_judge_decision
from core.tools.permission import PermissionStore


def _payload(judge, permission):
    messages = judge._build_input(permission)
    text = messages[0].content
    return json.loads(text.split("待判定动作：\n", 1)[1])


class TestJudgeInput:
    def test_recent_approvals_and_routing(self):
        store = PermissionStore()
        store.approve("d1", "ls", kind="command")
        store.approve("d2", "http://127.0.0.1:8000", kind="browser:navigate")
        store.approve("d3", "C:\\out", kind="path:write")
        judge = PermissionJudge(
            None, None, store,
            role="master", has_user_channel=True,
            workspace_root="E:\\ws", cwd="E:\\ws",
        )
        payload = _payload(judge, {
            "kind": "path:read",
            "command": "C:\\secret",
            "tool": "read",
            "decision_id": "d4",
            "mode": "ai",
            "reason": "outside read",
        })
        assert payload["routing"] == {"mode": "ai", "reason": "outside read"}
        assert "command: ls" in payload["recent_approvals"]
        assert "navigate: http://127.0.0.1:8000" in payload["recent_approvals"]
        assert "path:write: C:\\out" in payload["recent_approvals"]
        assert payload["path_metadata"]["path"] == "C:\\secret"
        assert "content" not in payload["path_metadata"]


def _judge(config=None, **kwargs):
    cfg = SimpleNamespace(permissions=PermissionConfig.from_dict(config or {}))
    return PermissionJudge(cfg, None, PermissionStore(), **kwargs)


def _permission(did="d1", command="ls", **extra):
    data = {"decision_id": did, "command": command, "kind": "command", "tool": "bash"}
    data.update(extra)
    return data


class TestJudgeDecisions:
    def test_failure_policy_has_no_allow(self):
        assert PermissionConfig.from_dict(
            {"judge": {"on_error": "allow"}}).judge.on_error == "ask"

    def test_failure_uses_deny_policy(self):
        judge = _judge({"judge": {"on_error": "deny"}})

        def _boom(*_args):
            raise RuntimeError("boom")

        judge._invoke = _boom
        decision = judge.decide(_permission())
        assert decision.decision == "deny"
        assert decision.source == "failure"

    def test_risk_defaults_to_medium(self):
        judge = _judge()
        judge._invoke = lambda *_args: (
            {"decision": "allow", "summary": "查看目录", "reason": "只读"}, None)
        decision = judge.decide(_permission())
        assert decision.decision == "allow"
        assert decision.risk == "medium"

    def test_intent_changes_cache_key(self):
        judge = _judge()
        calls = []

        def _invoke(messages, _cfg):
            calls.append(messages)
            return ({"decision": "allow", "risk": "low", "summary": "ok", "reason": "ok"},
                    None)

        judge._invoke = _invoke
        judge.decide(_permission(user_intent=["看看目录"]))
        judge.decide(_permission(user_intent=["删除目录"]))
        assert len(calls) == 2
        judge.decide(_permission(user_intent=["看看目录"]))
        assert len(calls) == 2  # 同意图命中缓存

    def test_deny_is_not_cached(self):
        judge = _judge()
        calls = []

        def _invoke(messages, _cfg):
            calls.append(messages)
            return ({"decision": "deny", "risk": "high", "summary": "危险", "reason": "危险"},
                    None)

        judge._invoke = _invoke
        judge.decide(_permission())
        judge.decide(_permission())
        assert len(calls) == 2

    def test_circuit_breaker_after_consecutive_denials(self):
        judge = _judge(has_user_channel=True)
        judge._invoke = lambda *_args: (
            {"decision": "deny", "risk": "high", "summary": "危险", "reason": "危险"}, None)
        first = judge.decide(_permission(did="d1"))
        second = judge.decide(_permission(did="d2"))
        third = judge.decide(_permission(did="d3"))
        assert first.decision == "deny"
        assert second.decision == "deny"
        assert third.decision == "ask"
        assert third.source == "circuit_breaker"

    def test_allow_resets_consecutive_denials(self):
        judge = _judge(has_user_channel=True)
        results = iter([
            {"decision": "deny", "risk": "high", "summary": "x", "reason": "x"},
            {"decision": "allow", "risk": "low", "summary": "x", "reason": "x"},
            {"decision": "deny", "risk": "high", "summary": "x", "reason": "x"},
        ])
        judge._invoke = lambda *_args: (next(results), None)
        judge.decide(_permission(did="d1"))
        judge.decide(_permission(did="d2"))
        decision = judge.decide(_permission(did="d3"))
        assert decision.decision == "deny"
        assert decision.source == "judge"


class TestJudgeContext:
    def test_intent_in_payload(self):
        judge = _judge()
        payload = _payload(judge, _permission(user_intent=["把构建产物清掉"]))
        assert payload["user_intent"] == ["把构建产物清掉"]
        assert payload["action_truncated"] is False

    def test_intent_disabled(self):
        judge = _judge({"judge": {"intent_context": False}})
        payload = _payload(judge, _permission(user_intent=["把构建产物清掉"]))
        assert payload["user_intent"] == []

    def test_action_truncated_flag(self):
        judge = _judge()
        payload = _payload(judge, _permission(command="x" * 8001))
        assert payload["action_truncated"] is True
        assert len(payload["action"]) == 8000

    def test_recent_user_texts_skips_synthetic(self):
        messages = [
            {"role": "user", "content": "旧请求"},
            {"role": "user", "content": [{"type": "tool_result", "content": "结果"}]},
            {"role": "user", "content": "后台通知", "_meta": {"background_notification": True}},
            {"role": "user", "content": "循环警告", "_meta": {"loop_warning": True}},
            {"role": "user", "content": "交接摘要", "_meta": {"fork": True, "handoff": True}},
            {"role": "user", "content": "任务与计划", "_meta": {"pinned": True}},
            {"role": "user", "content": "已压缩旧消息", "_meta": {"valid": False}},
            {"role": "user", "content": "文件卡", "_meta": {"file_card": True}},
            {"role": "assistant", "content": "回复"},
            {"role": "user", "content": "新请求"},
        ]
        assert recent_user_texts(messages) == ["旧请求", "新请求"]

    def test_path_metadata_fields(self, tmp_path):
        target = tmp_path / ".ssh" / "id_rsa"
        meta = _path_metadata(str(target), str(tmp_path))
        assert meta["filename"] == "id_rsa"
        assert meta["directory"] == str(target.parent)
        assert meta["sensitive"] is True
        assert "content" not in meta


class TestPersistGating:
    def _cfg(self, persist=True):
        return PermissionConfig.from_dict({"judge": {"persist_allow": persist}})

    def _decision(self, **kwargs):
        data = {"decision": "allow", "risk": "low", "summary": "ok", "reason": "ok",
                "suggested_rule": {"type": "prefix", "value": "git status"}}
        data.update(kwargs)
        return JudgeDecision(**data)

    def test_low_risk_with_suggestion(self):
        assert should_persist_judge_decision(self._decision(), self._cfg(), False) is True

    def test_medium_risk_rejected(self):
        assert should_persist_judge_decision(
            self._decision(risk="medium"), self._cfg(), False) is False

    def test_missing_suggestion_rejected(self):
        assert should_persist_judge_decision(
            self._decision(suggested_rule=None), self._cfg(), False) is False

    def test_cache_source_rejected(self):
        assert should_persist_judge_decision(
            self._decision(source="cache"), self._cfg(), False) is False

    def test_teen_unlocked_rejected(self):
        assert should_persist_judge_decision(self._decision(), self._cfg(), True) is False

    def test_persist_disabled(self):
        assert should_persist_judge_decision(
            self._decision(), self._cfg(persist=False), False) is False


class TestJudgeConfigAndText:
    def test_config_error_does_not_disable_judge(self):
        judge = _judge()
        calls = []

        def _invoke(*_args):
            calls.append(1)
            raise JudgeConfigError("bad model")

        judge._invoke = _invoke
        for i in range(4):
            decision = judge.decide(_permission(did=f"d{i}"))
            assert decision.decision == "ask"
            assert decision.source == "failure"
        assert judge._consecutive_failures == 0
        assert len(calls) == 4

    def test_circuit_breaker_no_channel_reason(self):
        judge = _judge(has_user_channel=False)
        judge._invoke = lambda *_args: (
            {"decision": "deny", "risk": "high", "summary": "危险", "reason": "危险"}, None)
        judge.decide(_permission(did="d1"))
        judge.decide(_permission(did="d2"))
        third = judge.decide(_permission(did="d3"))
        assert third.decision == "deny"
        assert third.source == "circuit_breaker"
        assert "无询问通道" in third.reason

    def test_recent_user_texts_keeps_tail(self):
        body = "日志" * 1000 + "\n删除 build 目录"
        items = recent_user_texts([{"role": "user", "content": body}])
        assert len(items) == 1
        assert "删除 build 目录" in items[0]
        assert len(items[0]) <= 600
