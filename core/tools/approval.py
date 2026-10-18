"""高风险动作的会话级审批共享模块（占位符 / 存储 / 文案 / 审计）。

审批流程重构（workspace/approval-flow-design.md）：静态 deny 黑名单已移除——工具层
只做「规则匹配 + 证据收集」，未命中返回 completed=False 占位符，由 Loop 做 lite 判定
分派（allow → 落盘规则并重执行 / ask → 合成确认卡 / deny → 拒绝，判定器见
core/security/approval_judge.py，规则引擎见 core/security/approval_rules.py）。

本模块保留：

- ApprovalStore：会话级放行（decision_id → {command, kind}，批准不消费）+ 单槽
  pending。持久化已移交 ApprovalRules——本类只管会话内存状态（重启失效）。
- 文案：卡片问题（含含义/风险/理由）、占位文本、四分拒绝文本、子代理下放段落。
- 审计：append_approval_audit（asked/decided 成对事件 → {session_dir}/approval_audit.jsonl）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ToolResult.meta 中标记"需要判定/批准"的键（占位符管道，未变）
META_KEY = "approval_required"

APPROVE_LABEL = "允许本次会话"
REMEMBER_LABEL = "一直允许（此类命令）"
REJECT_LABEL = "拒绝"

_RISK_LABELS = {"low": "低", "medium": "中", "high": "高"}


def canonicalize_command(command: str) -> str:
    """保守规范化命令，用于生成稳定的 decision_id。

    仅折叠连续空白，不做任何解析——避免误判。模型批准后按原样重发即可命中。
    """
    return " ".join(command.split())


def approval_decision_id(command: str) -> str:
    """命令 → 会话内审批判定 ID（sha256 前 16 位，确定性）。"""
    return hashlib.sha256(canonicalize_command(command).encode("utf-8")).hexdigest()[:16]


def _risk_label(risk: str) -> str:
    return _RISK_LABELS.get(risk, "中")


def build_approval_question(approval: dict[str, Any]) -> str:
    """构造发给用户的批准问题文案（含含义/风险/理由，设计 §3.4）。"""
    kind = approval.get("kind", "command")
    summary = str(approval.get("summary") or "").strip()
    risk = str(approval.get("risk") or "").strip()
    reason = str(approval.get("reason") or "").strip()
    display = str(approval.get("rule_display") or "").strip()

    if kind == "browser:navigate":
        head = (
            f"检测到非公网地址导航，需要您批准后才能执行：\n\n"
            f"地址：`{approval['command']}`\n"
        )
    elif kind == "path:delete":
        head = (
            f"检测到工作区外的文件操作，需要您批准后才能执行：\n\n"
            f"操作：删除 `{approval['command']}`\n"
        )
    elif kind.startswith("path:"):
        head = (
            f"检测到工作区外的文件操作，需要您批准后才能执行：\n\n"
            f"操作：写入 `{approval['command']}`\n"
        )
    else:
        head = (
            f"检测到需要确认的命令，需要您批准后才能执行：\n\n"
            f"命令：`{approval['command']}`\n"
        )

    if summary:
        head += f"含义：{summary}\n"
    if risk:
        head += f"风险：{_risk_label(risk)}\n"
    if reason:
        head += f"判定理由：{reason}\n"
    head += f"判定编号：{approval['decision_id']}\n"

    target_label = "路径" if kind.startswith("path:") else ("地址" if kind == "browser:navigate" else "命令")
    persist_desc = f"（{display}）" if display else ""
    return (
        f"{head}\n"
        f"「{APPROVE_LABEL}」仅本次会话放行；「{REMEMBER_LABEL}」会写入此工作区{persist_desc}，"
        f"重启后仍自动放行同类{target_label}（包括委派给子代理）；「{REJECT_LABEL}」仅拦截本次执行。"
    )


def approval_placeholder_text(approval: dict[str, Any]) -> str:
    """工具返回给 LLM 的占位文本（completed=False）。

    该文本通常不会到达模型——Loop 会用判定结论（放行重执行 / 确认卡 / 四分拒绝
    文本）替换它；仅在判定器缺失等边界场景兜底可见。
    """
    kind = approval.get("kind", "command")
    target_label = "地址" if kind == "browser:navigate" else ("路径" if kind.startswith("path:") else "命令")
    return (
        f"Error: 该{target_label}未命中允许规则，已被拦截，等待安全判定（自动判定或用户确认）。\n"
        f"{target_label}：`{approval.get('command', '')}`\n"
        f"判定编号：{approval.get('decision_id', '')}\n"
        f"拦截原因：{approval.get('reason', '')}\n\n"
        f"请勿原样重发；等待判定结果或用户决定。\n"
        f"如果你是子代理（worker/lite）：无法等待用户确认，请换用非拦截命令，或向父代理报告此操作。"
    )


# ─── 四分拒绝文本（设计 §3.6：模型需要分清「人不干」和「没人可问」） ────────

def judge_deny_text(approval: dict[str, Any], summary: str, reason: str, alternative: str) -> str:
    alt = f"建议替代做法：{alternative}。" if alternative else ""
    return (
        f"该命令被安全判定拒绝：{reason}\n"
        f"命令含义：{summary or approval.get('command', '')}\n{alt}"
        f"判定编号：{approval.get('decision_id', '')}\n"
        f"（系统提示：不要原样重发；请按替代建议调整，或改用其他方式完成目标。）"
    )


def no_channel_text(approval: dict[str, Any], summary: str = "") -> str:
    meaning = f"（{summary}）" if summary else ""
    return (
        f"该命令需要用户确认{meaning}，但当前是自主执行的子代理，无询问通道。\n"
        f"命令：`{approval.get('command', '')}`\n"
        f"判定编号：{approval.get('decision_id', '')}\n"
        f"（系统提示：请换用非拦截命令，或向父代理报告此操作。）"
    )


def judge_unavailable_text(approval: dict[str, Any], error: str) -> str:
    return (
        f"自动判定暂不可用（{error}），已按保守策略拒绝。\n"
        f"命令：`{approval.get('command', '')}`\n"
        f"判定编号：{approval.get('decision_id', '')}\n"
        f"（系统提示：可稍后重试相同命令，或改用其他方式完成目标。）"
    )


def user_reject_hint(answer: str, approval: dict[str, Any] | None) -> str:
    """用户在卡片上选「拒绝」/自由输入时，附加给模型的引导（注入占位符文本尾部）。"""
    summary = str((approval or {}).get("summary") or "").strip()
    meaning = f"（{summary}）" if summary else ""
    return (
        f"{answer}\n\n"
        f"（系统提示：用户拒绝了该命令{meaning}。请改用其他方式完成目标，不要原样重发该命令。）"
    )


def build_approved_commands_section(store: "ApprovalStore | None", rules=None) -> str:
    """子代理任务消息中下放的已批准命令/导航/路径与已落盘规则段落（无则空串）。

    worker/lite 据此得知主会话已放行的内容，避免重复尝试被拒（放行本身由共享
    store / rules 保证，本段仅作提示）。
    """
    if not store and not rules:
        return ""
    lines: list[str] = []
    if store:
        approved = store.approved_commands()
        navigations = store.approved_navigations()
        path_rules = store.approved_path_rules()
        if approved or navigations or path_rules:
            lines += ["### Session-approved actions", ""]
            if approved:
                lines.append("The following commands have been approved by the user for this session:")
                lines += [f"- `{cmd}`" for cmd in approved]
            if navigations:
                lines.append("The following navigation URLs have been approved for this session:")
                lines += [f"- navigate `{url}`" for url in navigations]
            if path_rules:
                lines.append("The following file operations have been approved for this session:")
                for rule in path_rules:
                    verb = "delete" if rule["kind"] == "path:delete" else "write"
                    lines.append(f"- {verb} `{rule['command']}`")
            lines.append("")
    if rules:
        displays = rules.granted_displays()
        if displays:
            lines += [
                "### Workspace-approved rules",
                "",
                "The following rules were approved (by the user or the safety judge) in this",
                "workspace and take effect permanently:",
            ]
            lines += [f"- {d}" for d in displays]
            lines.append("")
    return "\n".join(lines) if lines else ""


def append_approval_audit(session_dir, event: str, data: dict[str, Any]) -> None:
    """审批审计：asked / decided 成对事件追加到 {session_dir}/approval_audit.jsonl。

    独立于会话消息（不进 LLM 上下文、不进聊天 UI），供误判复盘与设置页审计。
    任何失败静默（审计绝不影响审批主流程）。
    """
    try:
        if not session_dir:
            return
        d = Path(session_dir)
        d.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "event": event, **data}
        with open(d / "approval_audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        logger.debug("[approval-audit] 写审计事件失败", exc_info=True)


class ApprovalStore:
    """会话级放行存储：decision_id → {command, kind}，批准不消费；单槽 pending。

    - _approved：会话内持续生效（含委派给子代理），重启失效；
    - pending：单槽待判定/待批准项，由 Loop 写入、answer 端点读取后清空；
    - 持久化规则见 ApprovalRules（core/security/approval_rules.py），本类不落盘。
    """

    def __init__(self) -> None:
        self._approved: dict[str, dict[str, str]] = {}
        self.pending: dict[str, Any] | None = None

    def is_approved(self, decision_id: str) -> bool:
        return decision_id in self._approved

    def approve(self, decision_id: str, command: str, kind: str = "command") -> None:
        """记录会话级批准（不消费；重启失效）。"""
        self._approved[decision_id] = {"command": command, "kind": kind}

    def approved_commands(self) -> list[str]:
        """仅命令类规则（kind == "command"）。"""
        return [v["command"] for v in self._approved.values() if v["kind"] == "command"]

    def approved_navigations(self) -> list[str]:
        """导航类规则（kind == "browser:navigate"）。"""
        return [v["command"] for v in self._approved.values() if v["kind"] == "browser:navigate"]

    def approved_path_rules(self) -> list[dict[str, str]]:
        """路径类规则（kind == "path:write"/"path:delete"）。"""
        return [
            {"kind": v["kind"], "command": v["command"]}
            for v in self._approved.values() if v["kind"].startswith("path:")
        ]

    def set_pending(self, approval: dict[str, Any]) -> None:
        self.pending = approval

    def clear_pending(self) -> None:
        self.pending = None
