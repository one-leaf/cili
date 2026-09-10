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
                    "python x.py", "py x.py", "cmd /c dir", "wsl ls /"]:
            mode, reason = _check_bash(cmd)
            assert mode == MODE_DENY, f"{cmd!r} should be deny, got {mode} ({reason})"

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
        for cmd in ["iex 'x'", "Invoke-Expression 'x'", "python x.py", "py x.py",
                    "bash -c 'x'", "cmd /c dir", "wsl ls /", "pwsh -Command x"]:
            mode, reason = _check_pwsh(cmd)
            assert mode == MODE_DENY, f"{cmd!r} should be deny, got {mode} ({reason})"

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
