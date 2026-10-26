"""PowerShell tool - execute commands via pwsh with background task support."""

from __future__ import annotations

import os
import re
from typing import Any

from core.security.path_policy import OP_WRITE, PathTarget
from core.security.shell_paths import extract_shell_targets
from core.tools.permission import (
    META_KEY,
    denied_rule_text,
    no_channel_text,
    permission_decision_id,
    permission_placeholder_text,
)
from core.tools.base import (
    _VENV_DIR,
    _VENV_SCRIPTS,
    Tool,
    ToolResult,
    _strip_shell_strings,
)

# PowerShell 静态证据模式（原 ask/deny 黑名单转为判定证据，设计 §3.2.4 / §3.3.2）：
# 命中不再拒绝，而是作为 static_findings 交给 lite 判定器。
# 扫描前先剥掉字符串字面量（见 collect_command_findings），只扫代码部分。
#
# 递归强删规则：别名齐全（Remove-Item 及其别名 rm/ri/del/erase/rd/rmdir）、
# 参数顺序无关、支持参数缩写（-r / -rec / -fo 等）、目标覆盖盘符（正反斜杠）和 $env: 路径。
_DELETE_CMD = r"(?:Remove-Item|ri|rm|del|erase|rd|rmdir)"
_RECURSE_PARAM = r"-(?:r|re|rec|recu|recur|recurse)\b"
_FORCE_PARAM = r"-(?:fo|for|forc|force)\b"
_DRIVE_TARGET = r"(?:[A-Za-z]:[\\/]|\$env:)"
_RECURSIVE_FORCE_DELETE = re.compile(
    r"\b" + _DELETE_CMD + r"\b"
    r"(?=[^|;&]*" + _RECURSE_PARAM + r")"
    r"(?=[^|;&]*" + _FORCE_PARAM + r")"
    r"(?=[^|;&]*" + _DRIVE_TARGET + r")",
    re.I,
)

_FINDING_PATTERNS = [
    (_RECURSIVE_FORCE_DELETE,
     "system-destructive", "Remove-Item -Recurse -Force on drive path (destructive recursive delete)"),
    (re.compile(r"\bFormat-Volume\b", re.I), "system-destructive", "Format-Volume (disk format)"),
    (re.compile(r"\bClear-Disk\b", re.I), "system-destructive", "Clear-Disk (disk wipe)"),
    (re.compile(r"\bInitialize-Disk\b", re.I), "system-destructive", "Initialize-Disk (disk initialize)"),
    (re.compile(r"\bdiskpart\b", re.I), "system-destructive", "diskpart (disk management)"),
    (re.compile(r"\bStop-Computer\b", re.I), "system-destructive", "Stop-Computer (shutdown)"),
    (re.compile(r"\bRestart-Computer\b", re.I), "system-destructive", "Restart-Computer (reboot)"),
    # Also flag legacy cmd-style dangerous commands
    (re.compile(r"\bformat\s+[a-zA-Z]:", re.I), "system-destructive", "format (disk format)"),
    (re.compile(r"\bshutdown\b", re.I), "system-destructive", "shutdown"),
    (re.compile(r"\breboot\b", re.I), "system-destructive", "reboot"),
    (re.compile(r"\bStop-Process\s+-Id\s+0\b", re.I), "system-destructive", "Stop-Process -Id 0 (system process)"),
    # Code execution from string: the payload lives in a string literal that
    # string-stripping would hide from the scan, so flag the invocation itself
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:Invoke-Expression|iex)(?![a-zA-Z0-9_-])", re.I),
     "code-from-string", "Invoke-Expression (code execution from string — run the command directly)"),
    # Cross-tool isolation: use dedicated tools instead of calling from pwsh
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:bash|sh)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "cross-tool-isolation", "Bash invocation from PowerShell (use the bash tool instead)"),
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:cmd|wsl)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "cross-tool-isolation", "cmd/WSL invocation from PowerShell (cross-tool isolation)"),
    # UAC 提权（Start-Process powershell -Verb RunAs）是合法需求；嵌套 -Command 载荷
    # 不参与本扫描，判定器会提示用户确认完整命令
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:powershell|pwsh)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "cross-tool-isolation", "PowerShell re-invocation (nested -Command payload bypasses the safety scan; verify before approving)"),
]


class PwshTool(Tool):
    name = "pwsh"

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session=None,
                 permission_store=None, permission_rules=None, permission_policy=None,
                 approval_store=None, approval_rules=None):
        super().__init__(
            cwd, workspace_uuid, session,
            permission_store=permission_store, permission_rules=permission_rules,
            permission_policy=permission_policy,
            approval_store=approval_store, approval_rules=approval_rules,
        )
        self.description = self._build_description()

    def _build_description(self) -> str:
        """Build tool description for the LLM."""
        return (
            "Execute a PowerShell command and return its output. "
            "Each call runs in a fresh pwsh process — no state (cwd, variables, functions) persists between calls.\n"
            "Paths use native Windows format (e.g., C:\\Users); environment variables use $env:NAME.\n"
            "Do NOT use pwsh to invoke bash, cmd, or WSL — use the dedicated tools instead (cross-tool calls "
            "are flagged by static analysis and usually rejected by the safety judge).\n"
            "Commands that do not match a pre-approved rule go through an automatic safety judgment; "
            "if a command needs user confirmation it will be surfaced to the user. Do NOT reword a command "
            "just to dodge the judgment — follow the suggestion in the error instead.\n"
            f"Current working directory: {self.cwd}\n\n"
            "## Background tasks\n"
            "Long-running commands: action=\"run\", run_in_background=true → task_id. "
            "You will receive an automatic notification when it completes — no polling needed. "
            "Use action=\"read\"/\"kill\"/\"write_stdin\"/\"list\" to manage background tasks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """Parameters schema — mirrors bash tool interface."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["run", "read", "kill", "write_stdin", "list"],
                    "description": (
                        "Operation to perform. Defaults to 'run' if omitted. "
                        "'run': execute a PowerShell command. "
                        "'read': read background task output (requires task_id). "
                        "'kill': terminate a background task (requires task_id). "
                        "'write_stdin': send input to a background task (requires task_id + text). "
                        "'list': list all background tasks."
                    ),
                },
                "command": {
                    "type": "string",
                    "description": "The PowerShell command to execute (action='run'). Do NOT include timeout in the command — use the timeout parameter instead.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 120, max: 600). Set this as a SEPARATE parameter, do not embed in command.",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Override the working directory for this command. Defaults to the agent's cwd. Use Windows paths (e.g., C:\\Projects).",
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
            "required": [],
        }

    DEFAULT_TIMEOUT = 120
    MAX_TIMEOUT = 600

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
        """Execute PowerShell command or manage background tasks.

        Dispatch logic:
        - action='run' (or omitted): execute a PowerShell command (default)
        - action='read': read background task output (requires task_id)
        - action='kill': terminate a background task (requires task_id)
        - action='write_stdin': send input to a background task (requires task_id + text)
        - action='list': list all background tasks
        """
        effective_action = action or "run"

        # Handle background task operations (shared with bash)
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

        # Regular command execution
        if not command:
            return ToolResult("Error: command is required", error=True)

        # 青少年模式：回退类命令硬拒（开启且未解锁时）
        teen_block = self._teen_command_guard(command)
        if teen_block is not None:
            return teen_block

        policy = self._path_policy()
        initial_dir = self.cwd
        working_targets: list[PathTarget] = []
        did = permission_decision_id(command)

        # Apply working_dir override：目标折叠进统一审批占位（一次授权覆盖命令+越界目录），
        # Set-Location 前缀在批准后、真正执行前才拼接
        if working_dir:
            resolved = policy.resolve(working_dir)
            if not os.path.isdir(resolved):
                return ToolResult(f"Error: working_dir does not exist: {resolved}", error=True)
            initial_dir = resolved
            working_targets = [PathTarget(OP_WRITE, working_dir, "working_dir", resolved=resolved)]

        # ── 权限管道：命令级裁决 → 统一占位 / 直接执行 ──
        targets = extract_shell_targets(command, "pwsh", cwd=self.cwd, initial_dir=initial_dir) + working_targets
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
            # 目录名可能含单引号，pwsh 单引号内用 '' 转义
            escaped_dir = initial_dir.replace("'", "''")
            command = f"Set-Location '{escaped_dir}'; " + command

        timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        timeout = min(timeout, self.MAX_TIMEOUT)
        if timeout <= 0:
            return ToolResult("Error: timeout must be a positive integer", error=True)

        # Background mode
        if run_in_background:
            # Build env prefix for venv (PowerShell syntax)
            env_prefix = ""
            if _VENV_DIR or _VENV_SCRIPTS:
                paths = []
                if _VENV_DIR:
                    paths.append(_VENV_DIR)
                if _VENV_SCRIPTS:
                    paths.append(_VENV_SCRIPTS)
                path_str = ";".join(paths)
                env_prefix = f'$env:PATH = "{path_str};$env:PATH"'

            return self._start_pwsh_background_task(command, env_prefix=env_prefix)

        # Foreground mode
        return self._run_pwsh(command, timeout=timeout)

    @staticmethod
    def collect_command_findings(command: str) -> list[dict]:
        """静态证据提取（原 deny/ask 黑名单转为判定证据，设计 §3.3.2）。

        返回全部命中项：id 对应 constraints 条目，evidence 为人读描述。
        String literals are stripped first so that string data (e.g. Write-Output "pwsh
        works") does not trigger findings; subexpressions inside double quotes
        ($( ... )) are preserved because they execute as code.
        """
        code = _strip_shell_strings(command, "pwsh")
        findings: list[dict] = []
        for pattern, constraint_id, label in _FINDING_PATTERNS:
            if pattern.search(code):
                findings.append({"id": constraint_id, "evidence": label})
        return findings
