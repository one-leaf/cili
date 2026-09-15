# -*- coding: utf-8 -*-
"""路径权限判定（core/security/path_policy.py）单元测试。

覆盖：is_path_within 边界语义；resolve 解析；decision_id 写删分离/大小写无关/
动态稳定；status 四态；first_pending/any_denied 聚合；approval_meta 结构与 reason。
"""

import os
import pytest

from core.tools.approval import ApprovalStore
from core.security.path_policy import (
    OP_DELETE,
    OP_WRITE,
    PathPolicy,
    PathTarget,
    is_path_within,
)


class TestIsPathWithin:
    def test_child_inside(self, tmp_path):
        root = str(tmp_path)
        assert is_path_within(os.path.join(root, "a", "b"), root) is True

    def test_self_inside(self, tmp_path):
        root = str(tmp_path)
        assert is_path_within(root, root) is True

    def test_parent_outside(self, tmp_path):
        root = os.path.join(str(tmp_path), "ws")
        assert is_path_within(str(tmp_path), root) is False

    def test_sibling_outside(self, tmp_path):
        root = str(tmp_path)
        sibling = os.path.join(str(tmp_path.parent), "other")
        assert is_path_within(sibling, root) is False

    @pytest.mark.skipif(os.name != "nt", reason="Windows 大小写不敏感")
    def test_case_insensitive_windows(self, tmp_path):
        root = str(tmp_path)
        assert is_path_within(os.path.join(root, "x"), root.upper()) is True


class TestPathPolicyResolve:
    def _policy(self, tmp_path):
        return PathPolicy(workspace_root=str(tmp_path))

    def test_relative_joins_cwd(self, tmp_path):
        policy = self._policy(tmp_path)
        assert policy.resolve("sub/file.txt") == os.path.realpath(
            os.path.join(str(tmp_path), "sub", "file.txt")
        )

    def test_absolute_kept(self, tmp_path):
        policy = self._policy(tmp_path)
        abs_path = os.path.join(str(tmp_path), "x.txt")
        assert policy.resolve(abs_path) == os.path.realpath(abs_path)

    def test_dotdot_normalized(self, tmp_path):
        policy = self._policy(tmp_path)
        assert policy.resolve("sub/../file.txt") == os.path.realpath(
            os.path.join(str(tmp_path), "file.txt")
        )

    def test_tilde_expands(self, tmp_path):
        policy = self._policy(tmp_path)
        home = os.path.expanduser("~")
        assert policy.resolve("~/x.txt").startswith(home)


class TestDecisionId:
    def _policy(self, tmp_path):
        return PathPolicy(workspace_root=str(tmp_path))

    def test_write_delete_separate(self, tmp_path):
        p = self._policy(tmp_path)
        assert p.decision_id(PathTarget(OP_WRITE, "C:\\x", resolved="C:\\x")) != \
            p.decision_id(PathTarget(OP_DELETE, "C:\\x", resolved="C:\\x"))

    def test_case_insensitive_same(self, tmp_path):
        p = self._policy(tmp_path)
        a = p.decision_id(PathTarget(OP_WRITE, "C:\\Work\\x", resolved="C:\\Work\\x"))
        b = p.decision_id(PathTarget(OP_WRITE, "c:\\work\\x", resolved="c:\\work\\x"))
        assert a == b

    def test_different_path_different_id(self, tmp_path):
        p = self._policy(tmp_path)
        assert p.decision_id(PathTarget(OP_WRITE, "C:\\x", resolved="C:\\x")) != \
            p.decision_id(PathTarget(OP_WRITE, "C:\\y", resolved="C:\\y"))

    def test_dynamic_stable(self, tmp_path):
        p = self._policy(tmp_path)
        a = p.decision_id(PathTarget(OP_DELETE, "$DIR/x", dynamic=True))
        b = p.decision_id(PathTarget(OP_DELETE, "$DIR/x", dynamic=True))
        assert a == b
        assert a != p.decision_id(PathTarget(OP_DELETE, "$DIR/y", dynamic=True))


class TestStatus:
    def _policy(self, tmp_path, store=None):
        return PathPolicy(workspace_root=str(tmp_path), approval_store=store)

    def test_inside_allow(self, tmp_path):
        p = self._policy(tmp_path)
        t = PathTarget(OP_WRITE, "in.txt", resolved=os.path.join(str(tmp_path), "in.txt"))
        assert p.status(t) == "allow"

    def test_outside_deny_without_store(self, tmp_path):
        p = self._policy(tmp_path)  # approval_store=None → fail-closed
        t = PathTarget(OP_WRITE, "C:\\out", resolved="C:\\out")
        assert p.status(t) == "deny"

    def test_outside_needs_approval(self, tmp_path):
        p = self._policy(tmp_path, ApprovalStore())
        t = PathTarget(OP_WRITE, "C:\\out", resolved="C:\\out")
        assert p.status(t) == "needs_approval"

    def test_outside_approved(self, tmp_path):
        store = ApprovalStore()
        p = self._policy(tmp_path, store)
        t = PathTarget(OP_WRITE, "C:\\out", resolved="C:\\out")
        store.approve(p.decision_id(t), "C:\\out", kind="path:write")
        assert p.status(t) == "approved"

    def test_dynamic_needs_approval(self, tmp_path):
        p = self._policy(tmp_path, ApprovalStore())
        t = PathTarget(OP_DELETE, "rm build/*", op_name="rm", dynamic=True)
        assert p.status(t) == "needs_approval"

    def test_dynamic_deny_without_store(self, tmp_path):
        p = self._policy(tmp_path)
        t = PathTarget(OP_DELETE, "rm $DIR/x", op_name="rm", dynamic=True)
        assert p.status(t) == "deny"


class TestAggregation:
    def test_first_pending_skips_allow_and_approved(self, tmp_path):
        store = ApprovalStore()
        p = PathPolicy(workspace_root=str(tmp_path), approval_store=store)
        inside = PathTarget(OP_WRITE, "in", resolved=os.path.join(str(tmp_path), "in"))
        approved = PathTarget(OP_WRITE, "C:\\a", resolved="C:\\a")
        pending = PathTarget(OP_DELETE, "C:\\b", resolved="C:\\b")
        store.approve(p.decision_id(approved), "C:\\a", kind="path:write")
        assert p.first_pending([inside, approved, pending]) is pending

    def test_first_pending_none_when_all_ok(self, tmp_path):
        p = PathPolicy(workspace_root=str(tmp_path), approval_store=ApprovalStore())
        inside = PathTarget(OP_WRITE, "in", resolved=os.path.join(str(tmp_path), "in"))
        assert p.first_pending([inside]) is None

    def test_any_denied_without_store(self, tmp_path):
        p = PathPolicy(workspace_root=str(tmp_path))
        inside = PathTarget(OP_WRITE, "in", resolved=os.path.join(str(tmp_path), "in"))
        outside = PathTarget(OP_WRITE, "C:\\out", resolved="C:\\out")
        assert p.any_denied([inside, outside]) is outside
        assert p.any_denied([inside]) is None


class TestApprovalMeta:
    def _policy(self, tmp_path):
        return PathPolicy(workspace_root=str(tmp_path))

    def test_write_kind(self, tmp_path):
        p = self._policy(tmp_path)
        t = PathTarget(OP_WRITE, "C:\\out", op_name="write", resolved="C:\\out")
        meta = p.approval_meta(t)
        assert meta["kind"] == "path:write"
        assert meta["command"] == "C:\\out"
        assert meta["decision_id"] == p.decision_id(t)
        assert "工作区" in meta["reason"]
        assert "无法静态解析" not in meta["reason"]

    def test_delete_kind(self, tmp_path):
        p = self._policy(tmp_path)
        t = PathTarget(OP_DELETE, "rm out", op_name="rm", resolved="C:\\out")
        assert p.approval_meta(t)["kind"] == "path:delete"

    def test_dynamic_reason_notes_unresolvable(self, tmp_path):
        p = self._policy(tmp_path)
        t = PathTarget(OP_DELETE, "$DIR/x", op_name="rm", dynamic=True)
        meta = p.approval_meta(t)
        assert meta["kind"] == "path:delete"
        assert meta["command"] == "$DIR/x"
        assert "无法静态解析" in meta["reason"]
