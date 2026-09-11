"""PowerShell tool - execute commands via pwsh with background task support."""

from __future__ import annotations

import os
import re
from typing import Any

from core.tools.approval import (
    META_KEY,
    MODE_ASK,
    MODE_DENY,
    approval_decision_id,
    approval_placeholder_text,
)
from core.tools.base import (
    Tool,
    ToolResult,
    _VENV_DIR,
    _VENV_SCRIPTS,
    _strip_shell_strings,
)


# PowerShell 危险命令黑名单（大小写不敏感）
# 扫描前先剥掉字符串字面量（见 _check_deny_patterns），只扫代码部分。
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

_DENY_PATTERNS = [
    # --- ask：破坏性操作，可经用户批准后在本次会话内执行 ---
    (_RECURSIVE_FORCE_DELETE,
     "Remove-Item -Recurse -Force on drive path (destructive recursive delete)", MODE_ASK),
    (re.compile(r"\bFormat-Volume\b", re.I), "Format-Volume (disk format)", MODE_ASK),
    (re.compile(r"\bClear-Disk\b", re.I), "Clear-Disk (disk wipe)", MODE_ASK),
    (re.compile(r"\bInitialize-Disk\b", re.I), "Initialize-Disk (disk initialize)", MODE_ASK),
    (re.compile(r"\bdiskpart\b", re.I), "diskpart (disk management)", MODE_ASK),
    (re.compile(r"\bStop-Computer\b", re.I), "Stop-Computer (shutdown)", MODE_ASK),
    (re.compile(r"\bRestart-Computer\b", re.I), "Restart-Computer (reboot)", MODE_ASK),
    # Also block legacy cmd-style dangerous commands
    (re.compile(r"\bformat\s+[a-zA-Z]:", re.I), "format (disk format)", MODE_ASK),
    (re.compile(r"\bshutdown\b", re.I), "shutdown", MODE_ASK),
    (re.compile(r"\breboot\b", re.I), "reboot", MODE_ASK),
    # --- deny：架构性/代码执行，问用户无意义，保持硬拒绝 ---
    (re.compile(r"\bStop-Process\s+-Id\s+0\b", re.I), "Stop-Process -Id 0 (system process)", MODE_DENY),
    # Code execution from string: the payload lives in a string literal that
    # string-stripping would hide from the scan, so block the invocation itself
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:Invoke-Expression|iex)(?![a-zA-Z0-9_-])", re.I),
     "Invoke-Expression (code execution from string — run the command directly)", MODE_DENY),
    # Cross-tool isolation: use dedicated tools instead of calling from pwsh
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:python3?|pythonw?|py)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "Python invocation from PowerShell (use the python tool instead)", MODE_DENY),
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:bash|sh)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "Bash invocation from PowerShell (use the bash tool instead)", MODE_DENY),
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:cmd|wsl)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "cmd/WSL invocation from PowerShell (cross-tool isolation)", MODE_DENY),
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:powershell|pwsh)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "PowerShell re-invocation (use native PowerShell commands)", MODE_DENY),
]


class PwshTool(Tool):
    name = "pwsh"

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session_manager=None,
                 approval_store=None):
        super().__init__(cwd, workspace_uuid, session_manager, approval_store=approval_store)
        self.description = self._build_description()

    def _build_description(self) -> str:
        """Build tool description for the LLM."""
        return (
            "Execute a PowerShell command and return its output. "
            "Each call runs in a fresh pwsh process: no state (cwd, variables, functions) persists between calls. "
            "Paths use native Windows format (e.g., C:\\Users). "
            "Environment variables use $env:NAME syntax. "
            "Do NOT use pwsh to invoke Python (use the `python` tool), bash (use the `bash` tool), "
            "cmd, or WSL — cross-tool invocations are blocked. "
            "Some destructive commands (e.g. Remove-Item -Recurse -Force, disk operations) require user approval "
            "before execution — if blocked for approval, wait for the user's decision then retry the exact command.\n"
            f"Current working directory: {self.cwd}\n\n"
            "## Background Tasks\n"
            "For long-running commands, use `run_in_background: true` to start the command in background. "
            "You will receive a `task_id` to manage the task:\n"
            "- `read_task(task_id)`: Read accumulated output (non-blocking)\n"
            "- `kill_task(task_id)`: Terminate the background task\n"
            "- `write_stdin(task_id, text)`: Send input to the running process (for interactive prompts)\n"
            "- `list_tasks()`: List all background tasks\n\n"
            "Example workflow:\n"
            "1. `pwsh(command=\"Get-Process\", run_in_background=true)` → returns `task_id: bg-1`\n"
            "2. `pwsh(read_task=\"bg-1\")` → check progress\n"
            "3. Repeat step 2 until task completes"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """Parameters schema — mirrors bash tool interface."""
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The PowerShell command to execute. Do NOT include timeout in the command — use the timeout parameter instead.",
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
                        "If true, run the command in background and return immediately "
                        "with a task_id. Use read_task/kill_task/write_stdin to manage it."
                    ),
                },
                "read_task": {
                    "type": "string",
                    "description": (
                        "Task ID to read output from (e.g., 'bg-1'). "
                        "Returns accumulated output since last read. Non-blocking."
                    ),
                },
                "kill_task": {
                    "type": "string",
                    "description": "Task ID to terminate (e.g., 'bg-1').",
                },
                "write_stdin": {
                    "type": "object",
                    "description": "Send input to a running background task.",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "Task ID to write to (e.g., 'bg-1').",
                        },
                        "text": {
                            "type": "string",
                            "description": "Text to send to stdin (e.g., 'y\\n').",
                        },
                    },
                    "required": ["task_id", "text"],
                },
                "list_tasks": {
                    "type": "boolean",
                    "description": "If true, list all background tasks and their status.",
                },
            },
            "required": [],
        }

    DEFAULT_TIMEOUT = 120
    MAX_TIMEOUT = 600

    def execute(
        self,
        command: str | None = None,
        timeout: int | None = None,
        working_dir: str | None = None,
        run_in_background: bool | None = None,
        read_task: str | None = None,
        kill_task: str | None = None,
        write_stdin: dict | None = None,
        list_tasks: bool | None = None,
    ) -> ToolResult:
        """Execute PowerShell command or manage background tasks."""

        # Handle background task operations (shared with bash)
        if list_tasks:
            return self._list_background_tasks()
        if read_task:
            return self._read_background_task(read_task)
        if kill_task:
            return self._kill_background_task(kill_task)
        if write_stdin:
            task_id = write_stdin.get("task_id")
            text = write_stdin.get("text", "")
            return self._write_stdin_to_task(task_id, text)

        # Regular command execution
        if not command:
            return ToolResult("Error: command is required", error=True)

        # Safety: deny dangerous commands (ask 档先查会话级批准，未批准则等待用户确认)
        deny = self._check_deny_patterns(command)
        if deny:
            mode, reason = deny
            if mode == MODE_DENY or not self.approval_store:
                return ToolResult(f"Error: command blocked by safety check — {reason}", error=True)
            approval = {
                "decision_id": approval_decision_id(command),
                "command": command,
                "reason": reason,
            }
            if self.approval_store.is_approved(approval["decision_id"]):
                pass  # 本次会话已批准，放行执行
            else:
                return ToolResult(
                    approval_placeholder_text(approval),
                    completed=False,
                    meta={META_KEY: approval},
                )

        # Apply working_dir override (no path conversion needed for pwsh)
        if working_dir:
            resolved = os.path.abspath(self._resolve_path(working_dir))
            if not os.path.isdir(resolved):
                return ToolResult(f"Error: working_dir does not exist: {resolved}", error=True)
            # 目录名可能含单引号，pwsh 单引号内用 '' 转义；注入段参与 deny 扫描
            escaped_dir = resolved.replace("'", "''")
            cd_prefix = f"Set-Location '{escaped_dir}'; "
            deny_prefix = self._check_deny_patterns(cd_prefix + command)
            if deny_prefix:
                mode, reason = deny_prefix
                return ToolResult(
                    f"Error: command blocked by safety check — {reason}", error=True
                )
            command = cd_prefix + command

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
    def _check_deny_patterns(command: str) -> tuple[str, str] | None:
        """Check command against deny patterns. Returns (mode, reason) if blocked, None if OK.

        mode ∈ {"ask", "deny"}: ask 表示破坏性操作可经用户批准后执行；deny 表示硬拒绝。
        String literals are stripped first so that string data (e.g. Write-Output "pwsh
        works") does not trigger keyword rules; subexpressions inside double quotes
        ($( ... )) are preserved because they execute as code.
        """
        code = _strip_shell_strings(command, "pwsh")
        for pattern, reason, mode in _DENY_PATTERNS:
            if pattern.search(code):
                return mode, reason
        return None
