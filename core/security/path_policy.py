"""路径权限判定：写入/删除必须在 workspace 内，越界走审批。

采纳 nanobot 的路径前缀匹配思路（Path.resolve + relative_to，symlink 解析），
但越界不是硬拒绝而是审批：main agent 弹 ask_user 卡，worker/lite 查共享
ApprovalStore（未批准则降级为权限拒绝）。读取不经过本模块（任意放行）。

approval_store 为 None 时 fail-closed：越界一律 deny，不询问。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

OP_WRITE = "write"
OP_DELETE = "delete"

# 动态目标（变量/通配符/$(...) 等无法静态解析）的 decision_id 前缀。
# 动态目标 resolved=None，按原始串生成稳定 ID，保证"原样重发"命中同一 ID。
_DYN_KEY_PREFIX = "dyn"


@dataclass
class PathTarget:
    """一个待判定的写/删目标。

    raw: 原始目标串（可能含 $、* 等）；resolved: realpath 后绝对路径。
    dynamic=True 表示无法静态解析（保守按越界待批）。
    """

    op: str  # OP_WRITE / OP_DELETE
    raw: str
    op_name: str = ""  # 触发词（rm/mv/>/os.remove/open），用于 reason
    dynamic: bool = False
    resolved: str | None = None


def is_path_within(resolved: str, root: str) -> bool:
    """resolved 是否在 root 内（Windows 大小写不敏感，跨盘符视为越界）。

    与 base.Tool._is_within_workspace 同思路：commonpath 两侧 normcase 后比较。
    """
    try:
        return os.path.normcase(
            os.path.commonpath([resolved, root])
        ) == os.path.normcase(root)
    except ValueError:
        return False  # 不同盘符


class PathPolicy:
    """基于路径前缀的写/删权限判定。

    workspace_root 即工作区根（= 工具 cwd）；cwd 用于相对路径 join；
    approval_store 为共享审批存储（master 与 worker/lite 同一实例）。
    """

    def __init__(self, workspace_root: str, cwd: str | None = None,
                 approval_store=None):
        self.cwd = os.path.abspath(cwd or workspace_root)
        self.workspace_root = os.path.realpath(workspace_root)
        self.approval_store = approval_store

    def resolve(self, path: str) -> str:
        """展开 ~ → 相对路径 join cwd → realpath。不做边界抛错。"""
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            p = os.path.join(self.cwd, p)
        return os.path.realpath(p)

    def is_inside(self, resolved: str) -> bool:
        return is_path_within(resolved, self.workspace_root)

    def decision_id(self, target: PathTarget) -> str:
        """路径 → 审批判定 ID。静态按 op+resolved（normcase 保证大小写无关）；
        动态按原始串，原样重发命中同一 ID。"""
        if target.resolved is not None:
            key = f"{target.op}:{os.path.normcase(target.resolved)}"
        else:
            key = f"{_DYN_KEY_PREFIX}:{target.op}:{target.raw.strip()}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def status(self, target: PathTarget) -> str:
        """allow（区内）| approved（外部但已批）| needs_approval（外部未批）| deny（无 store）"""
        if target.resolved is not None and self.is_inside(target.resolved):
            return "allow"
        if self.approval_store is None:
            return "deny"
        if self.approval_store.is_approved(self.decision_id(target)):
            return "approved"
        return "needs_approval"

    def any_denied(self, targets: list[PathTarget]) -> PathTarget | None:
        """按序返回第一个 deny 目标（无 store 时的 fail-closed）。"""
        for t in targets:
            if self.status(t) == "deny":
                return t
        return None

    def first_pending(self, targets: list[PathTarget]) -> PathTarget | None:
        """按序返回第一个 needs_approval 目标（pending 单槽兼容，一次一卡）。"""
        for t in targets:
            if self.status(t) == "needs_approval":
                return t
        return None

    def approval_meta(self, target: PathTarget) -> dict:
        """构造与 bash 审批同构的 meta（META_KEY.approval_required 内容）。"""
        kind = "path:delete" if target.op == OP_DELETE else "path:write"
        display = target.resolved if target.resolved is not None else target.raw
        reason = f"{target.op_name or '操作'}目标不在工作区 {self.workspace_root!r} 内"
        if target.dynamic:
            reason += "（路径含变量/通配符，无法静态解析）"
        return {
            "decision_id": self.decision_id(target),
            "command": display,
            "path": display,
            "kind": kind,
            "reason": reason,
        }
