"""高风险命令的会话级审批共享模块。

三档拦截（allow/ask/deny）中的 ask 档：命令命中后不直接拒绝，
先查 ApprovalStore 是否已在本会话内被用户批准；未批准则返回
completed=False 占位符，由 Master Agent 循环合成 ask_user 卡询问用户。

ApprovalStore 由 Master Agent 持有，Master/Worker/Lite 的 bash/pwsh 与
agent 委派共享同一实例：Master 批准的会话级命令在 Worker/Lite 中同样
放行。仅存内存，服务器重启即失效，不做任何持久化。
"""

from __future__ import annotations

import hashlib
from typing import Any

MODE_ASK = "ask"
MODE_DENY = "deny"

APPROVE_LABEL = "允许本次会话"
REJECT_LABEL = "拒绝"

# ToolResult.meta 中标记"需要用户批准"的键
META_KEY = "approval_required"


def canonicalize_command(command: str) -> str:
    """保守规范化命令，用于生成稳定的 decision_id。

    仅折叠连续空白，不做任何解析——避免误判。模型批准后按原样重发即可命中。
    """
    return " ".join(command.split())


def approval_decision_id(command: str) -> str:
    """命令 → 会话内审批判定 ID（sha256 前 16 位，确定性）。"""
    return hashlib.sha256(canonicalize_command(command).encode("utf-8")).hexdigest()[:16]


def build_approval_question(approval: dict[str, Any]) -> str:
    """构造发给用户的批准问题文案。"""
    return (
        f"检测到高风险命令，需要您批准后才能执行：\n\n"
        f"命令：`{approval['command']}`\n"
        f"拦截原因：{approval['reason']}\n"
        f"判定编号：{approval['decision_id']}\n\n"
        f"批准后，本次会话内执行相同命令（包括委派给子代理）将不再询问。"
    )


def approval_placeholder_text(approval: dict[str, Any]) -> str:
    """工具返回给 LLM 的占位文本（completed=False，等待用户批准）。"""
    return (
        "该命令被高风险拦截，需要用户批准后才能执行。\n"
        "等待用户选择「允许本次会话」或「拒绝」...\n"
        "若用户允许，请**原样重发**该命令执行。"
    )


def build_approved_commands_section(approval_store: "ApprovalStore | None") -> str:
    """子代理任务消息中下放的已批准命令段落（无批准时返回空串）。"""
    if not approval_store:
        return ""
    approved = approval_store.approved_commands()
    if not approved:
        return ""
    lines = [
        "### Pre-approved commands",
        "",
        "The following commands have been approved by the user for this session and may be executed directly:",
        "",
    ]
    lines += [f"- `{cmd}`" for cmd in approved]
    lines.append("")
    return "\n".join(lines)


class ApprovalStore:
    """会话级高风险命令审批存储（内存，不持久化）。

    - _approved: decision_id -> command，会话内持续生效，不按次数消费
    - pending: 单槽待批准项 {decision_id, command, reason}，由 root 循环写入、
      answer 端点读取后清空
    """

    def __init__(self) -> None:
        self._approved: dict[str, str] = {}
        self.pending: dict[str, Any] | None = None

    def is_approved(self, decision_id: str) -> bool:
        return decision_id in self._approved

    def approve(self, decision_id: str, command: str) -> None:
        self._approved[decision_id] = command

    def approved_commands(self) -> list[str]:
        return list(self._approved.values())

    def set_pending(self, approval: dict[str, Any]) -> None:
        self.pending = approval

    def clear_pending(self) -> None:
        self.pending = None
