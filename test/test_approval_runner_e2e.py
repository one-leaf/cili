"""审批卡与真实 runner 的端到端测试：合成卡片、结算、重启后识别、单槽行为。"""

from __future__ import annotations

import time

from core.approval_cards import (
    APPROVE_TEXT,
    DECISION_APPROVE,
    apply_answer,
    find_pending_approval,
    is_expired,
    settle_expired_approvals,
)
from core.tools.permission import (
    ABORT_LABEL,
    APPROVE_LABEL,
    PermissionStore,
    permission_decision_id,
)
from test.test_approval_timeout import FakeSession, _approval_messages  # noqa: F401 —— 共享测试桩


class TestRunnerEndToEnd:
    @staticmethod
    def _card(runner, monkeypatch, *, timeout_seconds: int = 120) -> dict:
        """用真实 runner 合成审批卡，返回载荷。"""

        from core.permission_settings import PermissionConfig

        monkeypatch.setattr(
            "core.session_runner.get_permission_config",
            lambda _cfg: PermissionConfig.from_dict({
                "approval": {"timeout_seconds": timeout_seconds, "auto_resume": "web"}
            }),
        )
        permission = {
            "decision_id": permission_decision_id("rm -rf /tmp/e2e"),
            "command": "rm -rf /tmp/e2e",
            "tool": "bash",
            "kind": "command",
            "reason": "destructive",
        }
        runner.set_turn_interface("web")
        runner._handle_permission_required(permission)
        pending = find_pending_approval(runner.session, max_messages=None)
        assert pending is not None
        return pending["permission"]

    def test_entry_settles_expired_approval(self, runner, monkeypatch):
        """runner 入口兜底结算：看门狗没跑到（被淘汰/重启）也不能让新消息误答。"""
        permission = self._card(runner, monkeypatch)
        # 把期限拨到过去：等价于卡片放置超过时限
        pending = find_pending_approval(runner.session, max_messages=None)
        pending["block"]["_meta"]["pending_approval"]["deadline_ts"] = time.time() - 10

        assert runner._settle_expired_approvals() == 1
        block = runner.session.messages[-1]["content"][0]
        assert block["_meta"]["outcome"] == "expired"
        assert not runner.permission_store.is_approved(permission["decision_id"])
        # 幂等：再跑一次没有可结算项
        assert runner._settle_expired_approvals() == 0

    def test_notification_resume_waits_for_user(self, runner, monkeypatch):
        """有待答卡片时，后台通知不得抢跑恢复循环。"""
        self._card(runner, monkeypatch)
        called = []
        monkeypatch.setattr(runner, "_resume_loop", lambda sink=None: called.append(sink))
        runner.resume_from_notification()
        assert called == []

    def test_abort_ends_turn_without_llm(self, runner, monkeypatch):
        """终止：结算为 aborted、不调 LLM、不恢复循环、给出终止提示。"""
        from core.output_sink import OutputSink
        from core.turn_dispatch import execute_turn, prepare_turn

        permission = self._card(runner, monkeypatch)
        llm_calls = []

        def _fail(**_kw):
            llm_calls.append(_kw)
            raise AssertionError("终止后不应再调用 LLM")

        monkeypatch.setattr(runner.runner, "_call_llm", _fail)
        texts: list[str] = []
        prepared = prepare_turn(runner, content=ABORT_LABEL)
        assert prepared.mode == "aborted"
        execute_turn(prepared, OutputSink(on_text=texts.append), after_turn_hooks=False,
                     on_ask_user_answered=lambda *_a: None)

        assert llm_calls == []
        assert any("任务已终止" in t for t in texts)
        assert not runner.permission_store.is_approved(permission["decision_id"])
        assert runner.permission_store.pending is None
        block = runner.session.messages[-1]["content"][0]
        assert block["_meta"]["outcome"] == "aborted"

    def test_approve_resumes_and_model_sees_result_only(self, runner, monkeypatch):
        """批准：恢复循环，模型看到批准结论；审批载荷不进 LLM 上下文。"""
        import json as _json

        from core.output_sink import OutputSink
        from core.turn_dispatch import execute_turn, prepare_turn
        from test.test_session_runner_exec import _FakeLLMResponse

        permission = self._card(runner, monkeypatch)
        snapshots: list[str] = []

        def _fake_llm(**_kw):
            msgs = runner._prepare_messages_for_llm()
            snapshots.append(_json.dumps([m.to_dict() for m in msgs], ensure_ascii=False))
            return _FakeLLMResponse()

        monkeypatch.setattr(runner.runner, "_call_llm", _fake_llm)
        prepared = prepare_turn(runner, content=APPROVE_LABEL)
        assert prepared.mode == "resume"
        execute_turn(prepared, OutputSink(on_text=lambda _t: None), after_turn_hooks=False,
                     on_ask_user_answered=lambda *_a: None)

        assert snapshots, "批准后应恢复循环并调用 LLM"
        assert runner.permission_store.is_approved(permission["decision_id"])
        payload = snapshots[0]
        assert "用户已允许该操作" in payload
        assert "pending_approval" not in payload, "审批载荷不应进入模型上下文"


# ─── 重启往返：载荷与结算结果都必须能从盘上重建 ───────────────────────

class TestRestartRoundTrip:
    """重启后 PermissionStore 是空的，识别与结算只能靠盘上的载荷。

    覆盖 plan 风险 #2（落盘）与 #7（旧数据/重启）：卡片载荷经 index.json
    commits 的 block meta 持久化，结算文案经答案文件持久化。
    """

    @staticmethod
    def _reload(session):
        from core.session import SessionStore

        store = SessionStore(session.session_id, session.sessions_dir)
        assert store.load() is True
        return store

    def test_payload_survives_restart(self, runner, monkeypatch):
        from core.approval_cards import _find_tool_result_block

        permission = TestRunnerEndToEnd._card(runner, monkeypatch)
        reloaded = self._reload(runner.session)

        pending = find_pending_approval(reloaded, max_messages=None)
        assert pending is not None, "重启后应能重新发现待决审批"
        payload = pending["permission"]
        assert payload["deadline_ts"] > time.time()
        assert payload["interface"] == "web"
        assert payload["timeout_seconds"] == 120
        assert payload["decision_id"] == permission["decision_id"]

        # 重启后 store 为空：批准仍应生效并写审计（不依赖内存 pending）
        fresh_store = PermissionStore()
        outcome = apply_answer(reloaded, pending["tool_use_id"], decision=DECISION_APPROVE,
                              permission_store=fresh_store)
        assert outcome == "answered"
        assert fresh_store.is_approved(permission["decision_id"])

        # 再重启：结算结果（outcome + 答案文件）已落盘
        again = self._reload(runner.session)
        settled = _find_tool_result_block(again, pending["tool_use_id"])
        assert settled["_meta"]["completed"] is True
        assert settled["_meta"]["outcome"] == "approved"
        answer_file = again.session_dir / settled["_meta"]["output_path"]
        assert answer_file.exists()
        assert APPROVE_TEXT.strip() in answer_file.read_text(encoding="utf-8")
        assert find_pending_approval(again, max_messages=None) is None

    def test_expired_card_survives_restart_and_settles(self, runner, monkeypatch):
        permission = TestRunnerEndToEnd._card(runner, monkeypatch)
        pending = find_pending_approval(runner.session, max_messages=None)
        pending["block"]["_meta"]["pending_approval"]["deadline_ts"] = time.time() - 5
        runner.session.mark_dirty()
        runner.session.save(force=True)

        reloaded = self._reload(runner.session)
        settled = settle_expired_approvals(reloaded, permission_store=PermissionStore())
        assert settled == [pending["tool_use_id"]]
        again = self._reload(runner.session)
        from core.approval_cards import _find_tool_result_block
        assert _find_tool_result_block(again, pending["tool_use_id"])["_meta"]["outcome"] == "expired"
        assert not PermissionStore().is_approved(permission["decision_id"])


# ─── 单槽 pending 与时钟 ──────────────────────────────────────────────

class TestSingleSlotAndClock:
    def test_settling_old_card_keeps_newer_pending(self, tmp_path):
        """单槽 pending：结算旧卡不能清掉后来排队的审批（否则排队项静默丢失）。"""
        messages, permission = _approval_messages(deadline_ts=time.time() + 3600)
        session = FakeSession(messages, tmp_path)
        newer = {
            "decision_id": "newer000000000001",
            "command": "rm -rf /tmp/newer",
            "tool": "bash",
            "kind": "command",
            "deadline_ts": time.time() + 3600,
        }
        store = PermissionStore()
        store.set_pending(newer)  # 内存 pending 已被更新的卡占用

        outcome = apply_answer(session, "call_approval_001", decision=DECISION_APPROVE,
                               permission_store=store)
        assert outcome == "answered"
        assert store.pending == newer, "旧卡结算不得清掉新卡的 pending"

    def test_clock_jump_settles_instead_of_hanging(self, tmp_path):
        """时钟大幅前跳：按过期结算，不永久挂起。"""
        messages, permission = _approval_messages(deadline_ts=time.time() + 3600)
        session = FakeSession(messages, tmp_path)
        store = PermissionStore()
        store.set_pending(permission)
        pending = find_pending_approval(session)

        assert not is_expired(pending)
        assert is_expired(pending, now=permission["deadline_ts"] + 1)
        assert settle_expired_approvals(
            session, permission_store=store,
            now=permission["deadline_ts"] + 1) == ["call_approval_001"]
