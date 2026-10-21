"""TeenModeManager 单元测试：密码/限流/解锁窗口/策略/审计/工作区白名单/时长。"""

from datetime import datetime, timedelta

import pytest

from core.teen_mode import (
    SCOPE_GLOBAL,
    TeenLockedOut,
    TeenModeDenied,
    _parse_windows,
    deny_message,
)


class TestEnableAndVerify:
    """开启、密码哈希与校验。"""

    def test_enable_sets_state(self, teen):
        teen.enable("parent123")
        assert teen.is_enabled() is True
        state = teen._load()
        assert state["password"]["algo"] == "scrypt"
        # 明文不落盘
        assert "parent123" not in (teen.state_path.read_text(encoding="utf-8"))

    def test_enable_short_password_rejected(self, teen):
        with pytest.raises(ValueError):
            teen.enable("12345")
        assert teen.is_enabled() is False

    def test_verify_password_roundtrip(self, teen):
        teen.enable("parent123")
        assert teen.verify_password("parent123") is True
        assert teen.verify_password("wrong") is False

    def test_disable_requires_password(self, teen):
        teen.enable("parent123")
        with pytest.raises(TeenModeDenied):
            teen.disable("parent123")  # 未解锁窗口
        teen.unlock("parent123")
        with pytest.raises(PermissionError):
            teen.disable("wrongpw")
        teen.disable("parent123")
        assert teen.is_enabled() is False

    def test_change_password(self, teen):
        teen.enable("oldpass1")
        teen.unlock("oldpass1")
        teen.change_password("oldpass1", "newpass6")
        assert teen.verify_password("newpass6") is True
        assert teen.verify_password("oldpass1") is False


class TestRateLimit:
    """密码失败限流：连续 5 次锁 5 分钟，指数退避。"""

    def test_lockout_after_five_failures(self, teen):
        teen.enable("parent123")
        for _ in range(5):
            assert teen.verify_password("bad") is False
            teen.register_password_failure()
        with pytest.raises(TeenLockedOut):
            teen.verify_password("parent123")

    def test_successful_verify_resets_counter(self, teen):
        teen.enable("parent123")
        for _ in range(3):
            teen.register_password_failure()
        assert teen.verify_password("parent123") is True
        # 计数清零：再错 4 次不会触发锁定
        for _ in range(4):
            teen.register_password_failure()
        assert teen.verify_password("parent123") is True


class TestUnlockWindow:
    """解锁窗口：作用域、过期、手动上锁。"""

    def test_unlock_grants_window(self, teen):
        teen.enable("parent123")
        assert teen.is_unlocked() is False
        until = teen.unlock("parent123")
        assert until
        assert teen.is_unlocked() is True

    def test_unlock_expired(self, teen):
        teen.enable("parent123")
        teen.unlock("parent123")
        # 直接把到期时间改到过去（模拟时间流逝）
        def _expire(state):
            state["unlock"]["scopes"][SCOPE_GLOBAL] = "2000-01-01 00:00:00"
        teen._mutate(_expire)
        assert teen.is_unlocked() is False

    def test_lock_clears_all_scopes(self, teen):
        teen.enable("parent123")
        teen.unlock("parent123", scope="ws:abc")
        assert teen.is_unlocked("ws:abc") is True
        teen.lock()
        assert teen.is_unlocked("ws:abc") is False

    def test_wrong_password_does_not_unlock(self, teen):
        teen.enable("parent123")
        with pytest.raises(PermissionError):
            teen.unlock("wrongpw")
        assert teen.is_unlocked() is False
        # 失败已计入审计
        events = [e["event"] for e in teen.read_audit()]
        assert "unlock_failed" in events

    def test_unlock_disabled_mode_rejected(self, teen):
        with pytest.raises(TeenModeDenied):
            teen.unlock("whatever")


class TestPolicy:
    """策略读写：需解锁、白名单校验在路由层、默认值兜底。"""

    def test_policy_defaults_when_disabled(self, teen):
        assert teen.policy()["approval_strict"] is True
        assert teen.limits()["daily_minutes"] == 0

    def test_update_policy_requires_unlock(self, teen):
        teen.enable("parent123")
        with pytest.raises(TeenModeDenied):
            teen.update_policy(policy={"deny_hide": False})
        teen.unlock("parent123")
        teen.update_policy(policy={"deny_hide": False})
        assert teen.policy()["deny_hide"] is False

    def test_policy_unknown_keys_ignored(self, teen):
        teen.enable("parent123")
        teen.unlock("parent123")
        teen.update_policy(policy={"evil_key": True, "deny_share": False})
        assert "evil_key" not in teen.policy()
        assert teen.policy()["deny_share"] is False

    def test_disabled_mode_policy_returns_defaults(self, teen):
        teen.enable("parent123")
        teen.unlock("parent123")
        teen.update_policy(policy={"deny_share": False})
        teen.unlock("parent123")
        teen.disable("parent123")
        assert teen.policy()["deny_share"] is True  # 关闭时零行为差异


class TestWorkspaceAccess:
    """工作区白名单与解锁作用域。"""

    def test_disabled_allows_all(self, teen):
        assert teen.workspace_access("any-uuid") == "allow"

    def test_empty_whitelist_locks_everything(self, teen):
        teen.enable("parent123")
        assert teen.workspace_access("ws-a") == "locked"
        assert teen.workspace_access("system") == "locked"

    def test_whitelist_allows_listed(self, teen):
        teen.enable("parent123")
        teen.unlock("parent123")
        teen.update_policy(policy={"allowed_workspaces": ["ws-a"]})
        teen.lock()  # 全局解锁窗口会覆盖白名单，先回锁再验判定
        assert teen.workspace_access("ws-a") == "allow"
        assert teen.workspace_access("ws-b") == "locked"

    def test_ws_scope_unlock(self, teen):
        teen.enable("parent123")
        teen.unlock("parent123", scope="ws:abc")  # 解锁工作区 uuid=abc
        assert teen.workspace_access("abc") == "allow"
        assert teen.workspace_access("other") == "locked"

    def test_global_unlock_overrides(self, teen):
        teen.enable("parent123")
        teen.unlock("parent123")  # 全局
        assert teen.workspace_access("whatever") == "allow"

    def test_guard_workspace_raises(self, teen):
        teen.enable("parent123")
        with pytest.raises(TeenModeDenied):
            teen.guard_workspace("ws-x")
        audit = teen.read_audit()
        assert any(e.get("action") == "workspace_locked" for e in audit)


class TestAudit:
    """审计追加与读取。"""

    def test_audit_append_and_read(self, teen):
        teen.audit("enable", source="web")
        teen.audit("deny", action="session_delete", target="abc", source="web")
        entries = teen.read_audit()
        assert len(entries) == 2
        assert entries[0]["event"] == "enable"
        assert entries[1]["action"] == "session_delete"

    def test_read_audit_limit(self, teen):
        for i in range(10):
            teen.audit("deny", action=f"a{i}")
        assert len(teen.read_audit(limit=3)) == 3

    def test_audit_jsonl_format(self, teen):
        teen.audit("enable")
        line = teen.audit_path.read_text(encoding="utf-8").strip()
        assert line.startswith("{")
        import json
        data = json.loads(line)
        assert data["event"] == "enable"
        assert "ts" in data


class TestStateFileRobustness:
    """状态文件缺失/损坏/外部修改。"""

    def test_missing_file_means_disabled(self, teen):
        assert teen.is_enabled() is False

    def test_corrupted_file_falls_back_to_disabled(self, teen):
        teen.enable("parent123")
        assert teen.is_enabled() is True
        # 模拟外部写坏（指纹变化触发重读）
        teen.state_path.write_text("{not valid json", encoding="utf-8")
        # load_json_or_backup 会把损坏文件改名备份并返回 default
        assert teen.is_enabled() is False

    def test_external_edit_hot_reload(self, teen):
        teen.enable("parent123")
        assert teen.is_enabled() is True
        # 外部把 enabled 改为 False（家长手工编辑）→ 热重载察觉
        import json
        data = json.loads(teen.state_path.read_text(encoding="utf-8"))
        data["enabled"] = False
        teen.state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        assert teen.is_enabled() is False

    def test_startup_state_change_audited(self, teen):
        # 先留下「开启中」的审计，再外部关闭 → 启动检测记 startup_state_change
        teen.enable("parent123")
        import json
        data = json.loads(teen.state_path.read_text(encoding="utf-8"))
        data["enabled"] = False
        teen.state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        teen.check_startup_state()
        events = [e["event"] for e in teen.read_audit()]
        assert "startup_state_change" in events


class TestTimeLimits:
    """每日时长与允许时段。"""

    def test_no_limit_by_default(self, teen):
        teen.enable("parent123")
        teen.check_send_allowed()  # 不抛

    def test_daily_minutes_exhausted(self, teen):
        teen.enable("parent123")
        teen.unlock("parent123")
        teen.update_policy(limits={"daily_minutes": 120})
        teen.record_turn_seconds(60 * 121)  # 121 分钟
        with pytest.raises(TeenModeDenied) as exc_info:
            teen.check_send_allowed()
        assert "120" in str(exc_info.value)

    def test_time_window_outside(self, teen):
        teen.enable("parent123")
        teen.unlock("parent123")
        teen.update_policy(limits={"allowed_windows": ["00:00-00:01"]})
        # 除非恰好 00:00-00:01 运行，否则不在时段内
        now_minute = datetime.now().hour * 60 + datetime.now().minute
        if not (0 <= now_minute < 1):
            with pytest.raises(TeenModeDenied) as exc_info:
                teen.check_send_allowed()
            assert "时段" in str(exc_info.value)

    def test_time_window_inside(self, teen):
        teen.enable("parent123")
        teen.unlock("parent123")
        start = (datetime.now() - timedelta(minutes=5)).strftime("%H:%M")
        end = (datetime.now() + timedelta(minutes=5)).strftime("%H:%M")
        teen.update_policy(limits={"allowed_windows": [f"{start}-{end}"]})
        teen.check_send_allowed()  # 不抛（除整点边界极端情况）

    def test_parse_windows(self, teen):
        assert _parse_windows(["08:00-21:00"]) == [(480, 1260)]
        assert _parse_windows(["bad"]) == []
        assert _parse_windows([]) == []


class TestUsageAccounting:
    """用量记账：回合时长累计、拦截计数、跨日重置。"""

    def test_record_turn_seconds(self, teen):
        teen.record_turn_seconds(60, turns=1)
        teen.record_turn_seconds(30, turns=1)
        usage = teen.usage()
        assert usage["minutes"] == pytest.approx(1.5)
        assert usage["turns"] == 2

    def test_usage_date_reset(self, teen):
        teen.record_turn_seconds(60)
        # 伪造昨天的数据 → usage() 应重置
        import json
        data = json.loads(teen.usage_path.read_text(encoding="utf-8"))
        data["date"] = "2000-01-01"
        teen.usage_path.write_text(json.dumps(data), encoding="utf-8")
        usage = teen.usage()
        assert usage["minutes"] == 0.0

    def test_record_denied(self, teen):
        teen.record_denied("session_delete")
        teen.record_denied("session_delete")
        assert teen.usage()["denied"]["session_delete"] == 2


class TestDenyMessages:
    """被拒动作的中文文案。"""

    def test_known_actions(self, teen):
        for action in ("session_delete", "message_revert", "workspace_delete"):
            msg = deny_message(action)
            assert "青少年模式" in msg

    def test_unknown_action_default(self, teen):
        assert "家长" in deny_message("unknown_action")

    def test_denied_exception_carries_message(self, teen):
        teen.enable("parent123")
        with pytest.raises(TeenModeDenied) as exc_info:
            teen.ensure_allowed("session_delete", hard=True)
        assert "青少年模式" in str(exc_info.value)


class TestEnsureAllowed:
    """通用动作守卫：hard 语义与解锁窗口。"""

    def test_disabled_allows(self, teen):
        teen.ensure_allowed("anything", hard=True)  # 不抛

    def test_hard_denied_even_unlocked(self, teen):
        teen.enable("parent123")
        teen.unlock("parent123")
        with pytest.raises(TeenModeDenied):
            teen.ensure_allowed("session_delete", hard=True)

    def test_soft_allowed_when_unlocked(self, teen):
        teen.enable("parent123")
        with pytest.raises(TeenModeDenied):
            teen.ensure_allowed("config_update", hard=False)  # 未解锁 → 拒
        teen.unlock("parent123")
        teen.ensure_allowed("config_update", hard=False)  # 不抛

    def test_denied_recorded_in_usage(self, teen):
        teen.enable("parent123")
        with pytest.raises(TeenModeDenied):
            teen.ensure_allowed("session_delete", hard=True)
        assert teen.usage()["denied"]["session_delete"] == 1


class TestModuleSingleton:
    """模块级单例与 guard_or_raise。"""

    def test_guard_or_raise_uses_singleton(self, teen):
        from core.teen_mode import get_teen_mode, guard_or_raise, set_teen_mode

        assert get_teen_mode() is teen
        teen.enable("parent123")
        with pytest.raises(TeenModeDenied):
            guard_or_raise("session_delete", hard=True)
        set_teen_mode(None)

    def test_guard_or_raise_disabled_passes(self, teen):
        from core.teen_mode import guard_or_raise

        # 必须注入临时单例：直连真实状态文件的话，开发机若开着青少年模式
        # （data/cili/teen_mode.json enabled=true）此测试就会假失败
        guard_or_raise("session_delete", hard=True)  # 未开启不抛
