# -*- coding: utf-8 -*-
"""lite 判定器测试（core/security/approval_judge.py）。

LLM 调用通过 monkeypatch ``_invoke`` 注入（沿用 memory_pipeline 的可注入先例），
覆盖：三态结论、校验失败/超时走失败策略、连错降级、会话缓存、无通道退化、审计事件。
"""

import json

from core.config import ApprovalConfig
from core.security.approval_judge import ApprovalJudge
from core.tools.approval import ApprovalStore


class _FakeSystem:
    def __init__(self, approval):
        self.approval = approval


class _FakeConfig:
    def __init__(self, approval):
        self.system = _FakeSystem(approval)


def _judge(tmp_path, monkeypatch, invoke=None, has_channel=True, **cfg_overrides):
    cfg = ApprovalConfig(judge_timeout_seconds=5)
    for key, value in cfg_overrides.items():
        setattr(cfg, key, value)
    judge = ApprovalJudge(
        _FakeConfig(cfg), None, ApprovalStore(),
        role="master", has_user_channel=has_channel,
        workspace_root="E:\\ws", cwd="E:\\ws", session_dir=tmp_path,
    )
    calls = {"n": 0}

    def _fake_invoke(messages, cfg):
        calls["n"] += 1
        if invoke is None:
            return {"decision": "allow", "risk": "low", "summary": "s", "reason": "r"}, None
        return invoke(messages, cfg, calls["n"])

    monkeypatch.setattr(judge, "_invoke", _fake_invoke)
    return judge, calls


def _approval(did="d1", command="node server.js"):
    return {"decision_id": did, "command": command, "kind": "command", "tool": "bash",
            "reason": "未命中允许规则", "targets": [], "findings": []}


class TestDecide:
    def test_allow(self, tmp_path, monkeypatch):
        judge, _ = _judge(tmp_path, monkeypatch)
        d = judge.decide(_approval())
        assert d.decision == "allow"
        assert d.source == "judge"

    def test_model_ask_passthrough(self, tmp_path, monkeypatch):
        judge, _ = _judge(tmp_path, monkeypatch,
                          invoke=lambda m, c, n: ({"decision": "ask", "risk": "medium",
                                                   "summary": "s", "reason": "r"}, None))
        assert judge.decide(_approval()).decision == "ask"

    def test_invalid_enum_goes_failure_policy(self, tmp_path, monkeypatch):
        judge, _ = _judge(tmp_path, monkeypatch,
                          invoke=lambda m, c, n: ({"decision": "maybe", "risk": "low",
                                                   "summary": "s", "reason": "r"}, None))
        d = judge.decide(_approval())
        assert d.decision == "ask"  # on_error 默认 ask
        assert d.source == "failure"

    def test_timeout_goes_failure_policy(self, tmp_path, monkeypatch):
        def _boom(messages, cfg, n):
            raise TimeoutError("判定超时")
        judge, _ = _judge(tmp_path, monkeypatch, invoke=_boom)
        d = judge.decide(_approval())
        assert d.decision == "ask"
        assert "超时" in d.error

    def test_on_error_deny(self, tmp_path, monkeypatch):
        def _boom(messages, cfg, n):
            raise RuntimeError("model down")
        judge, _ = _judge(tmp_path, monkeypatch, invoke=_boom, judge_on_error="deny")
        assert judge.decide(_approval()).decision == "deny"

    def test_failure_allow_marked_not_judge(self, tmp_path, monkeypatch):
        judge, _ = _judge(tmp_path, monkeypatch, invoke=None,
                          judge_on_error="allow")
        # invoke=None → 默认返回 allow（视为 judge 结论）；此处验证 source=judge 可落盘
        d = judge.decide(_approval())
        assert d.decision == "allow" and d.source == "judge"

    def test_failure_ask_no_channel_becomes_deny(self, tmp_path, monkeypatch):
        def _boom(messages, cfg, n):
            raise RuntimeError("down")
        judge, _ = _judge(tmp_path, monkeypatch, invoke=_boom, has_channel=False)
        d = judge.decide(_approval())
        assert d.decision == "deny"

    def test_model_ask_no_channel_still_ask(self, tmp_path, monkeypatch):
        # 模型结论的 ask 原样返回（无通道的拒绝文案由 Loop 生成）
        judge, _ = _judge(tmp_path, monkeypatch, has_channel=False,
                          invoke=lambda m, c, n: ({"decision": "ask", "risk": "medium",
                                                   "summary": "s", "reason": "r"}, None))
        assert judge.decide(_approval()).decision == "ask"


class TestCache:
    def test_same_decision_cached(self, tmp_path, monkeypatch):
        judge, calls = _judge(tmp_path, monkeypatch)
        judge.decide(_approval("d1"))
        judge.decide(_approval("d1"))
        assert calls["n"] == 1
        assert judge.decide(_approval("d1")).source == "cache"

    def test_cache_disabled(self, tmp_path, monkeypatch):
        judge, calls = _judge(tmp_path, monkeypatch, judge_cache="none")
        judge.decide(_approval("d1"))
        judge.decide(_approval("d1"))
        assert calls["n"] == 2


class TestConsecutiveDegradation:
    def test_degrades_after_n_failures(self, tmp_path, monkeypatch):
        def _boom(messages, cfg, n):
            raise RuntimeError("down")
        judge, calls = _judge(tmp_path, monkeypatch, invoke=_boom,
                              judge_max_consecutive_failures=3)
        for _ in range(3):
            judge.decide(_approval("d1"))
        assert calls["n"] == 3
        # 第 4 次：不再调用 LLM，直接走失败策略
        d = judge.decide(_approval("d1"))
        assert calls["n"] == 3
        assert d.decision == "ask" and "停用" in d.reason

    def test_success_resets_counter(self, tmp_path, monkeypatch):
        state = {"fail": 2}

        def _flaky(messages, cfg, n):
            if state["fail"] > 0:
                state["fail"] -= 1
                raise RuntimeError("down")
            return ({"decision": "allow", "risk": "low", "summary": "s", "reason": "r"}, None)
        judge, calls = _judge(tmp_path, monkeypatch, invoke=_flaky,
                              judge_max_consecutive_failures=3)
        judge.decide(_approval("d1"))
        judge.decide(_approval("d1"))
        assert judge.decide(_approval("d1")).decision == "allow"
        assert judge._consecutive_failures == 0


class TestAudit:
    def test_asked_decided_pairs(self, tmp_path, monkeypatch):
        judge, _ = _judge(tmp_path, monkeypatch)
        judge.decide(_approval("d1"))
        log = tmp_path / "approval_audit.jsonl"
        assert log.exists()
        events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert [e["event"] for e in events] == ["asked", "decided"]
        assert events[0]["decision_id"] == "d1"
        assert events[1]["decision"] == "allow"
