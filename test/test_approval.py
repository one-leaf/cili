# -*- coding: utf-8 -*-
"""权限模型测试（permission_rules / permission_policy / PermissionStore）。

旧 allow-only 审批模型的测试已由本文件替换：
- 规则 decision=allow/deny/ask 与 deny > ask > allow 优先级；
- 段级匹配、路径/URL 匹配、只读白名单、内置 deny；
- 默认策略、外部读取（含 ai 路径元数据）、无通道 fail-closed；
- 工具集成与卡片文案。
"""

import json

from core.config import PermissionConfig
from core.security.permission_policy import PermissionPolicy
from core.security.permission_rules import (
    PermissionRules,
    build_granted_rule,
    implied_path_rules,
    is_readonly_command,
    is_safe_create_command,
)
from core.tools.bash import BashTool
from core.tools.base import ToolResult
from core.tools.browser import BrowserTool
from core.tools.permission import (
    APPROVE_LABEL,
    META_KEY,
    REJECT_LABEL,
    REMEMBER_LABEL,
    PermissionStore,
    build_permission_question,
    denied_rule_text,
    judge_deny_text,
    judge_unavailable_text,
    no_channel_text,
    permission_decision_id,
    permission_placeholder_text,
    user_reject_hint,
)
from core.tools.read import ReadTool


def _rule(decision="allow", match_type="prefix", value="git status",
          tool="bash", kind="command", enabled=True):
    return {
        "id": f"r-{decision}-{match_type}-{value}",
        "enabled": enabled,
        "decision": decision,
        "source": "user",
        "tool": tool,
        "kind": kind,
        "match": {"type": match_type, "value": value},
        "note": value,
    }


def _rules(tmp_path, rules):
    path = tmp_path / "approval_rules.json"
    path.write_text(json.dumps({"version": 1, "rules": rules, "constraints": []},
                               ensure_ascii=False), encoding="utf-8")
    return PermissionRules(path, workspace_root=str(tmp_path))


def _policy(tmp_path, cfg=None, rules=None, store=None):
    return PermissionPolicy(
        PermissionConfig.from_dict(cfg or {}), rules, store,
        workspace_root=str(tmp_path), cwd=str(tmp_path),
    )


class TestPermissionStore:
    def test_session_level_not_consumed(self):
        store = PermissionStore()
        did = permission_decision_id("node server.js")
        store.approve(did, "node server.js")
        assert store.is_approved(did)
        assert store.is_approved(did)

    def test_other_command_still_blocked(self):
        store = PermissionStore()
        store.approve(permission_decision_id("node a.js"), "node a.js")
        assert not store.is_approved(permission_decision_id("node b.js"))

    def test_pending_single_slot(self):
        store = PermissionStore()
        store.set_pending({"decision_id": "a", "command": "x"})
        assert store.pending is not None
        store.clear_pending()
        assert store.pending is None

    def test_kind_classification(self):
        store = PermissionStore()
        store.approve("d1", "cmd", kind="command")
        store.approve("d2", "C:\\out", kind="path:write")
        store.approve("d3", "C:\\in", kind="path:read")
        store.approve("d4", "http://127.0.0.1", kind="browser:navigate")
        assert store.approved_commands() == ["cmd"]
        assert store.approved_navigations() == ["http://127.0.0.1"]
        assert {r["kind"] for r in store.approved_path_rules()} == {"path:write", "path:read"}


class TestRuleMatching:
    def test_prefix_token_boundary_and_piggyback(self, tmp_path):
        r = _rules(tmp_path, [_rule(value="git status")])
        assert r.evaluate_command("bash", "git status --short")[0] == "allow"
        assert r.evaluate_command("bash", "git statusx")[0] is None
        assert r.evaluate_command("bash", "git status && rm -rf /tmp/x")[0] is None

    def test_path_like_prefix_matches_subtree(self, tmp_path):
        r = _rules(tmp_path, [_rule(decision="deny", value="rm -rf /")])
        assert r.evaluate_command("bash", "rm -rf /tmp/x")[0] == "deny"

    def test_deny_overrides_allow(self, tmp_path):
        r = _rules(tmp_path, [
            _rule(decision="allow", value="git status"),
            _rule(decision="deny", match_type="exact", value="git status"),
        ])
        assert r.evaluate_command("bash", "git status")[0] == "deny"

    def test_ask(self, tmp_path):
        r = _rules(tmp_path, [_rule(decision="ask", value="git push")])
        decision, rule = r.evaluate_command("bash", "git push origin main")
        assert decision == "ask" and rule["decision"] == "ask"

    def test_argv_glob(self, tmp_path):
        r = _rules(tmp_path, [_rule(decision="allow", match_type="argv_glob",
                                    value=["git", "log", "*"])])
        assert r.evaluate_command("bash", "git log --oneline")[0] == "allow"
        assert r.evaluate_command("bash", "git push origin main")[0] is None

    def test_path_matching(self, tmp_path):
        r = _rules(tmp_path, [_rule(decision="allow", kind="path:write", value="E:\\out", tool="*")])
        assert r.evaluate_path("path:write", "E:\\out\\x.txt")[0] == "allow"
        assert r.evaluate_path("path:write", "E:\\out2\\x.txt")[0] is None
        assert r.evaluate_path("path:delete", "E:\\out\\x.txt")[0] is None

    def test_url_matching(self, tmp_path):
        r = _rules(tmp_path, [_rule(decision="allow", tool="browser",
                                    kind="browser:navigate", match_type="exact",
                                    value="http://127.0.0.1:8000")])
        assert r.evaluate_url("http://127.0.0.1:8000")[0] == "allow"
        assert r.evaluate_url("http://127.0.0.1:8000/x")[0] is None

    def test_crud_roundtrip(self, tmp_path):
        r = _rules(tmp_path, [])
        saved = r.add_rule(_rule(), "workspace")
        assert saved is not None
        assert r.evaluate_command("bash", "git status --short")[0] == "allow"
        assert r.update_rule(saved["id"], {"enabled": False}, "workspace")
        assert r.evaluate_command("bash", "git status --short")[0] is None
        assert r.delete_rule(saved["id"], "workspace")


class TestReadonlyWhitelist:
    def test_readonly_commands(self):
        assert is_readonly_command("bash", "git status --short")
        assert is_readonly_command("bash", "git config user.name")
        assert not is_readonly_command("bash", "git config user.name value")
        assert not is_readonly_command("bash", "touch x")
        assert is_safe_create_command("bash", "touch x")

    def test_builtin_deny(self, tmp_path):
        policy = _policy(tmp_path)
        assert policy.command_verdict("bash", "rm -rf /").action == "deny"
        assert policy.command_verdict("bash", "ls -la").action == "allow"


class TestPolicy:
    def test_default_modes(self, tmp_path):
        # 默认策略为默认允许；ai/ask 仍可显式配置
        assert _policy(tmp_path).command_verdict("bash", "node server.js").action == "allow"
        assert _policy(tmp_path, {"default_mode": "ai"}).command_verdict(
            "bash", "node server.js").action == "ai"
        assert _policy(tmp_path, {"default_mode": "ask"}).command_verdict(
            "bash", "node server.js").action == "ask"
        assert _policy(tmp_path, {"default_mode": "allow"}).command_verdict(
            "bash", "node server.js").action == "allow"

    def test_blacklist_beats_allow(self, tmp_path):
        r = _rules(tmp_path, [
            _rule(decision="allow", value="rm -rf /"),
            _rule(decision="deny", value="rm -rf /"),
        ])
        assert _policy(tmp_path, {"default_mode": "allow"}, r).command_verdict(
            "bash", "rm -rf /tmp/x").action == "deny"

    def test_outside_read_modes(self, tmp_path):
        outside = tmp_path.parent / f"outside-{tmp_path.name}" / "x.txt"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("x", encoding="utf-8")
        cfg = {"outside": {"read": "ai"}}
        assert _policy(tmp_path, cfg).path_verdict("path:read", str(outside)).action == "ai"
        assert _policy(tmp_path, {"outside": {"read": "deny"}}).path_verdict(
            "path:read", str(outside)).action == "deny"
        assert _policy(tmp_path, {"outside": {"read": "ask"}}).path_verdict(
            "path:read", str(outside)).action == "ask"

    def test_path_inside_and_outside_write(self, tmp_path):
        inside = str(tmp_path / "in.txt")
        outside = str(tmp_path.parent / f"outside-{tmp_path.name}" / "o.txt")
        policy = _policy(tmp_path)
        assert policy.path_verdict("path:write", inside).action == "allow"
        assert policy.path_verdict("path:write", outside).action == "ask"

    def test_store_approved_path(self, tmp_path):
        outside = str(tmp_path.parent / f"outside-{tmp_path.name}" / "o.txt")
        store = PermissionStore()
        policy = _policy(tmp_path, store=store)
        did = policy.path_decision_id("write", outside)
        store.approve(did, outside, kind="path:write")
        assert policy.path_verdict("path:write", outside, decision_id=did).action == "allow"


class TestToolIntegration:
    def test_bash_no_store_fail_closed(self, tmp_path):
        tool = BashTool(cwd=str(tmp_path))
        assert tool.execute(command="rm -rf /", timeout=5).error

    def test_bash_with_store_placeholder(self, tmp_path):
        store = PermissionStore()
        policy = _policy(tmp_path, {"default_mode": "ai"}, store=store)
        tool = BashTool(cwd=str(tmp_path), permission_store=store, permission_policy=policy)
        result = tool.execute(command="node server.js", timeout=5)
        assert result.completed is False
        meta = result.meta[META_KEY]
        assert meta["verdict"] == "ai"
        assert meta["command"] == "node server.js"

    def test_bash_allow_rule_runs(self, tmp_path, monkeypatch):
        rules = _rules(tmp_path, [_rule(value="npm run")])
        tool = BashTool(cwd=str(tmp_path), permission_rules=rules)
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout: ToolResult("ran ok"))
        result = tool.execute(command="npm run build", timeout=5)
        assert result.output == "ran ok"

    def test_read_outside_default_allow(self, tmp_path):
        outside = tmp_path.parent / f"outside-{tmp_path.name}" / "secret.txt"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("hello", encoding="utf-8")
        result = ReadTool(cwd=str(tmp_path)).execute(file_path=str(outside))
        assert result.error is False and "hello" in result.output

    def test_read_outside_ask_placeholder(self, tmp_path):
        outside = tmp_path.parent / f"outside-{tmp_path.name}" / "secret.txt"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("hello", encoding="utf-8")
        store = PermissionStore()
        policy = _policy(tmp_path, {"outside": {"read": "ask"}}, store=store)
        tool = ReadTool(cwd=str(tmp_path), permission_policy=policy,
                        permission_store=store)
        result = tool.execute(file_path=str(outside))
        assert result.completed is False
        assert result.meta[META_KEY]["kind"] == "path:read"

    def test_browser_no_store_fail_closed(self):
        from unittest.mock import MagicMock, patch
        svc = MagicMock()
        svc.is_running.return_value = True
        with patch("core.browser_service.get_service", return_value=svc):
            result = BrowserTool().execute(action="navigate", url="http://127.0.0.1:8000/")
        assert result.error
        svc.navigate.assert_not_called()

    def test_browser_with_store_placeholder(self):
        from unittest.mock import MagicMock, patch
        svc = MagicMock()
        svc.is_running.return_value = True
        store = PermissionStore()
        with patch("core.browser_service.get_service", return_value=svc):
            result = BrowserTool(permission_store=store).execute(
                action="navigate", url="http://127.0.0.1:8000/")
        assert result.completed is False
        assert result.meta[META_KEY]["kind"] == "browser:navigate"
        svc.navigate.assert_not_called()


class TestTexts:
    def test_labels_not_suffixes(self):
        assert APPROVE_LABEL == "允许本次会话"
        assert REMEMBER_LABEL == "一直允许（此类操作）"
        assert REJECT_LABEL == "拒绝"
        assert not APPROVE_LABEL.endswith(REMEMBER_LABEL)
        assert not REMEMBER_LABEL.endswith(APPROVE_LABEL)
        assert not REJECT_LABEL.endswith(APPROVE_LABEL)
        assert not REMEMBER_LABEL.endswith(REJECT_LABEL)

    def test_question_path_read(self):
        q = build_permission_question({
            "decision_id": "d1", "command": "C:\\secret.txt",
            "kind": "path:read", "reason": "路径在工作区外",
        })
        assert "工作区外文件读取" in q and "C:\\secret.txt" in q

    def test_placeholder_and_denied(self):
        assert "判定编号" in permission_placeholder_text(
            {"decision_id": "d1", "command": "x", "kind": "command"})
        assert "权限黑名单拒绝" in denied_rule_text(
            {"decision_id": "d1", "command": "x"}, {"note": "测试规则"})
        assert "权限判定拒绝" in judge_deny_text(
            {"decision_id": "d1", "command": "x"}, "摘要", "理由", "")
        assert "无询问通道" in no_channel_text({"decision_id": "d1", "command": "x"})
        assert "暂不可用" in judge_unavailable_text({"decision_id": "d1", "command": "x"}, "超时")
        assert "用户拒绝" in user_reject_hint("拒绝", {"summary": "删库"})


class TestRuleGeneration:
    def test_two_token_prefix(self):
        rule = build_granted_rule(kind="command", tool="bash", action="git commit -m x",
                                  granted_by="user")
        assert rule["match"] == {"type": "prefix", "value": "git commit"}

    def test_banned_prefix_rejected_for_allow(self):
        assert build_granted_rule(kind="command", tool="bash", action="python x.py",
                                  suggested={"type": "prefix", "value": "python x.py"},
                                  granted_by="user") is None

    def test_deny_allows_banned_prefix(self):
        rule = build_granted_rule(kind="command", tool="bash", action="python x.py",
                                  decision="deny",
                                  suggested={"type": "prefix", "value": "python"},
                                  granted_by="user")
        assert rule is not None and rule["decision"] == "deny"

    def test_destructive_head_degrades_exact(self):
        rule = build_granted_rule(kind="command", tool="bash", action="rm -rf ./build",
                                  suggested={"type": "prefix", "value": "rm -rf ./build"},
                                  granted_by="user")
        assert rule["match"]["type"] == "exact"

    def test_path_and_url(self, tmp_path):
        path_rule = build_granted_rule(kind="path:write", tool="*",
                                       action="E:\\out\\x.txt", granted_by="user")
        assert path_rule["match"]["type"] == "prefix"
        url_rule = build_granted_rule(kind="browser:navigate", tool="browser",
                                      action="http://127.0.0.1:8000/x", granted_by="user")
        assert "127.0.0.1:8000" in url_rule["match"]["value"]

    def test_implied_path_rules(self):
        rules = implied_path_rules(
            [{"op": "write", "raw": "E:\\out\\x", "resolved": "E:\\out\\x",
              "inside_workspace": False, "decision_id": "t1"}],
            "judge", "d1")
        assert rules and rules[0]["decision"] == "allow"


class TestLegacyApprovalFile:
    def test_rule_without_decision_defaults_allow(self, tmp_path):
        path = tmp_path / "approval_rules.json"
        path.write_text(json.dumps({
            "version": 1,
            "rules": [{
                "id": "legacy", "enabled": True, "tool": "bash", "kind": "command",
                "match": {"type": "prefix", "value": "git status"},
            }],
            "constraints": [],
        }, ensure_ascii=False), encoding="utf-8")
        rules = PermissionRules(path, workspace_root=str(tmp_path))
        assert rules.evaluate_command("bash", "git status --short")[0] == "allow"


class TestAuditFileName:
    def test_writes_approval_audit_jsonl(self, tmp_path):
        from core.tools.permission import append_permission_audit
        append_permission_audit(tmp_path, "asked", {"decision_id": "d1"})
        assert (tmp_path / "approval_audit.jsonl").exists()
