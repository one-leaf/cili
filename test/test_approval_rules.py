# -*- coding: utf-8 -*-
"""审批规则引擎测试（core/security/approval_rules.py）。

覆盖：段级匹配（防复合命令搭车）、复杂命令 exact-only、BANNED_PREFIXES、
DESTRUCTIVE_HEADS 降级、兜底推导、路径/导航匹配、去重保留首次来源、
旧 approvals.json 迁移、加载校验（防篡改）、mtime 热重载、只读快道、constraints 合并。
"""

import json

from core.security.approval_rules import (
    ApprovalRules,
    build_granted_rule,
    implied_path_rules,
    is_builtin_readonly,
)


def _rules(tmp_path, rules=None, constraints=None):
    """构造规则引擎实例；传 rules/constraints 时先写文件。"""
    path = tmp_path / "approval_rules.json"
    if rules is not None or constraints is not None:
        path.write_text(
            json.dumps({"version": 1, "rules": rules or [], "constraints": constraints or []},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    return ApprovalRules(path, workspace_root=str(tmp_path))


def _cmd_rule(match_type, value, tool="bash", kind="command", enabled=True):
    return {
        "id": f"r-{match_type}-{abs(hash((match_type, str(value)))) % 99999}",
        "enabled": enabled, "source": "granted", "granted_by": "user",
        "tool": tool, "kind": kind, "match": {"type": match_type, "value": value},
        "display": f"{match_type}: {value}",
    }


class TestBuildGrantedRule:
    """§3.5 管道：兜底推导 / 黑名单 / 破坏性动词头降级。"""

    def test_two_token_prefix_fallback(self):
        rule = build_granted_rule(kind="command", tool="bash", action="git commit -m x",
                                  granted_by="user", decision_id="d1")
        assert rule["match"] == {"type": "prefix", "value": "git commit"}
        assert rule["granted_by"] == "user"
        assert rule["source"] == "granted"

    def test_flag_second_token_degrades_exact(self):
        rule = build_granted_rule(kind="command", tool="bash", action="curl -s http://x",
                                  granted_by="user")
        assert rule["match"] == {"type": "exact", "value": "curl -s http://x"}

    def test_destructive_head_degrades_exact(self):
        rule = build_granted_rule(kind="command", tool="bash", action="rm -rf ./build",
                                  suggested={"type": "prefix", "value": "rm -rf ./build"},
                                  granted_by="judge")
        assert rule["match"]["type"] == "exact"
        assert rule["match"]["value"] == "rm -rf ./build"

    def test_banned_prefix_rejected(self):
        for value in ("python x.py", "bash -c 'x'", "sudo ls", "env FOO=1 x", "wsl ls"):
            rule = build_granted_rule(kind="command", tool="bash", action=value,
                                      suggested={"type": "prefix", "value": value},
                                      granted_by="judge")
            assert rule is None, value

    def test_exact_banned_head_allowed(self):
        # exact 不受限：精确命令在批准时可见（旧 approvals.json 里就有 python --version）
        rule = build_granted_rule(kind="command", tool="bash", action="python --version",
                                  granted_by="user")
        assert rule["match"] == {"type": "exact", "value": "python --version"}

    def test_path_kind_parent_prefix(self):
        rule = build_granted_rule(kind="path:write", tool="*", action="E:\\out\\x.txt",
                                  granted_by="user")
        assert rule["match"]["type"] == "prefix"
        assert rule["match"]["value"].lower().endswith("out")

    def test_navigate_host_prefix(self):
        rule = build_granted_rule(kind="browser:navigate", tool="browser",
                                  action="http://127.0.0.1:8899/page", granted_by="user")
        assert rule["match"]["type"] == "prefix"
        assert "127.0.0.1:8899" in rule["match"]["value"]

    def test_unknown_kind_rejected(self):
        assert build_granted_rule(kind="whatever", tool="*", action="x", granted_by="user") is None


class TestImpliedPathRules:
    def test_outside_target_generates_rule(self):
        rules = implied_path_rules(
            [{"op": "write", "raw": "E:\\out\\x.json", "resolved": "E:\\out\\x.json",
              "inside_workspace": False, "decision_id": "t1"}],
            granted_by="judge", decision_id="d1")
        assert len(rules) == 1
        assert rules[0]["kind"] == "path:write"
        assert rules[0]["match"]["type"] == "prefix"

    def test_inside_target_skipped(self):
        rules = implied_path_rules(
            [{"op": "write", "raw": "in.txt", "resolved": "E:\\ws\\in.txt",
              "inside_workspace": True, "decision_id": "t2"}],
            granted_by="judge", decision_id="d1")
        assert rules == []


class TestCommandMatching:
    """§3.2.3：整条 exact / 段级 prefix/glob / 复杂命令 exact-only / 防搭车。"""

    def test_exact_whole_command(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("exact", "python --version")])
        assert r.match_command("bash", "python --version") is not None
        assert r.match_command("bash", "python --version --help") is None

    def test_prefix_token_boundary(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("prefix", "git status")])
        assert r.match_command("bash", "git status --short") is not None
        assert r.match_command("bash", "git statusx") is None

    def test_prefix_not_cover_piggyback(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("prefix", "git status")])
        assert r.match_command("bash", "git status && rm -rf /tmp/x") is None

    def test_chain_all_segments_must_match(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("prefix", "git status"),
                                    _cmd_rule("exact", "make")])
        assert r.match_command("bash", "git status && make") is not None
        assert r.match_command("bash", "git status && make all") is None  # 第二段 exact 不中

    def test_substitution_exact_only(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("prefix", "echo")])
        assert r.match_command("bash", "echo $(rm -rf /tmp/x)") is None
        assert r.match_command("bash", "echo hi") is not None

    def test_substitution_exact_still_matches(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("exact", "echo $(date)")])
        assert r.match_command("bash", "echo $(date)") is not None

    def test_argv_glob(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("argv_glob", ["git", "log", "*"])])
        assert r.match_command("bash", "git log --oneline -5") is not None
        assert r.match_command("bash", "git push origin main") is None

    def test_tool_scoped(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("prefix", "Get-ChildItem", tool="pwsh")])
        assert r.match_command("pwsh", "Get-ChildItem -Name") is not None
        assert r.match_command("bash", "Get-ChildItem -Name") is None

    def test_disabled_rule_skipped(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("prefix", "git status", enabled=False)])
        assert r.match_command("bash", "git status") is None

    def test_whitespace_normalized(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("exact", "python --version")])
        assert r.match_command("bash", "python   --version") is not None


class TestPathAndUrlMatching:
    def test_path_prefix_boundary(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("prefix", "E:\\out", tool="*", kind="path:write")])
        assert r.match_path("path:write", "E:\\out\\sub\\x.txt") is not None
        assert r.match_path("path:write", "E:\\out") is not None
        assert r.match_path("path:write", "E:\\out2\\x.txt") is None
        assert r.match_path("path:delete", "E:\\out\\x.txt") is None  # kind 隔离

    def test_path_case_insensitive(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("prefix", "e:\\out", tool="*", kind="path:write")])
        assert r.match_path("path:write", "E:\\OUT\\x.txt") is not None

    def test_url_exact_and_prefix(self, tmp_path):
        r = _rules(tmp_path, rules=[
            _cmd_rule("exact", "http://127.0.0.1:8000", tool="browser", kind="browser:navigate"),
        ])
        assert r.match_url("http://127.0.0.1:8000") is not None
        assert r.match_url("http://127.0.0.1:8000/x") is None
        assert r.match_url("http://127.0.0.1:8000", tool="bash") is None  # tool 过滤


class TestStore:
    def test_dedupe_keeps_first_source(self, tmp_path):
        r = _rules(tmp_path)
        first = build_granted_rule(kind="command", tool="bash", action="git status",
                                   granted_by="user", decision_id="d1")
        r.add_granted(first)
        second = build_granted_rule(kind="command", tool="bash", action="git status",
                                    granted_by="judge", decision_id="d2")
        kept = r.add_granted(second)
        assert kept["granted_by"] == "user"
        assert len(r.all_rules()) == 1

    def test_persist_roundtrip(self, tmp_path):
        r = _rules(tmp_path)
        r.add_granted(build_granted_rule(kind="command", tool="bash", action="git status",
                                         granted_by="user"))
        reloaded = ApprovalRules(tmp_path / "approval_rules.json")
        assert reloaded.match_command("bash", "git status --short") is not None

    def test_no_path_memory_only(self, tmp_path):
        r = ApprovalRules(None)
        r.add_granted(build_granted_rule(kind="command", tool="bash", action="git status",
                                         granted_by="user"))
        assert r.match_command("bash", "git status") is not None
        assert not (tmp_path / "approval_rules.json").exists()


class TestValidation:
    def test_banned_prefix_in_file_skipped(self, tmp_path):
        r = _rules(tmp_path, rules=[_cmd_rule("prefix", "sudo rm")])
        assert r.match_command("bash", "sudo rm x") is None

    def test_corrupt_file_empty(self, tmp_path):
        path = tmp_path / "approval_rules.json"
        path.write_text("{not valid json", encoding="utf-8")
        r = ApprovalRules(path)
        assert r.match_command("bash", "anything") is None

    def test_hot_reload(self, tmp_path):
        path = tmp_path / "approval_rules.json"
        path.write_text(json.dumps({"version": 1, "rules": [], "constraints": []}), encoding="utf-8")
        r = ApprovalRules(path)
        assert r.match_command("bash", "git status") is None
        path.write_text(json.dumps(
            {"version": 1, "rules": [_cmd_rule("prefix", "git status")], "constraints": []}),
            encoding="utf-8")
        assert r.match_command("bash", "git status") is not None


class TestMigration:
    def test_legacy_approvals_migrated(self, tmp_path):
        legacy = tmp_path / "approvals.json"
        legacy.write_text(json.dumps({"rules": [
            {"decision_id": "abc123", "command": "python --version", "reason": "r1",
             "created_at": "2026-09-15 15:34:42"},
            {"decision_id": "def456", "command": "E:\\out", "reason": "r2", "kind": "path:write",
             "created_at": "2026-09-16 12:22:42"},
            {"decision_id": "ghi789", "command": "http://127.0.0.1:8000", "reason": "r3",
             "kind": "browser:navigate", "created_at": "2026-09-21 15:39:58"},
        ]}), encoding="utf-8")
        r = ApprovalRules(tmp_path / "approval_rules.json")
        # 旧文件保留 + .bak 生成
        assert legacy.exists()
        assert (tmp_path / "approvals.json.bak").exists()
        # exact 等效力
        assert r.match_command("bash", "python --version") is not None
        assert r.match_path("path:write", "E:\\out") is not None
        assert r.match_url("http://127.0.0.1:8000") is not None
        data = json.loads((tmp_path / "approval_rules.json").read_text(encoding="utf-8"))
        assert data["migrated_from"] == "approvals.json"
        assert len(data["rules"]) == 3
        assert data["rules"][0]["legacy_decision_id"] == "abc123"


class TestConstraints:
    def test_defaults_present(self, tmp_path):
        r = _rules(tmp_path)
        ids = {c["id"] for c in r.constraints()}
        assert "python-tool-routing" in ids
        assert "cross-tool-isolation" in ids

    def test_file_overrides_by_id(self, tmp_path):
        r = _rules(tmp_path, constraints=[
            {"id": "python-tool-routing", "severity": "warn", "text": "custom"},
        ])
        by_id = {c["id"]: c for c in r.constraints()}
        assert by_id["python-tool-routing"]["severity"] == "warn"
        assert by_id["python-tool-routing"]["text"] == "custom"


class TestBuiltinReadonly:
    def test_bash_readonly(self):
        assert is_builtin_readonly("bash", "ls -la")
        assert is_builtin_readonly("bash", "cat a.txt | grep foo")
        assert is_builtin_readonly("bash", "git status")
        assert is_builtin_readonly("bash", "git log --oneline -5")
        assert is_builtin_readonly("bash", "npm ls")
        assert is_builtin_readonly("bash", "seq 1 10000")
        assert is_builtin_readonly("bash", "echo hi && echo $((1+1))")

    def test_bash_not_readonly(self):
        assert not is_builtin_readonly("bash", "git push origin main")
        assert not is_builtin_readonly("bash", "find . -delete")
        assert not is_builtin_readonly("bash", "env FOO=1 cmd")
        assert not is_builtin_readonly("bash", "node server.js")
        assert not is_builtin_readonly("bash", "ls && rm -rf /tmp/x")
        assert not is_builtin_readonly("bash", "echo $(rm -rf x)")  # 替换不可信

    def test_pwsh_readonly(self):
        assert is_builtin_readonly("pwsh", "Get-ChildItem")
        assert is_builtin_readonly("pwsh", "Get-Process | Select-String x")

    def test_pwsh_not_readonly(self):
        assert not is_builtin_readonly("pwsh", "Remove-Item x")
        assert not is_builtin_readonly("pwsh", "Set-Content a.txt")
