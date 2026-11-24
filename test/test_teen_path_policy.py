"""青少年模式路径层测试：受保护路径硬 deny、越界降级、规则挂起。"""

import os

from core.security.path_policy import PathPolicy


def _make_policy(test_workspace, tmp_path):

    return PathPolicy(workspace_root=test_workspace, cwd=test_workspace)


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


class TestPathPolicyTeen:
    """PathPolicy.status：受保护路径 / 越界降级 / 规则挂起。"""

    def test_protected_data_dir_denied(self, teen, test_workspace):
        from core.security.path_policy import OP_WRITE, PathTarget

        teen.enable("parent123")
        policy = _make_policy(test_workspace, None)
        target = PathTarget(OP_WRITE, "x", "write",
                            resolved=os.path.join(test_workspace, ".cili", "sessions", "s1"))
        assert policy.status(target) == "deny"

    def test_protected_system_data_denied(self, teen, test_workspace):
        from core.config import DATA_DIR
        from core.security.path_policy import OP_DELETE, PathTarget

        teen.enable("parent123")
        policy = _make_policy(test_workspace, None)
        target = PathTarget(OP_DELETE, "x", "delete",
                            resolved=str(DATA_DIR / "setting.json"))
        assert policy.status(target) == "deny"

    def test_cili_tmp_exception(self, teen, test_workspace):
        from core.security.path_policy import OP_WRITE, PathTarget

        teen.enable("parent123")
        policy = _make_policy(test_workspace, None)
        target = PathTarget(OP_WRITE, "x", "write",
                            resolved=os.path.join(test_workspace, ".cili", "tmp", "f.txt"))
        # tmp 子目录不受保护（临时文件不持久化）——但 teen 未解锁时区外仍按
        # outside_workspace 判定；.cili/tmp 在工作区内 → allow
        assert policy.status(target) == "allow"

    def test_nested_workspace_under_tmp_denied(self, teen, test_workspace, monkeypatch):
        """宽根的 tmp 例外不得放行嵌在其 tmp/ 内的更具体工作区数据目录。"""
        from core.security.teen_paths import teen_path_verdict

        teen.enable("parent123")
        broad = os.path.join(test_workspace, ".cili")
        nested = os.path.join(broad, "tmp", "proj", ".cili")
        # 模拟 protected_roots 的实际顺序：宽根在前，嵌套工作区根在后
        monkeypatch.setattr(
            "core.security.teen_paths.protected_roots",
            lambda: [broad, nested],
        )
        assert teen_path_verdict(os.path.join(nested, "sessions", "s1"), "write") == "deny"
        # 纯临时文件仍按 tmp 例外放行
        assert teen_path_verdict(os.path.join(broad, "tmp", "f.txt"), "write") is None

    def test_outside_downgraded_to_deny(self, teen, test_workspace, tmp_path, monkeypatch):
        from core.security.path_policy import OP_WRITE, PathTarget

        other_tmp = str(tmp_path / "other-tmp")
        os.makedirs(other_tmp, exist_ok=True)
        monkeypatch.setenv("TMP", other_tmp)
        monkeypatch.setenv("TEMP", other_tmp)
        teen.enable("parent123")
        policy = _make_policy(test_workspace, None)
        target = PathTarget(OP_WRITE, "x", "write", resolved="D:/somewhere/outside.txt")
        # 青少年模式：现状 needs_approval → deny（无解锁窗口）
        assert policy.status(target) == "deny"

    def test_outside_normal_when_disabled(self, teen, test_workspace):
        from core.security.path_policy import OP_WRITE, PathTarget

        policy = _make_policy(test_workspace, None)
        target = PathTarget(OP_WRITE, "x", "write", resolved="D:/somewhere/outside.txt")
        # 关闭时维持现状：有审批通道 → needs_approval；无通道 → deny
        assert policy.status(target) in ("needs_approval", "deny")

    def test_rules_suspended_when_locked(self, teen, test_workspace):
        """strict 且未解锁：allow 规则挂起，先前攒下的放行不再生效。"""
        from core.security.path_policy import OP_WRITE, PathTarget

        teen.enable("parent123")
        teen.unlock("parent123")
        policy = PathPolicy(workspace_root=test_workspace, cwd=test_workspace,
                            permission_rules=_FakeRules(hit=True))
        target = PathTarget(OP_WRITE, "x", "write", resolved="D:/out.txt")
        # 解锁窗口内：规则生效 → approved
        assert policy.status(target) == "approved"
        teen.lock()
        # 回锁：规则挂起 → deny（strict）
        assert policy.status(target) == "deny"


