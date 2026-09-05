"""PowerShell tool - execute commands via pwsh with background task support."""

from __future__ import annotations

import os
import re
from typing import Any

from core.tools.shared.base import Tool, ToolResult, _PWSH_PATH, _VENV_DIR, _VENV_SCRIPTS


# PowerShell 危险命令黑名单（大小写不敏感）
_DENY_PATTERNS = [
    (re.compile(r"\bRemove-Item\s+.*-[Rr]ecurse\s+.*-[Ff]orce\s+[A-Z]:\\", re.I),
     "Remove-Item -Recurse -Force on system drive (destructive recursive delete)"),
    (re.compile(r"\bFormat-Volume\b", re.I), "Format-Volume (disk format)"),
    (re.compile(r"\bClear-Disk\b", re.I), "Clear-Disk (disk wipe)"),
    (re.compile(r"\bInitialize-Disk\b", re.I), "Initialize-Disk (disk initialize)"),
    (re.compile(r"\bStop-Computer\b", re.I), "Stop-Computer (shutdown)"),
    (re.compile(r"\bRestart-Computer\b", re.I), "Restart-Computer (reboot)"),
    (re.compile(r"\bStop-Process\s+-Id\s+0\b", re.I), "Stop-Process -Id 0 (system process)"),
    # Also block legacy cmd-style dangerous commands
    (re.compile(r"\bformat\s+[a-zA-Z]:", re.I), "format (disk format)"),
    (re.compile(r"\bshutdown\b", re.I), "shutdown"),
    (re.compile(r"\breboot\b", re.I), "reboot"),
    # Cross-tool isolation: use bash/python tools instead of calling from pwsh
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:python3?|python\.exe)(?![a-zA-Z0-9_-])", re.I),
     "Python invocation from PowerShell (use the python tool instead)"),
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:bash|bash\.exe)(?![a-zA-Z0-9_-])", re.I),
     "Bash invocation from PowerShell (use the bash tool instead)"),
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:powershell|pwsh)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "PowerShell re-invocation (use native PowerShell commands)"),
]


class PwshTool(Tool):
    name = "pwsh"

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session_manager=None):
        super().__init__(cwd, workspace_uuid, session_manager)
        self.description = self._build_description()

    def _build_description(self) -> str:
        """Build tool description for the LLM."""
        return (
            "Execute a PowerShell command and return its output. "
            "Each call runs in a fresh pwsh process: no state (cwd, variables, functions) persists between calls. "
            "Paths use native Windows format (e.g., C:\\Users). "
            "Environment variables use $env:NAME syntax. "
            "Do NOT use pwsh to invoke Python (use the `python` tool) or bash (use the `bash` tool). "
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

        # Safety: deny dangerous commands
        deny_msg = self._check_deny_patterns(command)
        if deny_msg:
            return ToolResult(f"Error: command blocked by safety check — {deny_msg}", error=True)

        # Apply working_dir override (no path conversion needed for pwsh)
        if working_dir:
            resolved = os.path.abspath(self._resolve_path(working_dir))
            if not os.path.isdir(resolved):
                return ToolResult(f"Error: working_dir does not exist: {resolved}", error=True)
            command = f"Set-Location '{resolved}'; {command}"

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
    def _check_deny_patterns(command: str) -> str | None:
        """Check command against deny patterns. Returns reason if blocked, None if OK."""
        for pattern, reason in _DENY_PATTERNS:
            if pattern.search(command):
                return reason
        return None
