# -*- coding: utf-8 -*-
"""bash/pwsh 写/删目标提取与路径权限门测试。

单元层：extract_shell_targets 的字面量/动态/重定向/cd 链/引号提取。
集成层：BashTool/PwshTool.execute 的区内放行、区外审批占位、已批放行、
无 store 拒绝、keyword 批准跳过路径门、working_dir 门。
"""

import os

from core.security.path_policy import OP_WRITE, PathPolicy, PathTarget
from core.security.shell_paths import extract_shell_targets
from core.tools.approval import ApprovalStore, META_KEY, approval_decision_id
from core.tools.base import ToolResult
from core.tools.bash import BashTool
from core.tools.pwsh import PwshTool


# ─── 单元：bash 提取 ─────────────────────────────────────────────


class TestExtractBash:
    def _t(self, command, cwd, initial_dir=None):
        return extract_shell_targets(command, "bash", cwd=cwd, initial_dir=initial_dir or cwd)

    def test_rm_literal(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("rm -rf out/x.txt", ws)
        assert len(targets) == 1
        assert targets[0].op == "delete"
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "out", "x.txt"))

    def test_mkdir_inside(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("mkdir -p build", ws)
        assert len(targets) == 1
        assert targets[0].op == "write"
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "build"))

    def test_cp_last_is_write(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("cp src.txt dest/out.txt", ws)
        assert [t.op for t in targets] == ["write"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "dest", "out.txt"))

    def test_mv_mixed(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("mv a.txt b.txt", ws)
        assert [t.op for t in targets] == ["delete", "write"]

    def test_redirect_write(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("echo hi > out/f.txt", ws)
        assert [t.op for t in targets] == ["write"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "out", "f.txt"))

    def test_redirect_dev_null_ignored(self, tmp_path):
        assert self._t("echo hi > /dev/null", str(tmp_path)) == []

    def test_dynamic_target(self, tmp_path):
        targets = self._t('rm "$DIR"/x', str(tmp_path))
        assert len(targets) == 1
        assert targets[0].dynamic is True
        assert targets[0].resolved is None

    def test_wildcard_dynamic(self, tmp_path):
        targets = self._t("rm -rf build/*", str(tmp_path))
        assert len(targets) == 1
        assert targets[0].dynamic is True

    def test_cd_outside_then_relative(self, tmp_path):
        ws = str(tmp_path)
        outside = os.path.realpath(os.path.join(str(tmp_path.parent), "out"))
        targets = self._t(f"cd {outside} && rm x.txt", ws)
        assert len(targets) == 1
        assert targets[0].resolved == os.path.realpath(os.path.join(outside, "x.txt"))

    def test_cd_inside_then_write(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("cd sub && touch new.txt", ws)
        assert len(targets) == 1
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "sub", "new.txt"))

    def test_quoted_path_with_space(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('rm "my dir/a file.txt"', ws)
        assert len(targets) == 1
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "my dir", "a file.txt"))

    def test_read_only_command_no_targets(self, tmp_path):
        ws = str(tmp_path)
        assert self._t("cat x.txt", ws) == []
        assert self._t("ls -la", ws) == []

    def test_comment_ignored(self, tmp_path):
        assert self._t("echo done # rm /outside/y", str(tmp_path)) == []

    def test_gitbash_drive_form(self, tmp_path):
        targets = self._t("rm /e/tmp/x.txt", str(tmp_path))
        assert len(targets) == 1
        assert targets[0].resolved.lower().startswith("e:\\tmp\\x.txt")

    def test_dd_of_write(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("dd if=in.bin of=out.bin", ws)
        assert [t.op for t in targets] == ["write"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "out.bin"))

    def test_tee_writes(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("echo hi | tee out.log", ws)
        assert [t.op for t in targets] == ["write"]

    def test_cp_t_target_directory(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("cp -t dest a.txt b.txt", ws)
        assert [t.op for t in targets] == ["write"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "dest"))


# ─── 单元：pwsh 提取 ─────────────────────────────────────────────


class TestExtractPwsh:
    def _t(self, command, cwd, initial_dir=None):
        return extract_shell_targets(command, "pwsh", cwd=cwd, initial_dir=initial_dir or cwd)

    def test_remove_item_path(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("Remove-Item -Path out\\x.txt -Recurse -Force", ws)
        assert [t.op for t in targets] == ["delete"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "out", "x.txt"))

    def test_new_item(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("New-Item -ItemType Directory -Path build", ws)
        assert [t.op for t in targets] == ["write"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "build"))

    def test_set_content(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("Set-Content -Path out.txt -Value 'hi'", ws)
        assert [t.op for t in targets] == ["write"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "out.txt"))

    def test_copy_item_destination(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("Copy-Item -Path a.txt -Destination b.txt", ws)
        assert [t.op for t in targets] == ["write"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "b.txt"))

    def test_out_file(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t("Get-Process | Out-File -FilePath out.txt", ws)
        assert [t.op for t in targets] == ["write"]

    def test_windows_backslash_preserved(self, tmp_path):
        targets = self._t("Remove-Item C:\\Users\\x\\f.txt", str(tmp_path))
        assert len(targets) == 1
        assert targets[0].resolved.lower() == os.path.realpath("C:\\Users\\x\\f.txt").lower()

    def test_cd_then_remove(self, tmp_path):
        ws = str(tmp_path)
        outside = os.path.realpath(os.path.join(str(tmp_path.parent), "out"))
        targets = self._t(f"Set-Location {outside}; Remove-Item x.txt", ws)
        assert len(targets) == 1
        assert targets[0].resolved == os.path.realpath(os.path.join(outside, "x.txt"))


# ─── 集成：BashTool.execute 路径门 ───────────────────────────────


class TestBashExecuteGate:
    def _tool(self, cwd, store=None):
        return BashTool(cwd=cwd, approval_store=store)

    def test_outside_write_placeholder(self, test_workspace, tmp_path):
        tool = self._tool(test_workspace, ApprovalStore())
        outside = tmp_path / "o.txt"
        result = tool.execute(command=f"touch {outside}", timeout=5)
        assert result.completed is False
        assert not result.error
        assert result.meta[META_KEY]["kind"] == "path:write"
        assert result.meta[META_KEY]["command"] == str(outside)

    def test_outside_delete_placeholder(self, test_workspace, tmp_path):
        tool = self._tool(test_workspace, ApprovalStore())
        outside = tmp_path / "o.txt"
        result = tool.execute(command=f"rm {outside}", timeout=5)
        assert result.completed is False
        assert result.meta[META_KEY]["kind"] == "path:delete"

    def test_inside_write_runs(self, test_workspace, monkeypatch):
        tool = self._tool(test_workspace, ApprovalStore())
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout: ToolResult("ran"))
        result = tool.execute(command="touch inside.txt", timeout=5)
        assert result.output == "ran"
        assert not result.error

    def test_approved_path_allows(self, test_workspace, tmp_path, monkeypatch):
        store = ApprovalStore()
        tool = self._tool(test_workspace, store)
        outside = tmp_path / "o.txt"
        policy = PathPolicy(workspace_root=test_workspace, cwd=test_workspace, approval_store=store)
        did = policy.decision_id(PathTarget(OP_WRITE, str(outside), resolved=policy.resolve(str(outside))))
        store.approve(did, str(outside), kind="path:write")
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout: ToolResult("ran"))
        result = tool.execute(command=f"touch {outside}", timeout=5)
        assert result.output == "ran"

    def test_no_store_outside_deny(self, test_workspace, tmp_path):
        tool = self._tool(test_workspace)
        outside = tmp_path / "o.txt"
        result = tool.execute(command=f"touch {outside}", timeout=5)
        assert result.error is True

    def test_whole_command_approved_skips_path_gate(self, test_workspace, monkeypatch):
        store = ApprovalStore()
        store.approve(approval_decision_id("rm -rf /tmp/x"), "rm -rf /tmp/x")
        tool = self._tool(test_workspace, store)
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout: ToolResult("ran"))
        result = tool.execute(command="rm -rf /tmp/x", timeout=5)
        assert result.output == "ran"
        assert not result.error

    def test_deny_keyword_first(self, test_workspace):
        tool = self._tool(test_workspace, ApprovalStore())
        result = tool.execute(command="rm -rf /", timeout=5)
        assert result.completed is False
        assert "rm -rf /" in result.meta[META_KEY]["command"]

    def test_working_dir_outside_gate(self, test_workspace, tmp_path):
        tool = self._tool(test_workspace, ApprovalStore())
        outside = tmp_path / "odir"
        outside.mkdir()
        result = tool.execute(command="ls", working_dir=str(outside), timeout=5)
        assert result.completed is False
        assert result.meta[META_KEY]["kind"] == "path:write"

    def test_working_dir_inside_runs(self, test_workspace, monkeypatch):
        tool = self._tool(test_workspace, ApprovalStore())
        os.makedirs(os.path.join(test_workspace, "sub"), exist_ok=True)
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout: ToolResult("ran"))
        result = tool.execute(command="ls", working_dir="sub", timeout=5)
        assert result.output == "ran"


class TestPwshExecuteGate:
    def test_outside_remove_placeholder(self, test_workspace, tmp_path):
        tool = PwshTool(cwd=test_workspace, approval_store=ApprovalStore())
        outside = tmp_path / "o.txt"
        result = tool.execute(command=f"Remove-Item {outside}", timeout=5)
        assert result.completed is False
        assert result.meta[META_KEY]["kind"] == "path:delete"

    def test_inside_write_runs(self, test_workspace, monkeypatch):
        tool = PwshTool(cwd=test_workspace, approval_store=ApprovalStore())
        monkeypatch.setattr(tool, "_run_pwsh", lambda command, timeout: ToolResult("ran"))
        result = tool.execute(command="New-Item -ItemType File out.txt", timeout=5)
        assert result.output == "ran"
        assert not result.error
