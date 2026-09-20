"""Bash tool - execute shell commands via Git Bash with background task support."""

from __future__ import annotations

import os
import re
from typing import Any

from core.security.path_policy import OP_WRITE, PathTarget
from core.security.shell_paths import extract_shell_targets
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
    _GIT_BASH_PATH,
    _VENV_DIR,
    _VENV_SCRIPTS,
    _strip_shell_strings,
    _to_bash_path,
)


# 危险命令黑名单（大小写不敏感），三档：ask（破坏性操作，询问用户）/ deny（硬拒绝）
# 扫描前先剥掉字符串字面量（见 _check_deny_patterns），只扫代码部分；
# 双引号内的 $(...) 和 `...` 子表达式会执行，保留参与扫描。
_DENY_PATTERNS = [
    # --- ask：破坏性操作，可经用户批准后在本次会话内执行 ---
    (re.compile(r"\brm\s+(-\w+\s+)*-[rf]+\s+/", re.I),        "rm -rf / (destructive recursive delete)", MODE_ASK),
    (re.compile(r"\brm\s+(-\w+\s+)*-[rf]+\s+\*", re.I),       "rm -rf * (destructive wildcard delete)", MODE_ASK),
    (re.compile(r"\brm\s+(-\w+\s+)*-[rf]+\s+~", re.I),        "rm -rf ~ (destructive home dir delete)", MODE_ASK),
    (re.compile(r"\bformat\s+[a-zA-Z]:", re.I),                "format (disk format)", MODE_ASK),
    (re.compile(r"\bdd\s+.*\bof=/dev/", re.I),                 "dd of=/dev/ (device write)", MODE_ASK),
    (re.compile(r"\bmkfs\b", re.I),                            "mkfs (filesystem format)", MODE_ASK),
    (re.compile(r"\bshutdown\b", re.I),                        "shutdown", MODE_ASK),
    (re.compile(r"\breboot\b", re.I),                          "reboot", MODE_ASK),
    (re.compile(r":\(\)\s*\{", re.I),                          "fork bomb", MODE_ASK),
    (re.compile(r"\bcd\s+\.\.\s*&&\s*rm\s", re.I),             "cd .. && rm (parent dir delete)", MODE_ASK),
    (re.compile(r">\s*/dev/sd[a-z]", re.I),                    "redirect to disk device", MODE_ASK),
    # --- deny：架构性/代码执行，问用户无意义，保持硬拒绝 ---
    # Code execution from string: the payload lives in a string literal that
    # string-stripping would hide from the scan, so block the invocation itself
    (re.compile(r"(?<![a-zA-Z0-9_-])eval\b", re.I),
     "eval (code execution from string — run the command directly)", MODE_DENY),
    # Cross-tool isolation: use pwsh/python tools instead of calling from bash
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:powershell|pwsh)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "PowerShell invocation from bash (use the pwsh tool instead)", MODE_DENY),
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:python3?|pythonw?)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "Python invocation from bash bypasses the python tool's safety checks (use the python tool instead)", MODE_ASK),
    (re.compile(r"(?<![a-zA-Z0-9_\\.-])py(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "py launcher invocation from bash (use the python tool instead)", MODE_DENY),
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:cmd|wsl)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "cmd/WSL invocation from bash (cross-tool isolation)", MODE_DENY),
]


class BashTool(Tool):
    name = "bash"

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session_manager=None,
                 approval_store=None):
        super().__init__(cwd, workspace_uuid, session_manager, approval_store=approval_store)
        # 动态注入当前工作目录到描述中
        self.description = self._build_description()

    def _build_description(self) -> str:
        """Build tool description with background task support."""
        return (
            "Execute a shell command via Git Bash and return its output. "
            "Commands run in the agent's working directory. Supports pipes, redirects, and all shell features.\n"
            "Use for: ls, git, npm, curl, system commands, file operations, etc.\n"
            "Do NOT use bash to invoke PowerShell (use `pwsh`) — cross-tool calls are blocked.\n"
            "Some destructive commands (e.g. rm -rf, format, shutdown) and Python invocation from bash "
            "require user approval; if blocked, wait for the user's decision then retry the exact command.\n"
            f"Paths are in Windows format (e.g., {self.cwd}).\n\n"
            "## Background tasks\n"
            "Long-running commands: action=\"run\", run_in_background=true → task_id. "
            "You will receive an automatic notification when it completes — no polling needed. "
            "Use action=\"read\"/\"kill\"/\"write_stdin\"/\"list\" to manage background tasks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """Dynamic parameters schema."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["run", "read", "kill", "write_stdin", "list"],
                    "description": (
                        "Operation to perform. Defaults to 'run' if omitted. "
                        "'run': execute a shell command. "
                        "'read': read background task output (requires task_id). "
                        "'kill': terminate a background task (requires task_id). "
                        "'write_stdin': send input to a background task (requires task_id + text). "
                        "'list': list all background tasks."
                    ),
                },
                "command": {
                    "type": "string",
                    "description": "The shell command to execute (action='run'). Do NOT include 'timeout' in the command — use the timeout parameter instead.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 120, max: 600). Set this as a SEPARATE parameter, do not embed in command.",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Override the working directory for this command. Defaults to the agent's cwd.",
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

    DEFAULT_TIMEOUT = 120
    MAX_TIMEOUT = 600  # 最大超时 10 分钟

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
        """Execute bash command or manage background tasks.

        Dispatch logic:
        - action='run' (or omitted): execute a shell command (default)
        - action='read': read background task output (requires task_id)
        - action='kill': terminate a background task (requires task_id)
        - action='write_stdin': send input to a background task (requires task_id + text)
        - action='list': list all background tasks
        """
        effective_action = action or "run"

        # Handle background task operations
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

        # Safety: deny dangerous commands (ask 档先查会话级批准，未批准则等待用户确认)
        deny = self._check_deny_patterns(command)
        keyword_approved = False
        if deny:
            mode, reason = deny
            if mode == MODE_DENY or not self.approval_store:
                return ToolResult(f"Error: command blocked by safety check — {reason}", error=True)
            approval = {
                "decision_id": approval_decision_id(command),
                "command": command,
                "reason": reason,
            }
            if not self.approval_store.is_approved(approval["decision_id"]):
                return ToolResult(
                    approval_placeholder_text(approval),
                    completed=False,
                    meta={META_KEY: approval},
                )
            keyword_approved = True  # 整条命令已批准，跳过路径门

        policy = self._path_policy()
        initial_dir = self.cwd

        # Apply working_dir override
        if working_dir:
            resolved = policy.resolve(working_dir)
            # 区外 working_dir 也需审批/拒绝
            gate = self._path_gate([PathTarget(OP_WRITE, working_dir, "working_dir", resolved=resolved)])
            if gate:
                return gate
            if not os.path.isdir(resolved):
                return ToolResult(f"Error: working_dir does not exist: {resolved}", error=True)
            bash_dir = _to_bash_path(resolved)
            # 目录名可能含引号，shell_escape 防止注入出引号；注入段参与 deny 扫描
            cd_prefix = f"cd {self._shell_escape(bash_dir)} && "
            deny_prefix = self._check_deny_patterns(cd_prefix + command)
            if deny_prefix:
                mode, reason = deny_prefix
                return ToolResult(
                    f"Error: command blocked by safety check — {reason}", error=True
                )
            command = cd_prefix + command
            initial_dir = resolved

        # 路径权限门：静态提取写/删目标，越界需审批（keyword_approved 时跳过）
        if not keyword_approved:
            targets = extract_shell_targets(command, "bash", cwd=self.cwd, initial_dir=initial_dir)
            gate = self._path_gate(targets)
            if gate:
                return gate

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

    @staticmethod
    def _check_deny_patterns(command: str) -> tuple[str, str] | None:
        """Check command against deny patterns. Returns (mode, reason) if blocked, None if OK.

        mode ∈ {"ask", "deny"}: ask 表示破坏性操作可经用户批准后执行；deny 表示硬拒绝。
        String literals are stripped first so that string data (e.g. echo "pwsh works")
        does not trigger keyword rules; subexpressions that execute as code ($( ... )
        and ` ... `) are preserved.
        """
        code = _strip_shell_strings(command, "bash")
        for pattern, reason, mode in _DENY_PATTERNS:
            if pattern.search(code):
                return mode, reason
        return None
