# -*- coding: utf-8 -*-
"""python AST 写/删目标收集与路径权限门测试。

单元层：collect_python_targets 的 open 模式判定、os/shutil/Path 操作、
别名与拼接、变量符号表、动态目标、语法错误空列表。
集成层：PythonTool.execute 的区内放行、区外审批占位、已批放行、
无 store 拒绝、deny 先于路径门。
"""

import os

from core.security.path_policy import OP_WRITE, PathPolicy, PathTarget
from core.security.python_paths import collect_python_targets
from core.tools.approval import ApprovalStore, META_KEY
from core.tools.base import ToolResult
from core.tools.python_tool import PythonTool


# ─── 单元：AST 收集 ──────────────────────────────────────────────


class TestCollectPythonTargets:
    def _t(self, code, cwd):
        return collect_python_targets(code, cwd)

    def test_open_write(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('open("out.txt", "w")', ws)
        assert [t.op for t in targets] == ["write"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "out.txt"))

    def test_open_read_skipped(self, tmp_path):
        ws = str(tmp_path)
        assert self._t('open("out.txt", "r")', ws) == []
        assert self._t('open("out.txt")', ws) == []

    def test_open_append_write(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('open("log.txt", "a")', ws)
        assert [t.op for t in targets] == ["write"]

    def test_os_remove(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('os.remove("a/x.txt")', ws)
        assert [t.op for t in targets] == ["delete"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "a", "x.txt"))

    def test_path_unlink(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('Path("a").unlink()', ws)
        assert [t.op for t in targets] == ["delete"]

    def test_shutil_rmtree(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('shutil.rmtree("build")', ws)
        assert [t.op for t in targets] == ["delete"]

    def test_write_text(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('Path("report.txt").write_text("hi")', ws)
        assert [t.op for t in targets] == ["write"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "report.txt"))

    def test_os_rename_delete_and_write(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('os.rename("a.txt", "b.txt")', ws)
        assert [t.op for t in targets] == ["delete", "write"]

    def test_os_makedirs(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('os.makedirs("out", exist_ok=True)', ws)
        assert [t.op for t in targets] == ["write"]

    def test_alias_and_join(self, tmp_path):
        ws = str(tmp_path)
        code = 'import os as o\nimport pathlib as pl\npl.Path(o.path.join("sub", "f.txt")).unlink()'
        targets = self._t(code, ws)
        assert [t.op for t in targets] == ["delete"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "sub", "f.txt"))

    def test_variable_literal_tracked(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('p = "out/x.txt"\nos.remove(p)', ws)
        assert len(targets) == 1
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "out", "x.txt"))

    def test_path_var_tracked(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('from pathlib import Path\np = Path("sub")\np.mkdir()', ws)
        assert [t.op for t in targets] == ["write"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "sub"))

    def test_path_div_join(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('from pathlib import Path\np = Path("sub") / "f.txt"\np.write_text("x")', ws)
        assert [t.op for t in targets] == ["write"]
        assert targets[0].resolved == os.path.realpath(os.path.join(ws, "sub", "f.txt"))

    def test_variable_dynamic(self, tmp_path):
        ws = str(tmp_path)
        targets = self._t('p = get_path()\nos.remove(p)', ws)
        assert len(targets) == 1
        assert targets[0].dynamic is True
        assert targets[0].resolved is None

    def test_syntax_error_empty(self, tmp_path):
        assert self._t("def broken(:", str(tmp_path)) == []


# ─── 集成：PythonTool.execute 路径门 ─────────────────────────────


class TestPythonExecuteGate:
    def _tool(self, cwd, store=None):
        return PythonTool(cwd=cwd, approval_store=store)

    def test_outside_write_placeholder(self, test_workspace, tmp_path):
        tool = self._tool(test_workspace, ApprovalStore())
        outside = tmp_path / "o.txt"
        result = tool.execute(action="execute", code=f'open(r"{outside}", "w")')
        assert result.completed is False
        assert not result.error
        assert result.meta[META_KEY]["kind"] == "path:write"
        assert result.meta[META_KEY]["command"] == str(outside)

    def test_inside_write_runs(self, test_workspace, monkeypatch):
        tool = self._tool(test_workspace, ApprovalStore())
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout, stdin: ToolResult("ran"))
        result = tool.execute(action="execute", code='open("in.txt", "w")')
        assert result.output == "ran"
        assert not result.error

    def test_inside_read_runs(self, test_workspace, monkeypatch):
        tool = self._tool(test_workspace, ApprovalStore())
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout, stdin: ToolResult("ran"))
        result = tool.execute(action="execute", code='open("in.txt", "r")')
        assert result.output == "ran"
        assert not result.error

    def test_approved_path_allows(self, test_workspace, tmp_path, monkeypatch):
        store = ApprovalStore()
        tool = self._tool(test_workspace, store)
        outside = tmp_path / "o.txt"
        policy = PathPolicy(workspace_root=test_workspace, cwd=test_workspace, approval_store=store)
        did = policy.decision_id(PathTarget(OP_WRITE, str(outside), resolved=policy.resolve(str(outside))))
        store.approve(did, str(outside), kind="path:write")
        monkeypatch.setattr(tool, "_run_bash", lambda command, timeout, stdin: ToolResult("ran"))
        result = tool.execute(action="execute", code=f'open(r"{outside}", "w")')
        assert result.output == "ran"

    def test_no_store_outside_deny(self, test_workspace, tmp_path):
        tool = self._tool(test_workspace)
        outside = tmp_path / "o.txt"
        result = tool.execute(action="execute", code=f'open(r"{outside}", "w")')
        assert result.error is True

    def test_deny_first(self, test_workspace):
        tool = self._tool(test_workspace, ApprovalStore())
        result = tool.execute(action="execute", code='eval("1+1")')
        assert result.error is True
        assert "blocked" in result.output
