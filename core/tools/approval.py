"""高风险命令的会话级审批共享模块。

三档拦截（allow/ask/deny）中的 ask 档：命令命中后不直接拒绝，
先查 ApprovalStore 是否已在本会话内被用户批准；未批准则返回
completed=False 占位符，由 Master Agent 循环合成 ask_user 卡询问用户。

ApprovalStore 由 Master Agent 持有，Master/Worker/Lite 的 bash/pwsh 与
agent 委派共享同一实例：Master 批准的会话级命令在 Worker/Lite 中同样
放行。会话级批准仅存内存，服务器重启即失效；用户选择「允许并记住」
时规则持久化到 workspace 的 approvals.json（Master 启动时回灌）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from core.fs_utils import atomic_write_json, load_json_or_backup

MODE_ASK = "ask"
MODE_DENY = "deny"

APPROVE_LABEL = "允许本次会话"
REMEMBER_LABEL = "允许并记住"
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
    """构造发给用户的批准问题文案。

    按 kind 分支：browser:navigate 为非公网地址导航；path:write/path:delete 为
    工作区外文件操作；其余为高风险命令。
    """
    kind = approval.get("kind", "command")
    target_label = "路径" if kind.startswith("path:") else "命令"
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
            f"检测到高风险命令，需要您批准后才能执行：\n\n"
            f"命令：`{approval['command']}`\n"
        )
    return (
        f"{head}"
        f"拦截原因：{approval['reason']}\n"
        f"判定编号：{approval['decision_id']}\n\n"
        f"「{APPROVE_LABEL}」仅本次会话放行；「{REMEMBER_LABEL}」会写入此工作区，"
        f"重启后仍放行相同{target_label}（包括委派给子代理）；「{REJECT_LABEL}」仅拦截本次执行，"
        f"下次遇到相同{target_label}仍会询问。"
    )


def approval_placeholder_text(approval: dict[str, Any]) -> str:
    """工具返回给 LLM 的占位文本（completed=False，等待用户批准）。"""
    kind = approval.get("kind", "command")
    reason = approval.get("reason", "").lower()
    command = approval.get("command", "")

    # 1. 检测 bash/pwsh 中调用 python 的情况
    if "python tool" in reason:
        return (
            "Error: 不能在 bash/pwsh 中调用 Python。\n"
            "原因：该命令需要用户批准，但子代理无法等待用户确认。\n"
            "解决：直接使用 `python` tool 执行 Python 代码。\n\n"
            "示例：\n"
            "- 错误：`bash(command=\"python script.py\")`\n"
            "- 正确：`python(action=\"execute_file\", file=\"script.py\")`"
        )

    # 2. 检测跨工具调用（bash调pwsh、pwsh调bash等）
    if "cross-tool" in reason or ("use the" in reason and "tool instead" in reason):
        return (
            f"Error: 跨工具调用被禁止。\n"
            f"原因：{approval.get('reason', '')}\n"
            f"命令：`{command}`\n\n"
            f"解决：请使用对应的专用工具，不要在 shell 中调用其他工具。"
        )

    # 3. 路径越界操作
    if kind.startswith("path:"):
        op = "删除" if kind == "path:delete" else "写入"
        return (
            f"Error: 该操作会{op}工作区外的文件，需要用户批准。\n"
            f"拦截原因：{approval.get('reason', '目标路径不在工作区内')}\n"
            f"操作目标：`{approval.get('path', approval.get('command', ''))}`\n\n"
            f"如果你是子代理（worker/lite），无法等待用户确认，请：\n"
            f"1. 改用工作区内的路径\n"
            f"2. 或向父代理报告，让父代理处理此操作"
        )

    # 4. 浏览器导航（非公网地址）
    if kind == "browser:navigate":
        return (
            f"Error: 该导航地址被 SSRF 防护拦截（非公网地址），需要用户批准。\n"
            f"地址：`{command}`\n\n"
            f"如果你是子代理（worker/lite），无法等待用户确认，请：\n"
            f"1. 确认地址是否正确（应使用公网地址）\n"
            f"2. 或向父代理报告，让父代理处理此导航"
        )

    # 5. 通用高风险命令
    return (
        f"Error: 该命令被安全策略拦截，需要用户批准。\n"
        f"拦截原因：{approval.get('reason', '高风险操作')}\n"
        f"命令：`{command}`\n\n"
        f"如果你是子代理（worker/lite），无法等待用户确认，请：\n"
        f"1. 尝试用更安全的方式完成相同任务\n"
        f"2. 或向父代理报告，让父代理处理此操作"
    )


def build_approved_commands_section(approval_store: "ApprovalStore | None") -> str:
    """子代理任务消息中下放的已批准命令/导航/路径段落（无批准时返回空串）。

    命令、导航 URL 与路径规则分列：worker/lite 据此得知主会话已放行的命令、
    非公网导航与工作区外文件操作，避免重复尝试被拒（放行本身由共享 store
    的 is_approved 保证，本段仅作提示）。
    """
    if not approval_store:
        return ""
    approved = approval_store.approved_commands()
    navigations = approval_store.approved_navigations()
    path_rules = approval_store.approved_path_rules()
    if not approved and not navigations and not path_rules:
        return ""
    lines = ["### Pre-approved commands", ""]
    if approved:
        lines.append(
            "The following commands have been approved by the user for this "
            "session and may be executed directly:"
        )
        lines += [f"- `{cmd}`" for cmd in approved]
    if navigations:
        lines.append(
            "The following navigation URLs have been approved by the user for "
            "this session and may be navigated to directly:"
        )
        lines += [f"- navigate `{url}`" for url in navigations]
    if path_rules:
        lines.append(
            "The following file operations have been approved by the user for "
            "this session and may be executed directly:"
        )
        for rule in path_rules:
            verb = "delete" if rule["kind"] == "path:delete" else "write"
            lines.append(f"- {verb} `{rule['command']}`")
    lines.append("")
    return "\n".join(lines)


class ApprovalStore:
    """高风险命令审批存储：会话级（内存）+ 可选持久化到 workspace。

    - _approved: decision_id -> command，会话内持续生效，不按次数消费；
      含从 rules_path 回灌的持久化规则
    - pending: 单槽待批准项 {decision_id, command, reason}，由 root 循环写入、
      answer 端点读取后清空
    - rules_path: 持久化规则文件（workspace 的 approvals.json）。为 None 时
      退化为纯内存存储（现有测试/独立调用零改动）。仅 Master 写入文件，
      Worker/Lite 通过共享实例继承。
    """

    def __init__(self, rules_path: Path | str | None = None) -> None:
        self._approved: dict[str, dict[str, str]] = {}
        self.pending: dict[str, Any] | None = None
        self._rules_path = Path(rules_path) if rules_path is not None else None
        self._load_rules()

    def _load_rules(self) -> None:
        """启动时从 rules_path 回灌持久化规则（文件缺失/损坏时静默跳过）。

        旧规则无 kind 字段，按 "command" 回灌（兼容既有 approvals.json）。
        """
        if self._rules_path is None:
            return
        data = load_json_or_backup(self._rules_path, {})
        for rule in data.get("rules", []):
            did = rule.get("decision_id")
            command = rule.get("command")
            if did and command:
                kind = rule.get("kind", "command")
                self._approved[did] = {"command": command, "kind": kind}

    def is_approved(self, decision_id: str) -> bool:
        return decision_id in self._approved

    def approve(self, decision_id: str, command: str, persist: bool = False,
                reason: str = "", kind: str = "command") -> None:
        """记录批准。persist=True 时把规则原子写入 rules_path（按 decision_id 去重）。

        kind 默认 "command"，现有调用点零改动；路径规则传 "path:write"/"path:delete"。
        """
        self._approved[decision_id] = {"command": command, "kind": kind}
        if persist:
            self._persist_rule(decision_id, command, reason, kind)

    def _persist_rule(self, decision_id: str, command: str, reason: str, kind: str) -> None:
        if self._rules_path is None:
            return
        data = load_json_or_backup(self._rules_path, {"rules": []})
        rules = [r for r in data.get("rules", []) if r.get("decision_id") != decision_id]
        rules.append({
            "decision_id": decision_id,
            "command": command,
            "reason": reason or "",
            "kind": kind,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        data["rules"] = rules
        atomic_write_json(self._rules_path, data)

    def approved_commands(self) -> list[str]:
        """仅命令类规则（kind == "command"），供现有调用/测试保持语义。"""
        return [v["command"] for v in self._approved.values() if v["kind"] == "command"]

    def approved_navigations(self) -> list[str]:
        """导航类规则（kind == "browser:navigate"），供子代理提示段落。"""
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
