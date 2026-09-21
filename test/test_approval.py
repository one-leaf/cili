# -*- coding: utf-8 -*-
"""高风险命令会话级审批测试。

覆盖：
1. 三档分类：破坏性操作 → ask（可询问）；跨工具隔离/eval → deny（硬拒绝）
2. decision_id 确定性
3. ApprovalStore 会话级：批准不消费，同命令持续放行，不同命令仍拦截
4. 工具执行路径：未批准返回占位符（completed=False + meta.approval_required）；已批准真正执行
5. 子代理降级：approval_required 结果 → is_error=True，不挂起
6. 提示词下放：已批准命令列表出现在子代理任务消息
7. ask_user 选项上限扩展为 6
"""

from core.tools.approval import (
    APPROVE_LABEL,
    META_KEY,
    MODE_ASK,
    MODE_DENY,
    REJECT_LABEL,
    REMEMBER_LABEL,
    ApprovalStore,
    approval_decision_id,
    build_approved_commands_section,
)
from core.tools.bash import BashTool
from core.tools.pwsh import PwshTool
from core.tools.base import ToolResult


def _check_bash(cmd: str):
    return BashTool._check_deny_patterns(cmd)


def _check_pwsh(cmd: str):
    return PwshTool._check_deny_patterns(cmd)


class TestDenyTiers:
    """三档分类：破坏性操作 ask / 架构性 deny。"""

    def test_bash_destructive_is_ask(self):
        for cmd in [
            "rm -rf /", "rm -rf *", "rm -rf ~",
            "format C:", "dd if=/dev/zero of=/dev/sda",
            "mkfs.ext4 /dev/sda1", "shutdown -h now", "reboot",
            ":(){ :|:& };:", "> /dev/sda",
        ]:
            mode, reason = _check_bash(cmd)
            assert mode == MODE_ASK, f"{cmd!r} should be ask, got {mode} ({reason})"

    def test_bash_architectural_is_deny(self):
        for cmd in ["eval 'x'", "pwsh -Command foo", "powershell -Command foo",
                    "py x.py", "cmd /c dir", "wsl ls /"]:
            mode, reason = _check_bash(cmd)
            assert mode == MODE_DENY, f"{cmd!r} should be deny, got {mode} ({reason})"

    def test_bash_python_invocation_is_ask(self):
        # python 调用降为 ask 档：可经用户批准成为例外，而非架构性硬拒
        for cmd in ["python x.py", "python3 x.py", "pythonw x.py", "python.exe x.py"]:
            mode, reason = _check_bash(cmd)
            assert mode == MODE_ASK, f"{cmd!r} should be ask, got {mode} ({reason})"

    def test_pwsh_destructive_is_ask(self):
        for cmd in [
            "Remove-Item -Recurse -Force C:\\x", "Format-Volume -DriveLetter C",
            "Clear-Disk -Number 0", "Initialize-Disk -Number 1", "diskpart /s wipe.txt",
            "Stop-Computer -Force", "Restart-Computer -Force",
            "format C:", "shutdown /s /t 0", "reboot",
        ]:
            mode, reason = _check_pwsh(cmd)
            assert mode == MODE_ASK, f"{cmd!r} should be ask, got {mode} ({reason})"

    def test_pwsh_architectural_is_deny(self):
        for cmd in ["iex 'x'", "Invoke-Expression 'x'", "py x.py",
                    "bash -c 'x'", "cmd /c dir", "wsl ls /", "pwsh -Command x"]:
            mode, reason = _check_pwsh(cmd)
            assert mode == MODE_DENY, f"{cmd!r} should be deny, got {mode} ({reason})"

    def test_pwsh_python_invocation_is_ask(self):
        for cmd in ["python x.py", "python3 x.py", "python.exe x.py"]:
            mode, reason = _check_pwsh(cmd)
            assert mode == MODE_ASK, f"{cmd!r} should be ask, got {mode} ({reason})"

    def test_benign_not_blocked(self):
        assert _check_bash("ls -la") is None
        assert _check_pwsh("Get-Process") is None


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
        did = approval_decision_id("rm -rf /tmp/x")
        store.approve(did, "rm -rf /tmp/x")
        assert store.is_approved(did)  # 第一次命中
        assert store.is_approved(did)  # 会话级：不消费，仍放行

    def test_other_command_still_blocked(self):
        store = ApprovalStore()
        store.approve(approval_decision_id("rm -rf /tmp/x"), "rm -rf /tmp/x")
        assert not store.is_approved(approval_decision_id("rm -rf /tmp/y"))

    def test_pending_single_slot(self):
        store = ApprovalStore()
        store.set_pending({"decision_id": "a", "command": "x", "reason": "r"})
        assert store.pending is not None
        store.clear_pending()
        assert store.pending is None

    def test_approved_commands_listing(self):
        store = ApprovalStore()
        store.approve(approval_decision_id("rm -rf /tmp/x"), "rm -rf /tmp/x")
        assert store.approved_commands() == ["rm -rf /tmp/x"]

    # ─── 持久化 ──────────────────────────────────────────────────

    def test_persist_writes_file(self, tmp_path):
        import json as _json
        store = ApprovalStore(rules_path=tmp_path / "approvals.json")
        did = approval_decision_id("rm -rf /tmp/x")
        store.approve(did, "rm -rf /tmp/x", persist=True, reason="rm -rf /")
        path = tmp_path / "approvals.json"
        assert path.exists()
        data = _json.loads(path.read_text(encoding="utf-8"))
        assert data["rules"][0]["decision_id"] == did
        assert data["rules"][0]["command"] == "rm -rf /tmp/x"
        assert data["rules"][0]["reason"] == "rm -rf /"
        assert "created_at" in data["rules"][0]

    def test_load_rules_from_file(self, tmp_path):
        store = ApprovalStore(rules_path=tmp_path / "approvals.json")
        store.approve(approval_decision_id("rm -rf /tmp/x"), "rm -rf /tmp/x", persist=True)
        # 新实例从文件回灌
        reloaded = ApprovalStore(rules_path=tmp_path / "approvals.json")
        assert reloaded.is_approved(approval_decision_id("rm -rf /tmp/x"))

    def test_persist_dedup_replaces(self, tmp_path):
        import json as _json
        path = tmp_path / "approvals.json"
        store = ApprovalStore(rules_path=path)
        did = approval_decision_id("rm -rf /tmp/x")
        store.approve(did, "rm -rf /tmp/x", persist=True, reason="old")
        store.approve(did, "rm -rf /tmp/y", persist=True, reason="new")
        data = _json.loads(path.read_text(encoding="utf-8"))
        assert len(data["rules"]) == 1
        assert data["rules"][0]["command"] == "rm -rf /tmp/y"
        assert data["rules"][0]["reason"] == "new"

    def test_no_path_does_not_write(self, tmp_path):
        store = ApprovalStore()  # rules_path=None → 纯内存，不落盘
        store.approve(approval_decision_id("rm -rf /tmp/x"), "rm -rf /tmp/x", persist=True)
        assert not (tmp_path / "approvals.json").exists()

    def test_corrupt_file_loads_default(self, tmp_path):
        path = tmp_path / "approvals.json"
        path.write_text("{not valid json", encoding="utf-8")
        store = ApprovalStore(rules_path=path)
        assert not store.is_approved("anything")


class TestExecuteApprovalFlow:
    def test_unapproved_returns_placeholder(self):
        store = ApprovalStore()
        tool = BashTool(cwd=".", approval_store=store)
        result = tool.execute(command="rm -rf /")
        assert result.completed is False
        assert not result.error
        assert result.meta[META_KEY]["command"] == "rm -rf /"
        assert "decision_id" in result.meta[META_KEY]

    def test_approved_runs(self, monkeypatch):
        store = ApprovalStore()
        did = approval_decision_id("rm -rf /tmp/x")
        store.approve(did, "rm -rf /tmp/x")
        tool = BashTool(cwd=".", approval_store=store)
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout: ToolResult("ran ok"))
        result = tool.execute(command="rm -rf /tmp/x", timeout=10)
        assert result.output == "ran ok"
        assert not result.error

    def test_no_store_hard_deny(self):
        tool = BashTool(cwd=".")  # approval_store=None → 硬拒绝，不询问
        result = tool.execute(command="rm -rf /tmp/x")
        assert result.error
        assert result.completed is not False

    def test_deny_tier_hard_deny_even_with_store(self):
        store = ApprovalStore()
        tool = BashTool(cwd=".", approval_store=store)
        result = tool.execute(command="py x.py")
        assert result.error

    def test_bash_python_ask_tier_placeholder(self):
        # python 调用降为 ask 档：有 store 时返回占位等待用户批准，而非硬拒
        store = ApprovalStore()
        tool = BashTool(cwd=".", approval_store=store)
        result = tool.execute(command="python x.py")
        assert result.completed is False
        assert result.meta[META_KEY]["command"] == "python x.py"

    def test_bash_python_ask_tier_hard_deny_without_store(self):
        # 无 store（如独立 worker）仍硬拒绝，不询问
        tool = BashTool(cwd=".")
        result = tool.execute(command="python x.py")
        assert result.error

    def test_pwsh_unapproved_returns_placeholder(self):
        store = ApprovalStore()
        tool = PwshTool(cwd=".", approval_store=store)
        result = tool.execute(command="Remove-Item -Recurse -Force C:\\x")
        assert result.completed is False
        assert result.meta[META_KEY]["command"] == "Remove-Item -Recurse -Force C:\\x"

    def test_pwsh_approved_runs(self, monkeypatch):
        store = ApprovalStore()
        did = approval_decision_id("Remove-Item -Recurse -Force C:\\x")
        store.approve(did, "Remove-Item -Recurse -Force C:\\x")
        tool = PwshTool(cwd=".", approval_store=store)
        monkeypatch.setattr(tool, "_run_pwsh", lambda command, timeout: ToolResult("done"))
        result = tool.execute(command="Remove-Item -Recurse -Force C:\\x", timeout=10)
        assert result.output == "done"
        assert not result.error


class TestAgentDowngrade:
    def test_downgrade_approval_result(self):
        from core.agent import Agent
        result = {
            "type": "tool_result",
            "is_error": False,
            "content": "等待批准...",
            "_meta": {"completed": False, META_KEY: {"decision_id": "x", "command": "c", "reason": "r"}},
        }
        Agent._downgrade_approval_result(result)
        assert result["is_error"] is True
        assert META_KEY not in result["_meta"]
        assert "completed" not in result["_meta"]

    def test_approved_commands_section_injected(self):
        store = ApprovalStore()
        store.approve(approval_decision_id("rm -rf /tmp/x"), "rm -rf /tmp/x")
        section = build_approved_commands_section(store)
        assert "rm -rf /tmp/x" in section
        assert "Pre-approved commands" in section

    def test_no_approved_no_section(self):
        assert build_approved_commands_section(ApprovalStore()) == ""
        assert build_approved_commands_section(None) == ""


class TestAskUserSchema:
    def test_options_up_to_six(self):
        from core.tools.ask_user import AskUserTool
        options = AskUserTool.parameters["properties"]["questions"]["items"]["properties"]["options"]
        assert options["maxItems"] == 6
        assert options["minItems"] == 2

    def test_approve_label_constant(self):
        assert APPROVE_LABEL == "允许本次会话"

    def test_remember_label_constant(self):
        assert REMEMBER_LABEL == "允许并记住"
        # endswith 匹配安全：两 label 互不为子串
        assert not APPROVE_LABEL.endswith(REMEMBER_LABEL)
        assert not REMEMBER_LABEL.endswith(APPROVE_LABEL)

    def test_approval_question_mentions_three_options(self):
        from core.tools.approval import build_approval_question
        q = build_approval_question({
            "decision_id": "abc",
            "command": "rm -rf /tmp/x",
            "reason": "destructive delete",
        })
        assert APPROVE_LABEL in q
        assert REMEMBER_LABEL in q
        assert REJECT_LABEL in q
        assert "rm -rf /tmp/x" in q


class TestPathKind:
    """kind 字段：路径规则与命令规则共存、互不串扰。"""

    def test_command_default_kind(self):
        store = ApprovalStore()
        store.approve(approval_decision_id("rm -rf /tmp/x"), "rm -rf /tmp/x")
        assert store.approved_commands() == ["rm -rf /tmp/x"]
        assert store.approved_path_rules() == []

    def test_path_write_rule(self, tmp_path):
        from core.security.path_policy import OP_WRITE, PathPolicy, PathTarget
        policy = PathPolicy(workspace_root=str(tmp_path))
        did = policy.decision_id(PathTarget(OP_WRITE, "C:\\out", resolved="C:\\out"))
        store = ApprovalStore()
        store.approve(did, "C:\\out", kind="path:write")
        assert store.is_approved(did)
        assert store.approved_path_rules() == [{"kind": "path:write", "command": "C:\\out"}]
        assert store.approved_commands() == []

    def test_write_does_not_unlock_delete(self, tmp_path):
        from core.security.path_policy import OP_DELETE, OP_WRITE, PathPolicy, PathTarget
        policy = PathPolicy(workspace_root=str(tmp_path))
        w = policy.decision_id(PathTarget(OP_WRITE, "C:\\x", resolved="C:\\x"))
        d = policy.decision_id(PathTarget(OP_DELETE, "C:\\x", resolved="C:\\x"))
        store = ApprovalStore()
        store.approve(w, "C:\\x", kind="path:write")
        assert store.is_approved(w)
        assert not store.is_approved(d)  # 批 write 不解锁同路径 delete

    def test_old_rule_without_kind_loads_as_command(self, tmp_path):
        import json as _json
        path = tmp_path / "approvals.json"
        path.write_text(_json.dumps({
            "rules": [{"decision_id": "abc123", "command": "rm -rf /tmp/old", "reason": ""}]
        }), encoding="utf-8")
        store = ApprovalStore(rules_path=path)
        assert store.approved_commands() == ["rm -rf /tmp/old"]
        assert store.approved_path_rules() == []

    def test_persist_writes_kind_field(self, tmp_path):
        import json as _json
        from core.security.path_policy import OP_WRITE, PathPolicy, PathTarget
        path = tmp_path / "approvals.json"
        policy = PathPolicy(workspace_root=str(tmp_path))
        did = policy.decision_id(PathTarget(OP_WRITE, "C:\\out", resolved="C:\\out"))
        store = ApprovalStore(rules_path=path)
        store.approve(did, "C:\\out", kind="path:write", persist=True)
        data = _json.loads(path.read_text(encoding="utf-8"))
        assert data["rules"][0]["kind"] == "path:write"

    def test_question_path_write_wording(self):
        from core.tools.approval import build_approval_question
        q = build_approval_question({
            "decision_id": "abc", "command": "C:\\out",
            "kind": "path:write", "reason": "写入目标不在工作区 内",
        })
        assert "工作区外" in q
        assert "操作：写入" in q
        assert APPROVE_LABEL in q and REMEMBER_LABEL in q and REJECT_LABEL in q

    def test_question_path_delete_wording(self):
        from core.tools.approval import build_approval_question
        q = build_approval_question({
            "decision_id": "abc", "command": "rm out",
            "kind": "path:delete", "reason": "删除目标不在工作区内",
        })
        assert "操作：删除" in q
        assert "操作：写入" not in q

    def test_question_default_command_wording(self):
        from core.tools.approval import build_approval_question
        q = build_approval_question({
            "decision_id": "abc", "command": "rm -rf /tmp/x", "reason": "r",
        })
        assert "高风险命令" in q
        assert "命令：`rm -rf /tmp/x`" in q

    def test_section_includes_path_rules(self):
        from core.security.path_policy import OP_WRITE, PathPolicy, PathTarget
        store = ApprovalStore()
        policy = PathPolicy(workspace_root="C:\\ws")
        did = policy.decision_id(PathTarget(OP_WRITE, "C:\\out", resolved="C:\\out"))
        store.approve(did, "C:\\out", kind="path:write")
        section = build_approved_commands_section(store)
        assert "Pre-approved commands" in section
        assert "write `C:\\out`" in section


class TestBrowserNavigateKind:
    """kind="browser:navigate"（SSRF 导航审批）：文案、存储归类、下放段落。"""

    URL = "http://127.0.0.1:8885/tcmp-war/"

    def test_question_wording(self):
        from core.tools.approval import build_approval_question
        q = build_approval_question({
            "decision_id": "abc",
            "command": self.URL,
            "kind": "browser:navigate",
            "reason": "127.0.0.1 不是公网地址（环回）",
        })
        assert "非公网地址导航" in q
        assert self.URL in q
        assert "拦截原因" in q
        assert APPROVE_LABEL in q and REMEMBER_LABEL in q and REJECT_LABEL in q

    def test_placeholder_wording(self):
        from core.tools.approval import approval_placeholder_text
        p = approval_placeholder_text({
            "decision_id": "abc", "command": self.URL,
            "kind": "browser:navigate", "reason": "r",
        })
        assert "SSRF" in p
        assert "原样重发" in p

    def test_store_classifies_navigation(self):
        store = ApprovalStore()
        store.approve(approval_decision_id(self.URL), self.URL, kind="browser:navigate")
        assert store.approved_navigations() == [self.URL]
        assert store.approved_commands() == []  # 导航不计入命令
        assert store.approved_path_rules() == []  # 也不落入路径规则

    def test_command_does_not_count_as_navigation(self):
        store = ApprovalStore()
        store.approve(approval_decision_id("rm -rf /tmp/x"), "rm -rf /tmp/x")
        assert store.approved_navigations() == []
        assert store.approved_commands() == ["rm -rf /tmp/x"]

    def test_section_includes_navigation(self):
        store = ApprovalStore()
        store.approve(approval_decision_id(self.URL), self.URL, kind="browser:navigate")
        section = build_approved_commands_section(store)
        assert "Pre-approved commands" in section
        assert f"navigate `{self.URL}`" in section

    def test_persist_roundtrip_kind(self, tmp_path):
        import json as _json
        path = tmp_path / "approvals.json"
        store = ApprovalStore(rules_path=path)
        store.approve(approval_decision_id(self.URL), self.URL, kind="browser:navigate", persist=True)
        data = _json.loads(path.read_text(encoding="utf-8"))
        assert data["rules"][0]["kind"] == "browser:navigate"
        reloaded = ApprovalStore(rules_path=path)
        assert reloaded.approved_navigations() == [self.URL]

    def test_browser_tool_placeholder_and_gate(self):
        """浏览器工具 navigate 到非公网地址：未批准返回占位，已批准 skip_ssrf 放行。"""
        from unittest.mock import MagicMock, patch
        from core.tools.approval import META_KEY
        from core.tools.browser import BrowserTool

        svc = MagicMock()
        svc.is_running.return_value = True

        # 无 store → 占位，不调用 service
        with patch("core.browser_service.get_service", return_value=svc):
            result = BrowserTool().execute(action="navigate", url=self.URL)
        assert result.completed is False
        assert not result.error
        assert result.meta[META_KEY]["kind"] == "browser:navigate"
        assert result.meta[META_KEY]["command"] == self.URL
        svc.navigate.assert_not_called()

        # 有 store 但未批准 → 仍占位
        store = ApprovalStore()
        with patch("core.browser_service.get_service", return_value=svc):
            result = BrowserTool(approval_store=store).execute(action="navigate", url=self.URL)
        assert result.completed is False
        svc.navigate.assert_not_called()

        # 已批准 → 真正导航（skip_ssrf=True）
        store.approve(approval_decision_id(self.URL), self.URL, kind="browser:navigate")
        with patch("core.browser_service.get_service", return_value=svc):
            BrowserTool(approval_store=store).execute(action="navigate", url=self.URL)
        svc.navigate.assert_called_once_with(self.URL, tab_index=None, skip_ssrf=True)
