"""青少年模式守卫分层集成测试：

① 数据层断言（SessionStore / delete_session_dir / ExecLogStore）
② 工具层路径（PathPolicy 受保护路径硬 deny + 越界降级 + 规则挂起）
③ 循环层策略（strict 审批：占位转拒绝、不落盘、高风险拒绝）
④ 回退类命令拒绝 + 提示词块 + 用量记账
"""

import os

import pytest

from core.teen_mode import TeenModeDenied


@pytest.fixture
def ws_session(test_workspace):
    """在测试工作区里建一个真实会话目录（含 index.json/messages.jsonl）。"""
    from core.session import SessionStore

    sessions_dir = os.path.join(test_workspace, ".cili", "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    store = SessionStore.create_new_session(sessions_dir, "teen-test")
    store.add_message("user", "hello", flush=True)
    yield store
    # 清理由 conftest 的 test_workspace teardown 兜底


# ── ① 数据层断言 ─────────────────────────────────────────────────────────


class TestSessionStoreGuards:
    """SessionStore.delete / revert_to_message / delete_agent_log 守卫。"""

    def test_delete_denied_when_enabled(self, teen, ws_session):
        teen.enable("parent123")
        with pytest.raises(TeenModeDenied):
            ws_session.delete()
        # 目录还在（未真的删除）
        assert ws_session.session_dir.exists()

    def test_delete_allowed_when_disabled(self, teen, ws_session):
        ws_session.delete()
        assert not ws_session.session_dir.exists()

    def test_revert_denied_when_enabled(self, teen, ws_session):
        teen.enable("parent123")
        msg_id = ws_session.messages[-1]["_meta"]["id"]
        with pytest.raises(TeenModeDenied):
            ws_session.revert_to_message(msg_id)
        # 消息未被截断
        assert len(ws_session.messages) == 1

    def test_revert_allowed_when_disabled(self, teen, ws_session):
        msg_id = ws_session.messages[-1]["_meta"]["id"]
        deleted = ws_session.revert_to_message(msg_id)
        assert deleted == 1

    def test_exec_log_delete_denied(self, teen, ws_session):
        teen.enable("parent123")
        with pytest.raises(TeenModeDenied):
            ws_session.agent_logs.delete_agent_log("exec_abc12345")

    def test_delete_session_dir_guard(self, teen, test_workspace):
        from core.session import SessionStore, delete_session_dir

        sessions_dir = os.path.join(test_workspace, ".cili", "sessions")
        store = SessionStore.create_new_session(sessions_dir, "guard-test")
        teen.enable("parent123")
        with pytest.raises(TeenModeDenied):
            delete_session_dir(sessions_dir, store.session_id)
        assert store.session_dir.exists()
        # 关闭后可删（守卫只在开启时生效）
        teen.unlock("parent123")
        teen.disable("parent123")
        delete_session_dir(sessions_dir, store.session_id)
        assert not store.session_dir.exists()


# ── ② 工具层路径 ─────────────────────────────────────────────────────────


def _make_policy(test_workspace, tmp_path):
    from core.security.path_policy import PathPolicy

    return PathPolicy(workspace_root=test_workspace, cwd=test_workspace)


class TestPathPolicyTeen:
    """PathPolicy.status：受保护路径 / 越界降级 / 规则挂起。"""

    def test_protected_data_dir_denied(self, teen, test_workspace):
        from core.security.path_policy import OP_WRITE, PathTarget

        teen.enable("parent123")
        policy = _make_policy(test_workspace, None)
        target = PathTarget(OP_WRITE, "x", "write",
                            resolved=os.path.join(test_workspace, ".cili", "sessions", "s1"))
        assert policy.status(target) == "deny"

    def test_protected_system_data_denied(self, teen, test_workspace):
        from core.config import DATA_DIR
        from core.security.path_policy import OP_DELETE, PathTarget

        teen.enable("parent123")
        policy = _make_policy(test_workspace, None)
        target = PathTarget(OP_DELETE, "x", "delete",
                            resolved=str(DATA_DIR / "setting.json"))
        assert policy.status(target) == "deny"

    def test_cili_tmp_exception(self, teen, test_workspace):
        from core.security.path_policy import OP_WRITE, PathTarget

        teen.enable("parent123")
        policy = _make_policy(test_workspace, None)
        target = PathTarget(OP_WRITE, "x", "write",
                            resolved=os.path.join(test_workspace, ".cili", "tmp", "f.txt"))
        # tmp 子目录不受保护（临时文件不持久化）——但 teen 未解锁时区外仍按
        # outside_workspace 判定；.cili/tmp 在工作区内 → allow
        assert policy.status(target) == "allow"

    def test_outside_downgraded_to_deny(self, teen, test_workspace, monkeypatch):
        from core.security.path_policy import OP_WRITE, PathTarget

        other_tmp = os.path.join(os.path.dirname(test_workspace), "other-tmp")
        os.makedirs(other_tmp, exist_ok=True)
        monkeypatch.setenv("TMP", other_tmp)
        monkeypatch.setenv("TEMP", other_tmp)
        teen.enable("parent123")
        policy = _make_policy(test_workspace, None)
        target = PathTarget(OP_WRITE, "x", "write", resolved="D:/somewhere/outside.txt")
        # 青少年模式：现状 needs_approval → deny（无解锁窗口）
        assert policy.status(target) == "deny"

    def test_outside_normal_when_disabled(self, teen, test_workspace):
        from core.security.path_policy import OP_WRITE, PathTarget

        policy = _make_policy(test_workspace, None)
        target = PathTarget(OP_WRITE, "x", "write", resolved="D:/somewhere/outside.txt")
        # 关闭时维持现状：有审批通道 → needs_approval；无通道 → deny
        assert policy.status(target) in ("needs_approval", "deny")

    def test_rules_suspended_when_locked(self, teen, test_workspace):
        """strict 且未解锁：allow 规则挂起，先前攒下的放行不再生效。"""
        from core.security.path_policy import OP_WRITE, PathTarget

        teen.enable("parent123")
        teen.unlock("parent123")
        policy = _make_policy(test_workspace, None)
        policy.approval_rules = _FakeRules(hit=True)
        target = PathTarget(OP_WRITE, "x", "write", resolved="D:/out.txt")
        # 解锁窗口内：规则生效 → approved
        assert policy.status(target) == "approved"
        teen.lock()
        # 回锁：规则挂起 → deny（strict）
        assert policy.status(target) == "deny"


class _FakeRules:
    def __init__(self, hit: bool):
        self._hit = hit

    def match_path(self, kind, resolved, tool="*"):
        return {"id": "fake"} if self._hit else None

    def match_command(self, tool, command):
        return {"id": "fake"} if self._hit else None


# ── ③ 回退类命令 ─────────────────────────────────────────────────────────


class TestRevertCommands:
    """is_revert_command 判定 + bash 工具硬拒。"""

    @pytest.mark.parametrize("command,expected", [
        ("git reset --hard HEAD~1", True),
        ("git reset", True),
        ("git clean -fd", True),
        ("git checkout -- src/main.py", True),
        ("git checkout .", True),
        ("git checkout HEAD -- .", True),
        ("git checkout main", False),          # 分支切换不拦
        ("git restore .", True),
        ("git stash drop", True),
        ("git stash clear", True),
        ("git stash list", False),
        ("git reflog expire --all", True),
        ("git status", False),
        ("rm -rf build", True),
        ("rm -r -f build", True),
        ("rm build.txt", False),
        ("cd src && git reset --hard", True),  # 链式命令逐段检查
        ("echo hello", False),
        ("git log --oneline", False),
    ])
    def test_is_revert_command(self, command, expected):
        from core.security.approval_rules import is_revert_command

        assert is_revert_command("bash", command) is expected

    def test_bash_tool_denies_revert_when_locked(self, teen, test_workspace):
        from core.tools.bash import BashTool

        teen.enable("parent123")
        tool = BashTool(cwd=test_workspace)
        result = tool.execute(command="git reset --hard")
        assert result.error is True
        assert "青少年模式" in result.output

    def test_bash_tool_allows_revert_when_unlocked(self, teen, test_workspace):
        from core.tools.bash import BashTool

        teen.enable("parent123")
        teen.unlock("parent123")
        tool = BashTool(cwd=test_workspace)
        result = tool.execute(command="git init -q && git reset --hard 2>&1; echo done:$?")
        # 解锁窗口内放行（走正常执行；git 报错与否不关心，关键是没被 teen 拦）
        assert "青少年模式" not in result.output

    def test_bash_tool_ignores_when_disabled(self, teen, test_workspace):
        from core.tools.bash import BashTool

        tool = BashTool(cwd=test_workspace)
        result = tool.execute(command="git init -q 2>/dev/null; git reset --hard 2>/dev/null; echo ok")
        assert "青少年模式" not in result.output


# ── ④ 循环层 strict 审批 ─────────────────────────────────────────────────


class _FakeJudge:
    def __init__(self, decision="allow", risk="low"):
        self._decision = decision
        self._risk = risk
        self.called = 0

    def decide(self, approval):
        self.called += 1
        from core.security.approval_judge import ApprovalDecision

        return ApprovalDecision(decision=self._decision, summary="s", reason="r",
                                risk=self._risk, source="judge")


class _FakeRules2(_FakeRules):
    def __init__(self):
        super().__init__(hit=False)
        self.granted = []

    def add_granted(self, rule):
        self.granted.append(rule)


def _fake_runner(test_workspace, judge, rules):
    """loop._resolve_approval_results 需要的最小 runner 形状（duck-typing）。"""
    from types import SimpleNamespace


    return SimpleNamespace(
        approval_judge=judge,
        approval_rules=rules,
        approval_store=None,
        config=SimpleNamespace(),
        _update_usage=lambda **kw: None,
        _execute_tool=lambda *a, **k: {"content": "re-executed"},
    )


def _pending_result(command="git push"):
    return {
        "content": "pending",
        "is_error": False,
        "_meta": {"approval_required": {
            "decision_id": "d1", "command": command, "kind": "command",
            "tool": "bash", "reason": "test",
        }, "completed": False},
        "tool_use_id": "tu1",
    }


class TestLoopStrict:
    """_resolve_approval_results 的青少年模式策略。"""

    def _resolve(self, runner, results, tool_calls=None):

        from core.session_runner_runtime.loop import Loop

        loop_obj = Loop.__new__(Loop)  # 不跑 __init__
        if tool_calls is None:
            tool_calls = []
        return loop_obj._resolve_approval_results(runner, results, tool_calls, False)

    @staticmethod
    def _fake_tool_call(tool_use_id="tu1"):
        from types import SimpleNamespace

        return SimpleNamespace(id=tool_use_id, name="bash", parse_arguments=lambda: {})

    def test_locked_no_judge_call_no_card(self, teen, test_workspace):

        judge = _FakeJudge(decision="allow")
        rules = _FakeRules2()
        runner = _fake_runner(test_workspace, judge, rules)
        teen.enable("parent123")
        results = [_pending_result()]
        ask = self._resolve(runner, results)
        assert ask is None            # 不合成审批卡
        assert judge.called == 0      # 不调用判定器
        assert not rules.granted      # 不落盘规则
        assert results[0]["is_error"] is True
        assert "家长" in results[0]["content"]

    def test_unlocked_no_persist_high_risk_denied(self, teen, test_workspace):
        judge = _FakeJudge(decision="allow", risk="high")
        rules = _FakeRules2()
        runner = _fake_runner(test_workspace, judge, rules)
        teen.enable("parent123")
        teen.unlock("parent123")
        results = [_pending_result()]
        self._resolve(runner, results)
        assert judge.called == 1
        assert not rules.granted      # 解锁窗口内判定放行也不落盘

    def test_unlocked_low_risk_allows_reexec(self, teen, test_workspace):
        judge = _FakeJudge(decision="allow", risk="low")
        rules = _FakeRules2()
        runner = _fake_runner(test_workspace, judge, rules)
        runner._execute_tool = lambda *a, **k: {"content": "ok"}
        teen.enable("parent123")
        teen.unlock("parent123")
        results = [_pending_result()]
        self._resolve(runner, results, tool_calls=[self._fake_tool_call()])
        assert results[0]["content"] == "ok"

    def test_normal_flow_when_disabled(self, teen, test_workspace):
        judge = _FakeJudge(decision="allow", risk="low")
        rules = _FakeRules2()
        runner = _fake_runner(test_workspace, judge, rules)
        runner._execute_tool = lambda *a, **k: {"content": "ok"}
        results = [_pending_result()]
        self._resolve(runner, results)
        assert judge.called == 1
        assert len(rules.granted) >= 0  # 关闭时走原管道（落盘与否由 cfg 决定）


# ── ⑤ 提示词块 ───────────────────────────────────────────────────────────


class TestPromptBlock:
    """teen_mode system block 注入与开关。"""

    def _build(self, teen):
        from types import SimpleNamespace

        from core.prompt_builder import build_system_prompt

        runner = SimpleNamespace(
            role_cfg=SimpleNamespace(
                system_prompt={"blocks": [{"type": "text", "content": "base"}]},
            ),
            workspace_uuid="test",
            cwd=".",
            config=None,
        )
        return build_system_prompt(runner)

    def test_block_injected_when_enabled(self, teen):
        teen.enable("parent123")
        parts = self._build(teen)
        text = "\n".join(parts)
        assert "Restricted Mode (Teen Mode)" in text

    def test_no_block_when_disabled(self, teen):
        parts = self._build(teen)
        text = "\n".join(parts)
        assert "Restricted Mode (Teen Mode)" not in text

    def test_no_block_when_policy_off(self, teen):
        teen.enable("parent123")
        teen.unlock("parent123")
        teen.update_policy(policy={"prompt_block": False})
        parts = self._build(teen)
        assert "Restricted Mode (Teen Mode)" not in "\n".join(parts)


# ── ⑥ 审计完整性与用量 ───────────────────────────────────────────────────


class TestAuditTrail:
    """被拒操作全部留痕。"""

    def test_guards_write_audit(self, teen, ws_session):
        teen.enable("parent123")
        with pytest.raises(TeenModeDenied):
            ws_session.delete()
        entries = teen.read_audit()
        deny_events = [e for e in entries if e.get("event") == "deny"]
        assert any(e.get("action") == "session_delete" for e in deny_events)
        assert any(e.get("enabled") is True for e in deny_events)

    def test_state_file_survives_reinstall(self, teen):
        """重启语义：同一状态文件重新加载后模式仍生效。"""
        from core.teen_mode import TeenModeManager

        teen.enable("parent123")
        reborn = TeenModeManager(state_path=teen.state_path,
                                 audit_path=teen.audit_path,
                                 usage_path=teen.usage_path)
        assert reborn.is_enabled() is True
        with pytest.raises(TeenModeDenied):
            reborn.ensure_allowed("session_delete", hard=True)


# ── ⑦ 路由层冒烟（路由 ↔ 管理器签名契约） ────────────────────────────────


def _fake_request():
    """_csrf_protect 只读 headers.get；client.host 供审计。"""
    from types import SimpleNamespace

    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                           headers={"origin": "http://localhost:8000"})


class TestTeenRoutes:
    """直接调用路由函数：任何 TypeError（签名不匹配）/ 异常都算失败。

    教训：unlock 路由曾传 ip= 而管理器无此参数，所有解锁请求 500，
    且管理器层单测完全测不到——路由层必须有冒烟。
    """

    def test_enable_status_unlock_policy_lock_disable_roundtrip(self, teen):
        import asyncio

        from web.routes_teen import (
            TeenDisableRequest,
            TeenEnableRequest,
            TeenLockRequest,
            TeenPolicyRequest,
            TeenUnlockRequest,
            teen_disable,
            teen_enable,
            teen_lock,
            teen_status,
            teen_unlock,
            teen_update_policy,
        )

        req = _fake_request()

        # enable
        body = TeenEnableRequest(password="parent123", window_minutes=15)
        result = asyncio.run(teen_enable(req, body))
        assert result["success"] is True

        # status（公开，无 request 参数）
        status = asyncio.run(teen_status())
        assert status["enabled"] is True
        assert status["unlocked"] is False

        # unlock（正确密码 + ws 作用域）
        body = TeenUnlockRequest(password="parent123", scope="ws:abc")
        result = asyncio.run(teen_unlock(req, body))
        assert result["success"] is True
        assert result["scope"] == "ws:abc"

        # unlock（错误密码 → 401）
        import fastapi

        body = TeenUnlockRequest(password="badpass")
        try:
            asyncio.run(teen_unlock(req, body))
            raised = False
        except fastapi.HTTPException as e:
            raised = True
            assert e.status_code == 401
        assert raised

        # policy（已 unlock? ws 作用域不够改全局策略 → 403）
        body = TeenPolicyRequest(policy={"deny_hide": False})
        try:
            asyncio.run(teen_update_policy(req, body))
            policy_denied = False
        except TeenModeDenied:
            policy_denied = True
        assert policy_denied

        # 全局解锁后改策略
        asyncio.run(teen_unlock(req, TeenUnlockRequest(password="parent123")))
        body = TeenPolicyRequest(policy={"deny_hide": False}, limits={"daily_minutes": 30})
        result = asyncio.run(teen_update_policy(req, body))
        assert result["success"] is True
        assert teen.policy()["deny_hide"] is False
        assert teen.limits()["daily_minutes"] == 30

        # lock（立即回锁）
        result = asyncio.run(teen_lock(req, TeenLockRequest()))
        assert result["success"] is True
        assert teen.is_unlocked() is False

        # disable（未解锁 → 403；解锁+密码 → 成功）
        try:
            asyncio.run(teen_disable(req, TeenDisableRequest(password="parent123")))
            disable_denied = False
        except TeenModeDenied:
            disable_denied = True
        assert disable_denied

        asyncio.run(teen_unlock(req, TeenUnlockRequest(password="parent123")))
        result = asyncio.run(teen_disable(req, TeenDisableRequest(password="parent123")))
        assert result["success"] is True
        assert teen.is_enabled() is False

    def test_password_change_route(self, teen):
        import asyncio

        from web.routes_teen import (
            TeenPasswordRequest,
            TeenUnlockRequest,
            teen_change_password,
            teen_enable,
            teen_unlock,
        )

        req = _fake_request()
        asyncio.run(teen_enable(req, _enable_body("parent123")))
        asyncio.run(teen_unlock(req, TeenUnlockRequest(password="parent123")))
        body = TeenPasswordRequest(old_password="parent123", new_password="newpass99")
        result = asyncio.run(teen_change_password(req, body))
        assert result["success"] is True
        assert teen.verify_password("newpass99") is True


def _enable_body(password):
    from web.routes_teen import TeenEnableRequest

    return TeenEnableRequest(password=password, window_minutes=15)


class TestAllowedWorkspacesValidation:
    """白名单嵌套校验：跨盘符不误伤（D: vs E: 的 commonpath 会抛 ValueError）。"""

    def _patch_index(self, monkeypatch, entries):
        from web import routes_teen

        monkeypatch.setattr(routes_teen, "load_workspaces_index", lambda: entries)
        monkeypatch.setattr(
            routes_teen, "find_workspace_entry",
            lambda u: next((e for e in entries if e["uuid"] == u), None))
        return routes_teen

    def test_cross_drive_ok(self, monkeypatch):
        rt = self._patch_index(monkeypatch, [
            {"uuid": "wsd", "workspace_name": "d", "directory": "D:/a"},
            {"uuid": "wse", "workspace_name": "e", "directory": "E:/somewhere"},
        ])
        assert rt._validate_allowed_workspaces(["wsd", "wse"]) == ["wsd", "wse"]

    def test_same_drive_nesting_rejected(self, monkeypatch):
        import fastapi

        rt = self._patch_index(monkeypatch, [
            {"uuid": "parent", "workspace_name": "p", "directory": "E:/parent"},
            {"uuid": "child", "workspace_name": "c", "directory": "E:/parent/child"},
        ])
        with pytest.raises(fastapi.HTTPException) as ei:
            rt._validate_allowed_workspaces(["parent"])
        assert "嵌套" in ei.value.detail

    def test_system_and_missing_rejected(self, monkeypatch):
        import fastapi

        rt = self._patch_index(monkeypatch, [
            {"uuid": "wsx", "workspace_name": "x", "directory": "E:/x"},
        ])
        with pytest.raises(fastapi.HTTPException):
            rt._validate_allowed_workspaces(["system"])
        with pytest.raises(fastapi.HTTPException):
            rt._validate_allowed_workspaces(["no-such-uuid"])

    def test_policy_route_saves_cross_drive_whitelist(self, teen, monkeypatch):
        """端到端：跨盘符白名单经 PUT /api/teen/policy 正常落盘。"""
        import asyncio

        from web.routes_teen import TeenPolicyRequest, teen_update_policy

        self._patch_index(monkeypatch, [
            {"uuid": "wsd", "workspace_name": "d", "directory": "D:/a"},
            {"uuid": "wse", "workspace_name": "e", "directory": "E:/somewhere"},
        ])
        teen.enable("parent123")
        teen.unlock("parent123")
        body = TeenPolicyRequest(policy={"allowed_workspaces": ["wsd", "wse"]})
        result = asyncio.run(teen_update_policy(_fake_request(), body))
        assert result["success"] is True
        assert teen.policy()["allowed_workspaces"] == ["wsd", "wse"]
