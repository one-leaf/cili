"""Shared base for shell command tools (bash / pwsh).

The two tools share dispatch, permission pipeline, timeout handling and background
management. Subclasses only provide shell-specific path normalization, cwd prefixing
and execution hooks.
"""

from __future__ import annotations

import os
import re
from typing import Any

from core.security.path_policy import OP_WRITE, PathTarget
from core.security.shell_paths import extract_shell_targets
from core.tools.base import Tool, ToolResult, _strip_shell_strings
from core.tools.permission import (
    META_KEY,
    denied_rule_text,
    no_channel_text,
    permission_decision_id,
    permission_placeholder_text,
)

# bash / pwsh 共用的静态证据模式（避免两份表逐渐漂移）
COMMON_DESTRUCTIVE_PATTERNS = [
    (re.compile(r"\bformat\s+[a-zA-Z]:", re.I), "system-destructive", "format (disk format)"),
    (re.compile(r"\bshutdown\b", re.I), "system-destructive", "shutdown"),
    (re.compile(r"\breboot\b", re.I), "system-destructive", "reboot"),
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:cmd|wsl)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "cross-tool-isolation", "cmd/WSL invocation (cross-tool isolation)"),
]


class ShellCommandTool(Tool):
    """Common implementation for bash/pwsh command tools."""

    shell_kind = "bash"
    _FINDING_PATTERNS: list = []
    _action_label = "shell command"
    _command_param_desc = (
        "The shell command to execute (action='run'). Do NOT include 'timeout' in the command "
        "— use the timeout parameter instead."
    )
    _working_dir_desc = "Override the working directory for this command. Defaults to the agent's cwd."

    DEFAULT_TIMEOUT = 120
    MAX_TIMEOUT = 600

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session=None,
                 permission_store=None, permission_rules=None, permission_policy=None,
                 **kwargs):
        super().__init__(
            cwd, workspace_uuid, session,
            permission_store=permission_store, permission_rules=permission_rules,
            permission_policy=permission_policy,
            **kwargs,
        )
        self.description = self._build_description()

    def _build_description(self) -> str:
        raise NotImplementedError

    @property
    def parameters(self) -> dict[str, Any]:
        """Dynamic parameters schema shared by bash/pwsh."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["run", "read", "kill", "write_stdin", "list"],
                    "description": (
                        "Operation to perform. Defaults to 'run' if omitted. "
                        f"'run': execute a {self._action_label}. "
                        "'read': read background task output (requires task_id). "
                        "'kill': terminate a background task (requires task_id). "
                        "'write_stdin': send input to a background task (requires task_id + text). "
                        "'list': list all background tasks."
                    ),
                },
                "command": {
                    "type": "string",
                    "description": self._command_param_desc,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 120, max: 600). Set this as a SEPARATE parameter, do not embed in command.",
                },
                "working_dir": {
                    "type": "string",
                    "description": self._working_dir_desc,
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "action='run' only. If true, run the command in background and return immediately "
                        "with a task_id. You will receive an automatic notification when it completes — no polling needed."
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Background task ID (e.g. 'bg-1'). Required for action='read'/'kill'/'write_stdin'."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Text to send to stdin. Required for action='write_stdin' (e.g. 'y\\n').",
                },
            },
            "required": [],  # All parameters are optional; execute() validates
        }

    def _resolve_working_dir(self, working_dir: str) -> str:
        raise NotImplementedError

    def _apply_cwd_prefix(self, command: str, initial_dir: str) -> str:
        raise NotImplementedError

    def _run_foreground(self, command: str, timeout: int) -> ToolResult:
        raise NotImplementedError

    def _run_background(self, command: str) -> ToolResult:
        raise NotImplementedError

    @classmethod
    def collect_command_findings(cls, command: str) -> list[dict]:
        """静态证据提取（原 deny/ask 黑名单转为判定证据，设计 §3.3.2）。

        返回全部命中项（不截断首个）：id 对应 constraints 条目，evidence 为人读描述。
        String literals are stripped first so that string data does not trigger
        findings; subexpressions that execute as code are preserved.
        """
        code = _strip_shell_strings(command, cls.shell_kind)
        findings: list[dict] = []
        for pattern, constraint_id, label in cls._FINDING_PATTERNS:
            if pattern.search(code):
                findings.append({"id": constraint_id, "evidence": label})
        return findings

    def execute(
        self,
        action: str | None = None,
        command: str | None = None,
        timeout: int | None = None,
        working_dir: str | None = None,
        run_in_background: bool | None = None,
        task_id: str | None = None,
        text: str | None = None,
    ) -> ToolResult:
        """Execute a shell command or manage background tasks."""
        effective_action = action or "run"

        if effective_action == "list":
            return self._list_background_tasks()
        if effective_action == "read":
            if not task_id:
                return ToolResult("Error: action='read' requires 'task_id' parameter (e.g. 'bg-1')", error=True)
            return self._read_background_task(task_id)
        if effective_action == "kill":
            if not task_id:
                return ToolResult("Error: action='kill' requires 'task_id' parameter (e.g. 'bg-1')", error=True)
            return self._kill_background_task(task_id)
        if effective_action == "write_stdin":
            if not task_id:
                return ToolResult("Error: action='write_stdin' requires 'task_id' and 'text' parameters", error=True)
            return self._write_stdin_to_task(task_id, text or "")

        if not command:
            return ToolResult("Error: command is required", error=True)

        # 青少年模式：回退类命令硬拒（开启且未解锁时）
        teen_block = self._teen_command_guard(command)
        if teen_block is not None:
            return teen_block

        initial_dir = self.cwd
        working_targets: list[PathTarget] = []
        did = permission_decision_id(command)

        # Apply working_dir override：目标折叠进统一权限占位（一次授权覆盖命令+越界目录）
        if working_dir:
            resolved = self._resolve_working_dir(working_dir)
            if not os.path.isdir(resolved):
                return ToolResult(f"Error: working_dir does not exist: {resolved}", error=True)
            initial_dir = resolved
            working_targets = [PathTarget(OP_WRITE, working_dir, "working_dir", resolved=resolved)]

        targets = extract_shell_targets(
            command, self.shell_kind, cwd=self.cwd, initial_dir=initial_dir,
        ) + working_targets
        findings = self.collect_command_findings(command)
        verdict = self._permission_policy().command_verdict(
            self.name, command, targets, findings, did,
        )
        if verdict.action != "allow":
            approval = self._pending_permission(
                command, did, targets, findings,
                reason=verdict.reason or "未命中允许规则，需要权限判定",
                mode=verdict.action, rule=verdict.rule,
            )
            if not self._permission_has_channel():
                if verdict.action == "deny":
                    return ToolResult(denied_rule_text(approval, verdict.rule), error=True)
                return ToolResult(no_channel_text(approval), error=True)
            return ToolResult(
                permission_placeholder_text(approval),
                completed=False,
                meta={META_KEY: approval},
            )

        if working_dir:
            command = self._apply_cwd_prefix(command, initial_dir)

        timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        timeout = min(timeout, self.MAX_TIMEOUT)
        if timeout <= 0:
            return ToolResult("Error: timeout must be a positive integer", error=True)

        if run_in_background:
            return self._run_background(command)
        return self._run_foreground(command, timeout)
