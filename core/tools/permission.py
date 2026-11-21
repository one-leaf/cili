"""权限卡片文案与决策 id。

会话批准存储与审计已移至 ``core/security/permission_state.py``；本模块只保留
给 LLM / 前端看的文案、占位文本与 decision_id。为既有调用方保留重导出。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from core.approval_cards import ABORT_LABEL, APPROVE_LABEL, REJECT_LABEL, REMEMBER_LABEL
from core.security.permission_rules import canonical_text
from core.security.permission_state import PermissionStore, append_permission_audit

__all__ = [
    "ABORT_LABEL",
    "APPROVE_LABEL",
    "META_KEY",
    "PermissionStore",
    "REJECT_LABEL",
    "REMEMBER_LABEL",
    "append_permission_audit",
    "build_approved_actions_section",
    "build_permission_question",
    "canonicalize_command",
    "denied_rule_text",
    "judge_deny_text",
    "judge_unavailable_text",
    "no_channel_text",
    "permission_decision_id",
    "permission_placeholder_text",
    "user_reject_hint",
]

logger = logging.getLogger(__name__)

META_KEY = "permission_required"

_RISK_LABELS = {"low": "低", "medium": "中", "high": "高"}


def canonicalize_command(command: str) -> str:
    """仅折叠连续空白，生成稳定 decision_id（与规则引擎共用 canonical_text）。"""
    return canonical_text(command)


def permission_decision_id(command: str) -> str:
    return hashlib.sha256(canonicalize_command(command).encode("utf-8")).hexdigest()[:16]


def _risk_label(risk: str) -> str:
    return _RISK_LABELS.get(risk, "中")


def _target_label(kind: str) -> str:
    if kind == "browser:navigate":
        return "地址"
    if kind.startswith("path:"):
        return "路径"
    return "操作"


def build_permission_question(permission: dict[str, Any]) -> str:
    """构造权限确认卡文案（含含义/风险/理由/动态路径说明）。"""
    kind = permission.get("kind", "command")
    summary = str(permission.get("summary") or "").strip()
    risk = str(permission.get("risk") or "").strip()
    reason = str(permission.get("reason") or "").strip()
    display = str(permission.get("display") or permission.get("command") or "").strip()
    dynamic_kind = str(permission.get("dynamic_kind") or "")
    source = str(permission.get("source") or "").strip()

    def _path_verb(default: str) -> str:
        if display.startswith(("读取", "写入", "删除")):
            return display
        return f"{default} {display}".strip()

    if kind == "browser:navigate":
        head = f"检测到非公网地址导航，需要您批准后才能执行：\n\n地址：`{display}`\n"
    elif kind == "path:read":
        head = f"检测到工作区外文件读取，需要您批准后才能执行：\n\n操作：{_path_verb('读取')}\n"
    elif kind == "path:delete":
        head = f"检测到工作区外文件操作，需要您批准后才能执行：\n\n操作：{_path_verb('删除')}\n"
    elif kind.startswith("path:"):
        head = f"检测到工作区外文件操作，需要您批准后才能执行：\n\n操作：{_path_verb('写入')}\n"
    else:
        head = f"检测到需要确认的操作，需要您批准后才能执行：\n\n操作：`{display}`\n"

    if source:
        head += f"位置：{source}\n"
    if summary:
        head += f"含义：{summary}\n"
    if risk:
        head += f"风险：{_risk_label(risk)}\n"
    if reason:
        head += f"判定理由：{reason}\n"
    head += f"判定编号：{permission['decision_id']}\n"

    target_label = _target_label(kind)
    if dynamic_kind == "opaque":
        return (
            f"{head}\n"
            f"「{APPROVE_LABEL}」仅本次会话放行；运行时路径无法生成持久规则。"
            f"「{REJECT_LABEL}」仅拦截本次执行。"
        )

    persist_desc = f"（{display}）" if display else ""
    return (
        f"{head}\n"
        f"「{APPROVE_LABEL}」仅本次会话放行；「{REMEMBER_LABEL}」会写入此工作区{persist_desc}，"
        f"重启后仍自动放行同类{target_label}（包括委派给子代理）；「{REJECT_LABEL}」仅拦截本次执行。"
    )


def permission_placeholder_text(permission: dict[str, Any]) -> str:
    """工具返回给 LLM 的占位文本（completed=False）。"""
    kind = permission.get("kind", "command")
    target_label = _target_label(kind)
    target = str(permission.get("display") or permission.get("command") or "")
    dynamic_kind = str(permission.get("dynamic_kind") or "")
    hint = "该路径在运行时确定，无法生成持久规则；仅可请求本次会话批准。" if dynamic_kind == "opaque" else ""
    return (
        f"Error: 该{target_label}未命中允许规则，已被拦截，等待权限判定（自动判定或用户确认）。\n"
        f"{target_label}：`{target}`\n"
        f"判定编号：{permission.get('decision_id', '')}\n"
        f"拦截原因：{permission.get('reason', '')}\n\n"
        f"{hint}请勿原样重发；等待判定结果或用户决定。\n"
        f"如果你是子代理（worker/lite）：无法等待用户确认，请换用非拦截方式，或向父代理报告此操作。"
    )


# ─── 拒绝文案 ──────────────────────────────────────────────────────────

def denied_rule_text(permission: dict[str, Any], rule: dict[str, Any] | None) -> str:
    rule = rule or {}
    note = str(rule.get("note") or rule.get("display") or "命中黑名单")
    display = str(rule.get("display") or note)
    return (
        f"该操作被权限黑名单拒绝：{note}\n"
        f"操作：`{permission.get('command', '')}`\n"
        f"规则：{display}\n"
        f"判定编号：{permission.get('decision_id', '')}\n"
        f"（系统提示：不要改写命令绕过黑名单；请换方案或告知用户。）"
    )


def judge_deny_text(permission: dict[str, Any], summary: str, reason: str, alternative: str) -> str:
    alt = f"建议替代做法：{alternative}。" if alternative else ""
    return (
        f"该操作被权限判定拒绝：{reason}\n"
        f"含义：{summary or permission.get('command', '')}\n{alt}"
        f"判定编号：{permission.get('decision_id', '')}\n"
        f"（系统提示：不要原样重发；请按替代建议调整，或改用其他方式完成目标。）"
    )


def no_channel_text(permission: dict[str, Any], summary: str = "") -> str:
    meaning = f"（{summary}）" if summary else ""
    return (
        f"该操作需要用户确认{meaning}，但当前是自主执行的子代理，无询问通道。\n"
        f"操作：`{permission.get('command', '')}`\n"
        f"判定编号：{permission.get('decision_id', '')}\n"
        f"（系统提示：请换用非拦截方式，或向父代理报告此操作。）"
    )


def judge_unavailable_text(permission: dict[str, Any], error: str) -> str:
    return (
        f"自动判定暂不可用（{error}），已按保守策略拒绝。\n"
        f"操作：`{permission.get('command', '')}`\n"
        f"判定编号：{permission.get('decision_id', '')}\n"
        f"（系统提示：可稍后重试相同操作，或改用其他方式完成目标。）"
    )


def user_reject_hint(answer: str, permission: dict[str, Any] | None) -> str:
    summary = str((permission or {}).get("summary") or "").strip()
    meaning = f"（{summary}）" if summary else ""
    return (
        f"{answer}\n\n"
        f"（系统提示：用户拒绝了该操作{meaning}。请改用其他方式完成目标，不要原样重发。）"
    )


def build_approved_actions_section(store: "PermissionStore | None", rules=None) -> str:
    """子代理任务消息中下放的已批准动作与已落盘规则段落。"""
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
                    verb = {"path:read": "read", "path:delete": "delete"}.get(rule["kind"], "write")
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


