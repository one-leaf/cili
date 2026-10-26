"""权限文案、会话批准存储与审计。

- ``PermissionStore``：会话级批准（decision_id → {command, kind}，批准不消费）；
- 文案：卡片问题、占位文本、黑名单/判定/无通道拒绝文本；
- 审计：``{session_dir}/approval_audit.jsonl``。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

META_KEY = "permission_required"

APPROVE_LABEL = "允许本次会话"
REMEMBER_LABEL = "一直允许（此类操作）"
REJECT_LABEL = "拒绝"

_RISK_LABELS = {"low": "低", "medium": "中", "high": "高"}


def canonicalize_command(command: str) -> str:
    """仅折叠连续空白，生成稳定 decision_id。"""
    return " ".join((command or "").split())


def permission_decision_id(command: str) -> str:
    return hashlib.sha256(canonicalize_command(command).encode("utf-8")).hexdigest()[:16]


def _risk_label(risk: str) -> str:
    return _RISK_LABELS.get(risk, "中")


def _target_label(kind: str) -> str:
    if kind == "browser:navigate":
        return "地址"
    if kind == "path:read":
        return "路径"
    if kind.startswith("path:"):
        return "路径"
    return "操作"


def _kind_action(kind: str) -> str:
    if kind == "browser:navigate":
        return "导航"
    if kind == "path:read":
        return "读取"
    if kind == "path:delete":
        return "删除"
    if kind.startswith("path:"):
        return "写入"
    return "执行"


def build_permission_question(permission: dict[str, Any]) -> str:
    """构造权限确认卡文案（含含义/风险/理由）。"""
    kind = permission.get("kind", "command")
    summary = str(permission.get("summary") or "").strip()
    risk = str(permission.get("risk") or "").strip()
    reason = str(permission.get("reason") or "").strip()
    display = str(permission.get("rule_display") or "").strip()

    if kind == "browser:navigate":
        head = (
            f"检测到非公网地址导航，需要您批准后才能执行：\n\n"
            f"地址：`{permission['command']}`\n"
        )
    elif kind == "path:read":
        head = (
            f"检测到工作区外文件读取，需要您批准后才能执行：\n\n"
            f"操作：读取 `{permission['command']}`\n"
        )
    elif kind == "path:delete":
        head = (
            f"检测到工作区外文件操作，需要您批准后才能执行：\n\n"
            f"操作：删除 `{permission['command']}`\n"
        )
    elif kind.startswith("path:"):
        head = (
            f"检测到工作区外文件操作，需要您批准后才能执行：\n\n"
            f"操作：写入 `{permission['command']}`\n"
        )
    else:
        head = (
            f"检测到需要确认的操作，需要您批准后才能执行：\n\n"
            f"操作：`{permission['command']}`\n"
        )

    if summary:
        head += f"含义：{summary}\n"
    if risk:
        head += f"风险：{_risk_label(risk)}\n"
    if reason:
        head += f"判定理由：{reason}\n"
    head += f"判定编号：{permission['decision_id']}\n"

    target_label = _target_label(kind)
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
    return (
        f"Error: 该{target_label}未命中允许规则，已被拦截，等待权限判定（自动判定或用户确认）。\n"
        f"{target_label}：`{permission.get('command', '')}`\n"
        f"判定编号：{permission.get('decision_id', '')}\n"
        f"拦截原因：{permission.get('reason', '')}\n\n"
        f"请勿原样重发；等待判定结果或用户决定。\n"
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


def append_permission_audit(session_dir, event: str, data: dict[str, Any]) -> None:
    """审计事件追加到 ``{session_dir}/approval_audit.jsonl``；失败静默。"""
    try:
        if not session_dir:
            return
        d = Path(session_dir)
        d.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "event": event, **data}
        with open(d / "approval_audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        logger.debug("[permission-audit] 写审计事件失败", exc_info=True)


class PermissionStore:
    """会话级批准存储：decision_id → {command, kind}，批准不消费；单槽 pending。"""

    def __init__(self) -> None:
        self._approved: dict[str, dict[str, str]] = {}
        self.pending: dict[str, Any] | None = None

    def is_approved(self, decision_id: str) -> bool:
        return decision_id in self._approved

    def approve(self, decision_id: str, command: str, kind: str = "command") -> None:
        self._approved[decision_id] = {"command": command, "kind": kind}

    def approved_commands(self) -> list[str]:
        return [v["command"] for v in self._approved.values() if v["kind"] == "command"]

    def approved_navigations(self) -> list[str]:
        return [v["command"] for v in self._approved.values() if v["kind"] == "browser:navigate"]

    def approved_path_rules(self) -> list[dict[str, str]]:
        return [
            {"kind": v["kind"], "command": v["command"]}
            for v in self._approved.values() if v["kind"].startswith("path:")
        ]

    def set_pending(self, permission: dict[str, Any]) -> None:
        self.pending = permission

    def clear_pending(self) -> None:
        self.pending = None
