"""Bash tool - execute shell commands via Git Bash with background task support."""

from __future__ import annotations

import os
from typing import Any

from core.tools.shared.base import Tool, ToolResult, _GIT_BASH_PATH, _VENV_DIR, _VENV_SCRIPTS, _to_bash_path


class BashTool(Tool):
    name = "bash"

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session_manager=None):
        super().__init__(cwd, workspace_uuid, session_manager)
        # 动态注入当前工作目录到描述中
        self.description = self._build_description()

    def _build_description(self) -> str:
        """Build tool description with background task support."""
        return (
            "Execute a shell command via Git Bash and return its output. "
            "Commands run in the agent's working directory. "
            "Supports pipes, redirects, and all shell features. "
            "Use for: ls, git, npm, curl, system commands, file operations, etc. "
            "The agent's Python virtual environment is automatically activated (python/pip are in PATH). "
            f"Paths are in Windows format (e.g., {self.cwd}).\n\n"
            "## Background Tasks\n"
            "For long-running commands, use `run_in_background: true` to start the command in background. "
            "You will receive a `task_id` to manage the task:\n"
            "- `read_task(task_id)`: Read accumulated output (non-blocking)\n"
            "- `kill_task(task_id)`: Terminate the background task\n"
            "- `write_stdin(task_id, text)`: Send input to the running process (for interactive prompts)\n"
            "- `list_tasks()`: List all background tasks\n\n"
            "Example workflow:\n"
            "1. `bash(command=\"npm run build\", run_in_background=true)` → returns `task_id: bg-1`\n"
            "2. `bash(read_task=\"bg-1\")` → check progress\n"
            "3. Repeat step 2 until task completes\n\n"
            "Example: interactive prompts\n"
            "1. `bash(command=\"apt-get install foo\", run_in_background=true)`\n"
            "2. `bash(write_stdin={\"task_id\": \"bg-1\", \"text\": \"y\\n\"})` → answer prompt"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """Dynamic parameters schema."""
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute. Do NOT include 'timeout' in the command — use the timeout parameter instead.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 120, max: 600). Set this as a SEPARATE parameter, do not embed in command.",
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
            "required": [],  # All parameters are optional; execute() validates
        }

    DEFAULT_TIMEOUT = 120
    MAX_TIMEOUT = 600  # 最大超时 10 分钟

    def execute(
        self,
        command: str | None = None,
        timeout: int | None = None,
        run_in_background: bool | None = None,
        read_task: str | None = None,
        kill_task: str | None = None,
        write_stdin: dict | None = None,
        list_tasks: bool | None = None,
    ) -> ToolResult:
        """Execute bash command or manage background tasks."""

        # Handle background task operations
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

        timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        timeout = min(timeout, self.MAX_TIMEOUT)
        if timeout <= 0:
            return ToolResult("Error: timeout must be a positive integer", error=True)

        # Background mode
        if run_in_background:
            # Build environment prefix for venv
            env_prefix = ""
            if _VENV_DIR or _VENV_SCRIPTS:
                # Add both _VENV_DIR (python.exe) and _VENV_SCRIPTS (pip.exe) to PATH
                paths = []
                if _VENV_DIR:
                    paths.append(_to_bash_path(_VENV_DIR))
                if _VENV_SCRIPTS:
                    paths.append(_to_bash_path(_VENV_SCRIPTS))
                path_str = ":".join(paths)
                env_prefix = f"export PATH=\"{path_str}:$PATH\""

            return self._start_background_task(
                command,
                shell_path=_GIT_BASH_PATH,
                env_prefix=env_prefix,
            )

        # Foreground mode (original behavior)
        return self._run_bash(command, timeout=timeout)
