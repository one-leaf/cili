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

from core.approval_cards import (
    ABORT_TEXT,
    APPROVAL_META_KEY,
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
from core.approval_watchdog import (
    _PROCESS_STARTED_AT,
    ApprovalWatchdog,
    _can_auto_resume,
)
from core.permission_settings import ApprovalConfig
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
        assert _can_auto_resume({"auto_resume": "all", "interface": "cli"})
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
        from core.permission_settings import PermissionConfig

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
        from core.approval_cards import APPROVAL_META_KEY

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
