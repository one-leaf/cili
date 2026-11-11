"""路径权限判定：工作区/临时目录内放行，外部按权限策略裁决。

- ``PathPolicy`` 委托 ``PermissionPolicy`` 做规则/会话批准/分项策略裁决；
- 青少年模式受保护路径硬 deny，strict 未解锁时挂起普通放行通道；
- 读取通过 ``read_status`` 使用 ``outside.read``（含 ``ai`` 路径元数据）。
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass

from core.config import PermissionConfig
from core.fs_utils import normalize_bash_path
from core.security.permission_policy import PermissionPolicy
from core.security.protected_paths import teen_path_verdict

logger = logging.getLogger(__name__)

OP_WRITE = "write"
OP_DELETE = "delete"
OP_READ = "read"

_DYN_KEY_PREFIX = "dyn"


def os_temp_root() -> str:
    """进程环境下的临时目录根（realpath 后），作为路径白名单安全区。"""
    for var in ("TMP", "TEMP"):
        v = os.environ.get(var)
        if v:
            return os.path.realpath(v)
    return os.path.realpath(tempfile.gettempdir())


def is_path_within(resolved: str, root: str) -> bool:
    """resolved 是否在 root 内（Windows 大小写不敏感，跨盘符视为越界）。"""
    try:
        return os.path.normcase(
            os.path.commonpath([resolved, root])
        ) == os.path.normcase(root)
    except ValueError:
        return False


@dataclass
class PathTarget:
    """一个待判定的路径目标。

    static / prefixed / opaque 三态：
    - static: resolved 非空，路径完全可静态确认；
    - prefixed: resolved 为空但 static_prefix 非空，动态部分位于稳定目录下；
    - opaque: 两者都为空，只能按代码 + 目标序号做会话级批准。
    """

    op: str  # OP_READ / OP_WRITE / OP_DELETE
    raw: str
    op_name: str = ""
    dynamic: bool = False
    resolved: str | None = None
    static_prefix: str | None = None
    display: str = ""
    source: str = ""
    code_hash: str = ""
    target_index: int = 0
    suggested_rule: dict | None = None

    @property
    def dynamic_kind(self) -> str:
        if self.resolved:
            return "static"
        if self.static_prefix:
            return "prefixed"
        return "opaque"


class PathPolicy:
    """基于路径前缀的读/写/删权限判定。"""

    def __init__(
        self,
        workspace_root: str,
        cwd: str | None = None,
        *,
        permission_policy: PermissionPolicy | None = None,
        permission_store=None,
        permission_rules=None,
        # 过渡参数：旧调用点仍在传 approval_store / approval_rules
        approval_store=None,
        approval_rules=None,
    ):
        self.cwd = os.path.abspath(cwd or workspace_root)
        self.workspace_root = os.path.realpath(workspace_root)
        self.temp_root = os_temp_root()
        store = permission_store if permission_store is not None else approval_store
        rules = permission_rules if permission_rules is not None else approval_rules
        if rules is not None and not hasattr(rules, "evaluate_path"):
            rules = None
        self.permission_policy = permission_policy or PermissionPolicy(
            PermissionConfig(), rules, store,
            workspace_root=self.workspace_root, cwd=self.cwd,
        )

    # ── 解析 ─────────────────────────────────────────────────────

    def resolve(self, path: str) -> str:
        path = normalize_bash_path(path)
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            p = os.path.join(self.cwd, p)
        return os.path.realpath(p)

    def is_inside(self, resolved: str) -> bool:
        if is_path_within(resolved, self.workspace_root):
            return True
        return is_path_within(resolved, self.temp_root)

    def decision_id(self, target: PathTarget) -> str:
        return self.permission_policy.path_decision_id(
            target.op, target.resolved, target.raw,
            static_prefix=target.static_prefix,
            code_hash=target.code_hash,
            target_index=target.target_index,
        )

    # ── 状态 ─────────────────────────────────────────────────────

    def _verdict_to_status(self, verdict, *, has_channel: bool) -> str:
        if verdict.action == "allow":
            return "approved" if verdict.approved else "allow"
        if verdict.action == "deny":
            return "deny"
        # ask / ai：无审批通道时 fail-closed
        return "needs_approval" if has_channel else "deny"

    def _has_channel(self) -> bool:
        return self.permission_policy.store is not None

    def status(self, target: PathTarget, tool: str = "*") -> str:
        """allow（区内）| approved（规则/会话已批）| needs_approval | deny。"""
        if target.resolved is not None and teen_path_verdict(target.resolved, target.op) == "deny":
            return "deny"

        from core.teen_mode import get_teen_mode

        tm = get_teen_mode()
        teen_locked = tm.is_enabled() and tm.approval_strict() and not tm.is_unlocked()
        if teen_locked:
            if target.resolved is not None and self.is_inside(target.resolved):
                return "allow"
            outside_policy = (tm.policy().get("outside_workspace") or "deny")
            if outside_policy == "approval" and self._has_channel():
                return "needs_approval"
            return "deny"

        kind = "path:delete" if target.op == OP_DELETE else "path:write"
        verdict = self.permission_policy.path_verdict(
            kind, target.resolved,
            decision_id=self.decision_id(target), tool=tool, raw=target.raw,
            static_prefix=target.static_prefix,
        )
        return self._verdict_to_status(verdict, has_channel=self._has_channel())

    def read_status(self, resolved: str | None, tool: str = "*", raw: str = "") -> str:
        """读取状态：allow / approved / needs_approval / deny。"""
        from core.teen_mode import get_teen_mode

        tm = get_teen_mode()
        if (
            tm.is_enabled()
            and tm.policy().get("deny_read_outside")
            and not tm.is_unlocked()
            and resolved is not None
            and not self.is_inside(resolved)
        ):
            return "deny"

        target = PathTarget(OP_READ, raw or (resolved or ""), "read", resolved=resolved)
        verdict = self.permission_policy.path_verdict(
            "path:read", resolved, decision_id=self.decision_id(target), tool=tool, raw=target.raw,
            static_prefix=target.static_prefix,
        )
        return self._verdict_to_status(verdict, has_channel=self._has_channel())

    def any_denied(self, targets: list[PathTarget], tool: str = "*") -> PathTarget | None:
        for t in targets:
            if self.status(t, tool=tool) == "deny":
                return t
        return None

    def first_pending(self, targets: list[PathTarget], tool: str = "*") -> PathTarget | None:
        for t in targets:
            if self.status(t, tool=tool) == "needs_approval":
                return t
        return None

    # ── 证据 / 审批 meta ─────────────────────────────────────────

    def target_evidence(self, target: PathTarget) -> dict:
        return {
            "op": target.op,
            "raw": target.raw,
            "resolved": target.resolved,
            "static_prefix": target.static_prefix,
            "dynamic_kind": target.dynamic_kind,
            "display": target.display,
            "source": target.source,
            "suggested_rule": target.suggested_rule,
            "inside_workspace": bool(target.resolved and self.is_inside(target.resolved)),
            "decision_id": self.decision_id(target),
        }

    def permission_meta(self, target: PathTarget, tool: str = "*") -> dict:
        """构造与命令审批同构的 meta（含 display / dynamic_kind / suggested_rule）。"""
        kind = {
            OP_READ: "path:read",
            OP_DELETE: "path:delete",
        }.get(target.op, "path:write")
        action = {"path:read": "读取", "path:delete": "删除"}.get(kind, "写入")
        display = target.display or target.resolved or target.raw

        if target.dynamic_kind == "opaque":
            reason = f"{target.op_name or action}目标无法静态解析（变量/通配符）"
        elif target.dynamic_kind == "prefixed":
            reason = f"{target.op_name or action}目标为动态路径，前缀 {target.static_prefix} 已确认"
        else:
            reason = f"{target.op_name or action}目标不在工作区 {self.workspace_root!r} 内"

        verdict = self.permission_policy.path_verdict(
            kind, target.resolved,
            decision_id=self.decision_id(target), tool=tool, raw=target.raw,
            static_prefix=target.static_prefix,
        )
        return {
            "decision_id": self.decision_id(target),
            "command": display,
            "path": display,
            "display": display,
            "dynamic_kind": target.dynamic_kind,
            "static_prefix": target.static_prefix,
            "source": target.source,
            "target_index": target.target_index,
            "suggested_rule": target.suggested_rule,
            "kind": kind,
            "reason": reason,
            "mode": verdict.action,
            "verdict": verdict.action,
            "rule": verdict.rule,
            "tool": tool,
        }

    # 旧名兼容（过渡）
    approval_meta = permission_meta
