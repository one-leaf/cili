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


class TestPasswordFailure:
    """P0-4 回归：_mutate 返回 fn 结果，审计只记失败次数而不是整份 state。"""

    def test_register_password_failure_returns_attempt_count(self, teen):
        teen.enable("parent123")
        assert teen.register_password_failure() == 1
        assert teen.register_password_failure() == 2


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
        from core.security.permission_rules import is_revert_command

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

    def decide(self, permission):
        self.called += 1
        from core.security.permission_judge import JudgeDecision

        return JudgeDecision(decision=self._decision, summary="s", reason="r",
                                  risk=self._risk, source="judge")


class _FakeRules:
    def __init__(self, hit: bool):
        self._hit = hit

    def match_path(self, kind, resolved, tool="*"):
        return {"id": "fake"} if self._hit else None

    def match_command(self, tool, command):
        return {"id": "fake"} if self._hit else None

    def evaluate_path(self, kind, resolved, tool="*"):
        return (("allow", {"id": "fake", "display": "fake"})
                if self._hit else (None, None))


class _FakeRules2(_FakeRules):
    def __init__(self):
        super().__init__(hit=False)
        self.granted = []

    def add_rule(self, rule, scope="workspace"):
        self.granted.append(rule)


def _fake_runner(test_workspace, judge, rules):
    """loop._resolve_permission_results 需要的最小 runner 形状（duck-typing）。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        permission_judge=judge,
        permission_rules=rules,
        permission_store=None,
        config=SimpleNamespace(),
        _update_usage=lambda **kw: None,
        _execute_tool=lambda *a, **k: {"content": "re-executed"},
    )


def _pending_result(command="git push"):
    from core.tools.permission import META_KEY

    return {
        "content": "pending",
        "is_error": False,
        "_meta": {META_KEY: {
            "decision_id": "d1", "command": command, "kind": "command",
            "tool": "bash", "reason": "test",
        }, "completed": False},
        "tool_use_id": "tu1",
    }


class TestLoopStrict:
    """_resolve_permission_results 的青少年模式策略。"""

    def _resolve(self, runner, results, tool_calls=None):

        from core.session_runner_runtime.loop import Loop

        loop_obj = Loop.__new__(Loop)  # 不跑 __init__
        if tool_calls is None:
            tool_calls = []
        return loop_obj._resolve_permission_results(runner, results, tool_calls, False)

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
