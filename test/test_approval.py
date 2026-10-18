# -*- coding: utf-8 -*-
"""审批流程测试（设计 workspace/approval-flow-design.md）。

覆盖：
1. 决策 ID 确定性；ApprovalStore 会话级语义（不消费 / kind 分类 / 无落盘）
2. 工具执行管道：规则命中执行 / 只读快道 / 未命中占位（completed=False + 证据）/
   无 store fail-closed / 防搭车
3. 卡片文案（含义/风险/理由/三选项）、label 互不为后缀、四分拒绝文本
4. 子代理降级安全网、提示词下放段落（会话批准 + 工作区规则）
5. 浏览器导航审批（规则命中放行 / 无通道 fail-closed）
"""

import json

from core.security.approval_rules import ApprovalRules
from core.tools.approval import (
    APPROVE_LABEL,
    META_KEY,
    REJECT_LABEL,
    REMEMBER_LABEL,
    ApprovalStore,
    approval_decision_id,
    build_approval_question,
    build_approved_commands_section,
    judge_deny_text,
    judge_unavailable_text,
    no_channel_text,
    user_reject_hint,
)
from core.tools.base import ToolResult
from core.tools.bash import BashTool
from core.tools.pwsh import PwshTool


def _rules(tmp_path, rules=None):
    path = tmp_path / "approval_rules.json"
    if rules is not None:
        path.write_text(json.dumps({"version": 1, "rules": rules, "constraints": []},
                                   ensure_ascii=False), encoding="utf-8")
    return ApprovalRules(path, workspace_root=str(tmp_path))


class TestDecisionId:
    def test_deterministic(self):
        assert approval_decision_id("rm -rf /tmp/x") == approval_decision_id("rm -rf /tmp/x")

    def test_whitespace_collapsed(self):
        assert approval_decision_id("rm  -rf  /tmp/x") == approval_decision_id("rm -rf /tmp/x")

    def test_different_command_different_id(self):
        assert approval_decision_id("rm -rf /tmp/x") != approval_decision_id("rm -rf /tmp/y")


class TestApprovalStore:
    def test_session_level_not_consumed(self):
        store = ApprovalStore()
        did = approval_decision_id("node server.js")
        store.approve(did, "node server.js")
        assert store.is_approved(did)  # 第一次命中
        assert store.is_approved(did)  # 会话级：不消费，仍放行

    def test_other_command_still_blocked(self):
        store = ApprovalStore()
        store.approve(approval_decision_id("node a.js"), "node a.js")
        assert not store.is_approved(approval_decision_id("node b.js"))

    def test_pending_single_slot(self):
        store = ApprovalStore()
        store.set_pending({"decision_id": "a", "command": "x", "reason": "r"})
        assert store.pending is not None
        store.clear_pending()
        assert store.pending is None

    def test_kind_classification(self):
        store = ApprovalStore()
        store.approve("d1", "cmd", kind="command")
        store.approve("d2", "C:\\out", kind="path:write")
        store.approve("d3", "http://127.0.0.1", kind="browser:navigate")
        assert store.approved_commands() == ["cmd"]
        assert store.approved_path_rules() == [{"kind": "path:write", "command": "C:\\out"}]
        assert store.approved_navigations() == ["http://127.0.0.1"]

    def test_no_persistence(self, tmp_path):
        # 持久化已移交 ApprovalRules；store 纯内存
        store = ApprovalStore()
        store.approve(approval_decision_id("x"), "x")
        assert not (tmp_path / "approvals.json").exists()
        assert not (tmp_path / "approval_rules.json").exists()


class TestCommandFlow:
    """bash/pwsh 执行管道：规则命中 → 只读快道 → 占位 → fail-closed。"""

    def test_readonly_runs_without_store(self, tmp_path, monkeypatch):
        tool = BashTool(cwd=str(tmp_path))
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout: ToolResult("ran ok"))
        result = tool.execute(command="echo hello", timeout=10)
        assert result.output == "ran ok"

    def test_findings_without_store_hard_deny(self, tmp_path):
        tool = BashTool(cwd=str(tmp_path))  # 无 store → fail-closed
        result = tool.execute(command="rm -rf /")
        assert result.error
        assert result.completed is not False

    def test_python_invocation_without_store_hard_deny(self, tmp_path):
        tool = BashTool(cwd=str(tmp_path))
        result = tool.execute(command="python x.py")
        assert result.error

    def test_benign_non_readonly_runs_without_store(self, tmp_path, monkeypatch):
        # 无 store 的独立/测试场景：无证据的良性命令保持旧行为直接执行
        tool = BashTool(cwd=str(tmp_path))
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout: ToolResult("ran ok"))
        result = tool.execute(command="node -e 'console.log(1)'", timeout=10)
        assert result.output == "ran ok"

    def test_unmatched_returns_placeholder_with_evidence(self, tmp_path):
        store = ApprovalStore()
        tool = BashTool(cwd=str(tmp_path), approval_store=store)
        result = tool.execute(command="node server.js", timeout=10)
        assert result.completed is False
        assert not result.error
        meta = result.meta[META_KEY]
        assert meta["command"] == "node server.js"
        assert meta["kind"] == "command"
        assert meta["tool"] == "bash"
        assert "decision_id" in meta

    def test_findings_pending_with_store(self, tmp_path):
        store = ApprovalStore()
        tool = BashTool(cwd=str(tmp_path), approval_store=store)
        result = tool.execute(command="python x.py", timeout=10)
        assert result.completed is False
        findings = result.meta[META_KEY]["findings"]
        assert any(f["id"] == "python-tool-routing" for f in findings)

    def test_session_approved_runs(self, tmp_path, monkeypatch):
        store = ApprovalStore()
        did = approval_decision_id("node server.js")
        store.approve(did, "node server.js")
        tool = BashTool(cwd=str(tmp_path), approval_store=store)
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout: ToolResult("ran ok"))
        result = tool.execute(command="node server.js", timeout=10)
        assert result.output == "ran ok"

    def test_rule_hit_runs(self, tmp_path, monkeypatch):
        rules = _rules(tmp_path, rules=[{
            "id": "r1", "enabled": True, "source": "granted", "granted_by": "user",
            "tool": "bash", "kind": "command", "match": {"type": "prefix", "value": "node server"},
            "display": "以 node server 开头",
        }])
        tool = BashTool(cwd=str(tmp_path), approval_rules=rules)
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout: ToolResult("ran ok"))
        result = tool.execute(command="node server --port 1", timeout=10)
        assert result.output == "ran ok"

    def test_rule_hit_does_not_cover_piggyback(self, tmp_path):
        rules = _rules(tmp_path, rules=[{
            "id": "r1", "enabled": True, "source": "granted", "granted_by": "user",
            "tool": "bash", "kind": "command", "match": {"type": "prefix", "value": "git status"},
            "display": "以 git status 开头",
        }])
        store = ApprovalStore()
        tool = BashTool(cwd=str(tmp_path), approval_store=store, approval_rules=rules)
        result = tool.execute(command="git status && rm -rf /tmp/x", timeout=10)
        # 第二段未命中 → 不放行，进入判定占位（防搭车）
        assert result.completed is False

    def test_path_pending_carries_evidence(self, tmp_path):
        store = ApprovalStore()
        tool = BashTool(cwd=str(tmp_path), approval_store=store)
        result = tool.execute(command="cp a.txt /e/outside/x.txt", timeout=10)
        assert result.completed is False
        meta = result.meta[META_KEY]
        # 统一占位：命令级卡片一次覆盖命令与越界目标（避免路径/命令双卡）
        assert meta["kind"] == "command"
        assert meta["command"] == "cp a.txt /e/outside/x.txt"
        assert meta["targets"], "证据应包含全部目标"
        outside = [t for t in meta["targets"] if not t["inside_workspace"]]
        assert outside and all(t["decision_id"] for t in outside)

    def test_pwsh_unmatched_returns_placeholder(self, tmp_path):
        store = ApprovalStore()
        tool = PwshTool(cwd=str(tmp_path), approval_store=store)
        result = tool.execute(command="Remove-Item -Recurse -Force C:\\x", timeout=10)
        assert result.completed is False
        meta = result.meta[META_KEY]
        # 证据先于路径门：命令级占位携带完整命令与 system-destructive 证据
        assert meta["kind"] == "command"
        assert meta["command"] == "Remove-Item -Recurse -Force C:\\x"
        assert any(f["id"] == "system-destructive" for f in meta["findings"])

    def test_pwsh_session_approved_runs(self, tmp_path, monkeypatch):
        store = ApprovalStore()
        cmd = "Remove-Item -Recurse -Force C:\\x"
        store.approve(approval_decision_id(cmd), cmd)
        tool = PwshTool(cwd=str(tmp_path), approval_store=store)
        monkeypatch.setattr(tool, "_run_pwsh", lambda command, timeout: ToolResult("done"))
        result = tool.execute(command=cmd, timeout=10)
        assert result.output == "done"


class TestCardTexts:
    def test_labels(self):
        assert APPROVE_LABEL == "允许本次会话"
        assert REMEMBER_LABEL == "一直允许（此类命令）"
        assert REJECT_LABEL == "拒绝"
        # endswith 匹配安全：互不为后缀
        assert not APPROVE_LABEL.endswith(REMEMBER_LABEL)
        assert not REMEMBER_LABEL.endswith(APPROVE_LABEL)
        assert not REJECT_LABEL.endswith(APPROVE_LABEL)
        assert not REMEMBER_LABEL.endswith(REJECT_LABEL)

    def test_question_contains_summary_risk_reason(self):
        q = build_approval_question({
            "decision_id": "abc", "command": "rm -rf ./build && make",
            "kind": "command", "summary": "删除构建目录并重新编译",
            "risk": "medium", "reason": "目标在工作区内",
        })
        assert "rm -rf ./build && make" in q
        assert "删除构建目录并重新编译" in q
        assert "风险：中" in q
        assert "目标在工作区内" in q
        assert APPROVE_LABEL in q and REMEMBER_LABEL in q and REJECT_LABEL in q

    def test_question_without_judge_fields(self):
        q = build_approval_question({"decision_id": "abc", "command": "x", "reason": "r"})
        assert "判定编号：abc" in q
        assert "含义：" not in q

    def test_placeholder_text(self):
        from core.tools.approval import approval_placeholder_text
        text = approval_placeholder_text({
            "decision_id": "abc", "command": "x", "reason": "r", "kind": "command",
        })
        assert "判定编号：abc" in text
        assert "worker/lite" in text


class TestRejectionTexts:
    """§3.6 四分拒绝文本。"""

    def test_judge_deny(self):
        t = judge_deny_text({"decision_id": "d1", "command": "x"}, "摘要", "理由", "改用 A")
        assert "安全判定拒绝" in t and "理由" in t and "改用 A" in t and "d1" in t

    def test_no_channel(self):
        t = no_channel_text({"decision_id": "d1", "command": "x"}, "摘要")
        assert "无询问通道" in t and "父代理" in t and "摘要" in t

    def test_unavailable(self):
        t = judge_unavailable_text({"decision_id": "d1", "command": "x"}, "超时")
        assert "暂不可用" in t and "超时" in t

    def test_user_reject_hint(self):
        t = user_reject_hint("拒绝", {"summary": "删库"})
        assert "用户拒绝" in t and "删库" in t and "不要原样重发" in t


class TestAgentDowngrade:
    def test_downgrade_approval_result(self):
        from core.session_runner import SessionRunner
        result = {
            "type": "tool_result",
            "is_error": False,
            "content": "等待批准...",
            "_meta": {"completed": False, META_KEY: {"decision_id": "x", "command": "c", "reason": "r"}},
        }
        SessionRunner._downgrade_approval_result(result)
        assert result["is_error"] is True
        assert META_KEY not in result["_meta"]
        assert "completed" not in result["_meta"]


class TestApprovedSection:
    def test_session_and_rules(self, tmp_path):
        store = ApprovalStore()
        store.approve(approval_decision_id("rm -rf /tmp/x"), "rm -rf /tmp/x")
        rules = _rules(tmp_path, rules=[{
            "id": "r1", "enabled": True, "source": "granted", "granted_by": "user",
            "tool": "bash", "kind": "command", "match": {"type": "prefix", "value": "git status"},
            "display": "以 git status 开头的命令",
        }])
        section = build_approved_commands_section(store, rules)
        assert "Session-approved actions" in section
        assert "rm -rf /tmp/x" in section
        assert "Workspace-approved rules" in section
        assert "以 git status 开头的命令" in section

    def test_empty_when_none(self, tmp_path):
        assert build_approved_commands_section(ApprovalStore(), _rules(tmp_path)) == ""
        assert build_approved_commands_section(None, None) == ""


class TestBrowserNavigateKind:
    """浏览器导航：硬拒绝保留在 browser_service；非公网走规则/判定管道。"""

    URL = "http://127.0.0.1:8885/tcmp-war/"

    def test_question_wording(self):
        q = build_approval_question({
            "decision_id": "abc", "command": self.URL,
            "kind": "browser:navigate", "reason": "127.0.0.1 不是公网地址（环回）",
        })
        assert "非公网地址导航" in q
        assert self.URL in q
        assert APPROVE_LABEL in q and REMEMBER_LABEL in q and REJECT_LABEL in q

    def test_no_store_fail_closed(self):
        from unittest.mock import MagicMock, patch

        from core.tools.browser import BrowserTool

        svc = MagicMock()
        svc.is_running.return_value = True
        with patch("core.browser_service.get_service", return_value=svc):
            result = BrowserTool().execute(action="navigate", url=self.URL)
        assert result.error  # 无审批通道 → fail-closed
        svc.navigate.assert_not_called()

    def test_unapproved_placeholder(self):
        from unittest.mock import MagicMock, patch

        from core.tools.browser import BrowserTool

        svc = MagicMock()
        svc.is_running.return_value = True
        store = ApprovalStore()
        with patch("core.browser_service.get_service", return_value=svc):
            result = BrowserTool(approval_store=store).execute(action="navigate", url=self.URL)
        assert result.completed is False
        svc.navigate.assert_not_called()

    def test_rule_hit_navigates(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from core.tools.browser import BrowserTool

        rules = _rules(tmp_path, rules=[{
            "id": "r1", "enabled": True, "source": "granted", "granted_by": "user",
            "tool": "browser", "kind": "browser:navigate",
            "match": {"type": "exact", "value": self.URL}, "display": "精确匹配",
        }])
        svc = MagicMock()
        svc.is_running.return_value = True
        with patch("core.browser_service.get_service", return_value=svc):
            BrowserTool(approval_rules=rules).execute(action="navigate", url=self.URL)
        svc.navigate.assert_called_once_with(self.URL, tab_index=None, approved_non_public=True)


class TestAskUserSchema:
    def test_options_up_to_six(self):
        from core.tools.ask_user import AskUserTool
        options = AskUserTool.parameters["properties"]["questions"]["items"]["properties"]["options"]
        assert options["maxItems"] == 6
        assert options["minItems"] == 2
