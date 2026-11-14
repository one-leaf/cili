"""审批超时/终止/自动恢复测试。

覆盖：
- 结构化决策（approve/remember/deny/abort）与旧文本 label 映射；
- 过期结算、重复提交幂等；
- 看门狗扫描：Web 自动恢复、IM 只结算、settle_only、claim 竞争；
- 配置解析与 clamp。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from core.approval_watchdog import (
    _PROCESS_STARTED_AT,
    ApprovalWatchdog,
    _can_auto_resume,
)
from core.config import ApprovalConfig
from core.tools.ask_user import (
    ABORT_TEXT,
    APPROVAL_META_KEY,
    APPROVE_TEXT,
    DECISION_ABORT,
    DECISION_APPROVE,
    DECISION_DENY,
    DECISION_REMEMBER,
    apply_answer,
    classify_pending,
    decision_from_text,
    find_pending_approval,
    is_expired,
    settle_expired_approvals,
)
from core.tools.permission import (
    ABORT_LABEL,
    APPROVE_LABEL,
    REJECT_LABEL,
    REMEMBER_LABEL,
    PermissionStore,
    permission_decision_id,
)


class FakeSession:
    """最小 SessionStore 替身：messages / session_dir / mark_dirty / save。"""

    def __init__(self, messages: list[dict], session_dir: Path):
        self.messages = messages
        self.session_dir = session_dir
        self.saved = False

    def mark_dirty(self) -> None:
        pass

    def save(self) -> None:
        self.saved = True


def _approval_messages(
    *,
    deadline_ts: float = 0,
    interface: str = "web",
    auto_resume: str = "web",
    timeout_seconds: int = 60,
) -> tuple[list[dict], dict]:
    tool_use_id = "call_approval_001"
    permission = {
        "decision_id": permission_decision_id("rm -rf /tmp/x"),
        "command": "rm -rf /tmp/x",
        "tool": "bash",
        "kind": "command",
        "reason": "destructive",
        "timeout_seconds": timeout_seconds,
        "deadline_ts": deadline_ts,
        "auto_resume": auto_resume,
        "interface": interface,
    }
    messages = [
        {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": "ask_user",
                "input": {"questions": [{
                    "question": "检测到需要确认的操作",
                    "header": "权限确认",
                    "options": [{"label": APPROVE_LABEL, "description": ""}],
                    "intent": {"kind": "approval"},
                }]},
            }],
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "等待用户确认",
                "is_error": True,
                "_meta": {
                    "completed": False,
                    "tool_name": "ask_user",
                    "kind": "approval",
                    APPROVAL_META_KEY: permission,
                },
            }],
        },
    ]
    return messages, permission


# ─── 文本 → 决策 ────────────────────────────────────────────────────────

class TestDecisionFromText:
    def test_exact_labels(self):
        assert decision_from_text(APPROVE_LABEL) == DECISION_APPROVE
        assert decision_from_text(REMEMBER_LABEL) == DECISION_REMEMBER
        assert decision_from_text(REJECT_LABEL) == DECISION_DENY
        assert decision_from_text(ABORT_LABEL) == DECISION_ABORT

    def test_legacy_suffix_and_unknown(self):
        assert decision_from_text(f"批准命令 {APPROVE_LABEL}") == DECISION_APPROVE
        assert decision_from_text(f"批准写入 {REMEMBER_LABEL}") == DECISION_REMEMBER
        assert decision_from_text("我想想") == DECISION_DENY
        assert decision_from_text("") == DECISION_DENY


# ─── 结构化决策结算 ────────────────────────────────────────────────────

class TestApplyAnswer:
    def _setup(self, tmp_path, **kwargs):
        future = time.time() + 3600
        messages, permission = _approval_messages(
            deadline_ts=kwargs.pop("deadline_ts", future), **kwargs)
        session = FakeSession(messages, tmp_path)
        store = PermissionStore()
        store.set_pending(permission)
        return session, store, permission

    def test_approve(self, tmp_path):
        session, store, permission = self._setup(tmp_path)
        pending = find_pending_approval(session)
        assert pending and pending["tool_use_id"] == "call_approval_001"
        assert classify_pending(session, store) == ("approval", "call_approval_001")

        outcome = apply_answer(session, "call_approval_001", decision=DECISION_APPROVE,
                               permission_store=store)
        assert outcome == "answered"
        assert store.is_approved(permission["decision_id"])
        block = session.messages[1]["content"][0]
        assert block["_meta"]["completed"] is True
        assert block["_meta"]["outcome"] == "approved"
        assert session.messages[0]["content"][0]["_meta"]["answered"] is True
        assert session.saved

    def test_abort_does_not_approve(self, tmp_path):
        session, store, permission = self._setup(tmp_path)
        outcome = apply_answer(session, "call_approval_001", decision=DECISION_ABORT,
                               permission_store=store)
        assert outcome == "aborted"
        assert not store.is_approved(permission["decision_id"])
        block = session.messages[1]["content"][0]
        assert block["content"] == ABORT_TEXT
        assert block["_meta"]["outcome"] == "aborted"

    def test_expired_settles_without_approval(self, tmp_path):
        session, store, permission = self._setup(tmp_path, deadline_ts=time.time() - 5)
        outcome = apply_answer(session, "call_approval_001", decision=DECISION_APPROVE,
                               permission_store=store)
        assert outcome == "expired"
        assert not store.is_approved(permission["decision_id"])
        block = session.messages[1]["content"][0]
        assert block["_meta"]["outcome"] == "expired"
        assert "审批超时" in block["content"]
        audit = tmp_path / "approval_audit.jsonl"
        assert audit.exists()
        assert '"event": "expired"' in audit.read_text(encoding="utf-8")

    def test_repeat_submit_is_ignored(self, tmp_path):
        session, store, _ = self._setup(tmp_path)
        assert apply_answer(session, "call_approval_001", decision=DECISION_DENY,
                            permission_store=store) == "answered"
        assert apply_answer(session, "call_approval_001", decision=DECISION_APPROVE,
                            permission_store=store) == "not_found"

    def test_structured_question_answers(self, tmp_path):
        messages = [
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": "call_q1", "name": "ask_user",
                "input": {"questions": [{
                    "question": "选哪种语言？", "header": "语言",
                    "options": [{"label": "Python", "description": ""},
                                {"label": "Go", "description": ""}],
                }]},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "call_q1",
                "content": "等待用户输入", "is_error": False,
                "_meta": {"completed": False, "tool_name": "ask_user"},
            }]},
        ]
        session = FakeSession(messages, tmp_path)
        outcome = apply_answer(
            session, "call_q1",
            answers=[{"id": "0", "selected": ["Go"], "custom": ""}],
        )
        assert outcome == "answered"
        block = session.messages[1]["content"][0]
        assert block["content"] == "选哪种语言？ Go"

    def test_settle_expired_only_matching(self, tmp_path):
        session, store, _ = self._setup(tmp_path, deadline_ts=time.time() - 1)
        settled = settle_expired_approvals(session, permission_store=store)
        assert settled == ["call_approval_001"]

        session2, store2, _ = self._setup(tmp_path, deadline_ts=time.time() + 3600)
        assert settle_expired_approvals(session2, permission_store=store2) == []


# ─── 回合分流（prepare_turn） ──────────────────────────────────────────

class TestPrepareTurn:
    @staticmethod
    def _runner(session):
        return SimpleNamespace(
            session=session,
            permission_store=PermissionStore(),
            permission_rules=None,
        )

    def test_abort_text_ends_turn(self, tmp_path):
        from core.turn_dispatch import prepare_turn
        messages, permission = _approval_messages(deadline_ts=time.time() + 3600)
        session = FakeSession(messages, tmp_path)
        runner = self._runner(session)
        runner.permission_store.set_pending(permission)

        prepared = prepare_turn(runner, content=ABORT_LABEL)
        assert prepared.mode == "aborted"
        assert prepared.pending_ask_user_id == "call_approval_001"
        assert session.messages[1]["content"][0]["_meta"]["outcome"] == "aborted"

    def test_expired_falls_back_to_new_message(self, tmp_path):
        from core.turn_dispatch import prepare_turn
        messages, permission = _approval_messages(deadline_ts=time.time() - 1)
        session = FakeSession(messages, tmp_path)
        runner = self._runner(session)
        runner.permission_store.set_pending(permission)

        prepared = prepare_turn(runner, content="先做别的事情")
        assert prepared.mode == "run"
        assert session.messages[1]["content"][0]["_meta"]["outcome"] == "expired"

    def test_unknown_text_denies_and_resumes(self, tmp_path):
        from core.turn_dispatch import prepare_turn
        messages, permission = _approval_messages(deadline_ts=time.time() + 3600)
        session = FakeSession(messages, tmp_path)
        runner = self._runner(session)
        runner.permission_store.set_pending(permission)

        prepared = prepare_turn(runner, content="换个方式做")
        assert prepared.mode == "resume"
        assert session.messages[1]["content"][0]["_meta"]["outcome"] == "denied"


# ─── 看门狗 ────────────────────────────────────────────────────────────

class FakeRegistry:
    def __init__(self, items: dict):
        self._items = dict(items)
        self.claimed: set[str] = set()
        self.released: list[str] = []

    def items(self):
        return list(self._items.items())

    def claim(self, key: str) -> bool:
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    def release(self, key: str) -> None:
        self.claimed.discard(key)
        self.released.append(key)


class FakeRunner:
    def __init__(self, session, permissions=None):
        self.session = session
        self.permission_store = permissions or PermissionStore()
        self.resumed = threading.Event()
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def resume_after_ask_user(self) -> None:
        self.resumed.set()


def _watchdog_case(tmp_path, *, interface="web", auto_resume="web"):
    # 期限放在进程启动之后、扫描时刻之前：既过期，又不被判为「历史遗留」。
    deadline = _PROCESS_STARTED_AT + 1
    messages, _permission = _approval_messages(
        deadline_ts=deadline, interface=interface, auto_resume=auto_resume)
    session = FakeSession(messages, tmp_path)
    runner = FakeRunner(session)
    registry = FakeRegistry({"ws:sid": runner})
    return session, runner, registry, deadline + 1


class TestWatchdog:
    def test_web_auto_resume(self, tmp_path):
        session, runner, registry, now = _watchdog_case(tmp_path, interface="web")
        watchdog = ApprovalWatchdog()
        assert watchdog.sweep_once(now=now, registry_obj=registry) == 1
        assert runner.resumed.wait(2.0), "Web 审批超时后应自动恢复"
        assert session.messages[1]["content"][0]["_meta"]["outcome"] == "expired"
        # resume 线程结束时释放执行权
        deadline = time.time() + 2
        while registry.claimed and time.time() < deadline:
            time.sleep(0.02)
        assert not registry.claimed

    def test_im_settles_without_resume(self, tmp_path):
        session, runner, registry, now = _watchdog_case(tmp_path, interface="qq")
        watchdog = ApprovalWatchdog()
        assert watchdog.sweep_once(now=now, registry_obj=registry) == 1
        assert not runner.resumed.wait(0.3), "IM 无入站消息时不应自动恢复"
        assert session.messages[1]["content"][0]["_meta"]["outcome"] == "expired"
        assert registry.released == ["ws:sid"]

    def test_stale_from_previous_process_does_not_resume(self, tmp_path):
        messages, _permission = _approval_messages(
            deadline_ts=_PROCESS_STARTED_AT - 10, interface="web")
        session = FakeSession(messages, tmp_path)
        runner = FakeRunner(session)
        registry = FakeRegistry({"ws:sid": runner})
        watchdog = ApprovalWatchdog()
        assert watchdog.sweep_once(now=time.time(), registry_obj=registry) == 1
        assert not runner.resumed.wait(0.3), "重启后历史遗留审批不应自动恢复"
        assert session.messages[1]["content"][0]["_meta"]["outcome"] == "expired"

    def test_settle_only_never_resumes(self, tmp_path):
        _session, runner, registry, now = _watchdog_case(tmp_path)
        watchdog = ApprovalWatchdog()
        assert watchdog.sweep_once(settle_only=True, now=now,
                                   registry_obj=registry) == 1
        assert not runner.resumed.wait(0.3)

    def test_claim_race_skips(self, tmp_path):
        session, _runner, registry, now = _watchdog_case(tmp_path)
        assert registry.claim("ws:sid")  # 用户答复已抢到执行权
        watchdog = ApprovalWatchdog()
        assert watchdog.sweep_once(now=now, registry_obj=registry) == 0
        assert session.messages[1]["content"][0]["_meta"]["completed"] is False

    def test_unexpired_untouched(self, tmp_path):
        messages, _permission = _approval_messages(deadline_ts=time.time() + 3600)
        session = FakeSession(messages, tmp_path)
        registry = FakeRegistry({"ws:sid": FakeRunner(session)})
        watchdog = ApprovalWatchdog()
        assert watchdog.sweep_once(now=time.time(), registry_obj=registry) == 0

    def test_auto_resume_policy(self):
        assert _can_auto_resume({"auto_resume": "web", "interface": "web"})
        assert not _can_auto_resume({"auto_resume": "web", "interface": "qq"})
        assert _can_auto_resume({"auto_resume": "all", "interface": "tui"})
        assert not _can_auto_resume({"auto_resume": "none", "interface": "web"})


# ─── 配置 ──────────────────────────────────────────────────────────────

class TestApprovalConfig:
    def test_defaults(self):
        cfg = ApprovalConfig.from_dict({})
        assert cfg.timeout_seconds == 300
        assert cfg.auto_resume == "web"

    def test_clamp_and_zero(self):
        assert ApprovalConfig.from_dict({"timeout_seconds": 5}).timeout_seconds == 30
        assert ApprovalConfig.from_dict({"timeout_seconds": 999999}).timeout_seconds == 86400
        assert ApprovalConfig.from_dict({"timeout_seconds": 0}).timeout_seconds == 0
        assert ApprovalConfig.from_dict({"timeout_seconds": "bad"}).timeout_seconds == 300

    def test_auto_resume_fallback(self):
        assert ApprovalConfig.from_dict({"auto_resume": "bogus"}).auto_resume == "web"


# ─── 卡片合成（真实 runner） ────────────────────────────────────────────

class TestPermissionCard:
    def test_card_carries_pending_payload(self, runner, monkeypatch):
        from core.config import PermissionConfig

        monkeypatch.setattr(
            "core.session_runner.get_permission_config",
            lambda _cfg: PermissionConfig.from_dict({
                "approval": {"timeout_seconds": 120, "auto_resume": "web"}
            }),
        )
        runner.set_turn_interface("web")
        runner._handle_permission_required({
            "decision_id": "d123",
            "command": "rm -rf /tmp/x",
            "tool": "bash",
            "kind": "command",
        })

        pending = find_pending_approval(runner.session)
        assert pending is not None
        payload = pending["permission"]
        assert payload["deadline_ts"] > time.time()
        assert payload["interface"] == "web"
        assert payload["auto_resume"] == "web"
        assert payload["timeout_seconds"] == 120
        block = runner.session.messages[-1]["content"][0]
        assert block["_meta"]["kind"] == "approval"
        assert APPROVAL_META_KEY in block["_meta"]

        tool_use = runner.session.messages[-2]["content"][0]
        question = tool_use["input"]["questions"][0]
        assert question["intent"]["kind"] == "approval"
        assert question["intent"]["abort"] == ABORT_LABEL
        assert question["intent"]["deadline_ts"] == payload["deadline_ts"]
        labels = [o["label"] for o in question["options"]]
        assert ABORT_LABEL in labels


# ─── 边界：无期限 / 旧卡片 fail-closed ────────────────────────────────

class TestDeadlineEdges:
    def test_timeout_zero_never_expires(self, tmp_path):
        """timeout_seconds=0（不自动过期）：看门狗与入口结算都不动它。"""
        messages, permission = _approval_messages(deadline_ts=0, timeout_seconds=0)
        session = FakeSession(messages, tmp_path)
        store = PermissionStore()
        store.set_pending(permission)

        pending = find_pending_approval(session)
        assert pending is not None
        assert not is_expired(pending)
        assert settle_expired_approvals(session, permission_store=store) == []
        # 仍可正常批准
        assert apply_answer(session, "call_approval_001", decision=DECISION_APPROVE,
                            permission_store=store) == "answered"
        assert store.is_approved(permission["decision_id"])

    def test_legacy_card_without_payload_fails_closed(self, tmp_path):
        """升级前发出、重启后丢载荷的旧卡片：不做批准副作用，按提问卡处理。"""
        from core.tools.ask_user import APPROVAL_META_KEY

        messages, permission = _approval_messages(deadline_ts=time.time() + 3600)
        meta = messages[1]["content"][0]["_meta"]
        meta.pop(APPROVAL_META_KEY)
        meta.pop("kind")
        session = FakeSession(messages, tmp_path)
        store = PermissionStore()  # 也没有 pending

        outcome = apply_answer(session, "call_approval_001", decision=DECISION_APPROVE,
                               permission_store=store)

        assert outcome == "answered"
        assert not store.is_approved(permission["decision_id"])
        assert not (tmp_path / "permission_rules.json").exists()
        block = session.messages[1]["content"][0]
        assert block["_meta"]["completed"] is True
        assert block["_meta"]["outcome"] == "answered"  # 未被当成审批结算


# ─── 看门狗：青少年模式 / 单会话失败隔离 / 幂等 ───────────────────────

class TestWatchdogGuards:
    def test_teen_mode_blocks_auto_resume(self, tmp_path, monkeypatch):
        session, runner, registry, now = _watchdog_case(tmp_path)
        monkeypatch.setattr("core.approval_watchdog._teen_allows_turn", lambda: False)
        watchdog = ApprovalWatchdog()
        assert watchdog.sweep_once(now=now, registry_obj=registry) == 1
        assert not runner.resumed.wait(0.3), "青少年模式限制期间不应自动恢复"
        assert session.messages[1]["content"][0]["_meta"]["outcome"] == "expired"
        assert registry.released == ["ws:sid"]

    def test_single_broken_session_does_not_block_others(self, tmp_path):
        session, runner, registry, now = _watchdog_case(tmp_path)

        class _Broken:
            permission_store = None

            @property
            def session(self):
                raise RuntimeError("session 加载失败")

        registry._items["ws:broken"] = _Broken()
        watchdog = ApprovalWatchdog()
        assert watchdog.sweep_once(settle_only=True, now=now,
                                   registry_obj=registry) == 1
        assert session.messages[1]["content"][0]["_meta"]["outcome"] == "expired"

    def test_settle_only_is_idempotent(self, tmp_path):
        session, _runner, registry, now = _watchdog_case(tmp_path)
        watchdog = ApprovalWatchdog()
        assert watchdog.sweep_once(settle_only=True, now=now, registry_obj=registry) == 1
        assert watchdog.sweep_once(settle_only=True, now=now, registry_obj=registry) == 0
        # 审计只记一条 expired
        audit = tmp_path / "approval_audit.jsonl"
        assert audit.read_text(encoding="utf-8").count('"event": "expired"') == 1


# ─── 真实 runner：入口结算 / 通知不抢跑 / 终止与批准的端到端 ───────────

class TestRunnerEndToEnd:
    @staticmethod
    def _card(runner, monkeypatch, *, timeout_seconds: int = 120) -> dict:
        """用真实 runner 合成审批卡，返回载荷。"""
        import time as _time

        from core.config import PermissionConfig
        from core.tools.permission import permission_decision_id

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
        from core.tools.ask_user import _find_tool_result_block

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
        from core.tools.ask_user import _find_tool_result_block
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
