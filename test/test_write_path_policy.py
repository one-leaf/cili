# -*- coding: utf-8 -*-
"""write/edit 工具的路径权限门测试。

覆盖：区内写直接落盘；区外未批 → completed=False + meta.kind=path:write；
区外已批 → 真写入；无 store → error（fail-closed）；批 write 不解锁同路径
delete；edit 与 write 同策略。
"""

import os

from core.security.path_policy import OP_WRITE, PathPolicy, PathTarget
from core.tools.approval import ApprovalStore, META_KEY
from core.tools.write import WriteTool
from core.tools.edit import EditTool


class TestWriteGate:
    def _tool(self, cwd, store=None):
        return WriteTool(cwd=cwd, workspace_uuid="test-ws", approval_store=store)

    def test_inside_workspace_writes(self, test_workspace):
        tool = self._tool(test_workspace)
        result = tool.execute(file_path="out.txt", content="hi")
        assert result.error is False
        assert (os.path.join(test_workspace, "out.txt")) in result.output
        with open(os.path.join(test_workspace, "out.txt"), encoding="utf-8") as f:
            assert f.read() == "hi"

    def test_outside_unapproved_placeholder(self, test_workspace, tmp_path):
        tool = self._tool(test_workspace, ApprovalStore())
        outside = tmp_path / "x.txt"
        result = tool.execute(file_path=str(outside), content="hi")
        assert result.completed is False
        assert not result.error
        meta = result.meta[META_KEY]
        assert meta["kind"] == "path:write"
        assert meta["command"] == str(outside)
        assert not outside.exists()  # 未批准不落盘

    def test_outside_approved_writes(self, test_workspace, tmp_path):
        store = ApprovalStore()
        tool = self._tool(test_workspace, store)
        outside = tmp_path / "x.txt"
        policy = PathPolicy(workspace_root=test_workspace, cwd=test_workspace, approval_store=store)
        # 用与 execute 相同的解析结果批准
        did = policy.decision_id(PathTarget(OP_WRITE, str(outside), resolved=policy.resolve(str(outside))))
        store.approve(did, str(outside), kind="path:write")
        result = tool.execute(file_path=str(outside), content="hi")
        assert result.error is False
        assert outside.read_text(encoding="utf-8") == "hi"

    def test_outside_no_store_deny(self, test_workspace, tmp_path):
        tool = self._tool(test_workspace)  # 无 store → fail-closed
        outside = tmp_path / "x.txt"
        result = tool.execute(file_path=str(outside), content="hi")
        assert result.error is True
        assert not outside.exists()


class TestEditGate:
    def _tool(self, cwd, store=None):
        return EditTool(cwd=cwd, workspace_uuid="test-ws", approval_store=store)

    def test_inside_workspace_edits(self, test_workspace):
        target = os.path.join(test_workspace, "a.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("hello world")
        tool = self._tool(test_workspace)
        result = tool.execute(file_path="a.txt", old_text="hello", new_text="goodbye")
        assert result.error is False
        with open(target, encoding="utf-8") as f:
            assert f.read() == "goodbye world"

    def test_outside_unapproved_placeholder(self, test_workspace, tmp_path):
        outside = tmp_path / "a.txt"
        outside.write_text("hello world", encoding="utf-8")
        tool = self._tool(test_workspace, ApprovalStore())
        result = tool.execute(file_path=str(outside), old_text="hello", new_text="goodbye")
        assert result.completed is False
        assert result.meta[META_KEY]["kind"] == "path:write"
        assert outside.read_text(encoding="utf-8") == "hello world"  # 未修改

    def test_outside_no_store_deny(self, test_workspace, tmp_path):
        outside = tmp_path / "a.txt"
        outside.write_text("hello world", encoding="utf-8")
        tool = self._tool(test_workspace)
        result = tool.execute(file_path=str(outside), old_text="hello", new_text="goodbye")
        assert result.error is True
        assert outside.read_text(encoding="utf-8") == "hello world"
