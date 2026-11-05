"""Bash tool - execute shell commands via Git Bash with background task support."""

from __future__ import annotations

import re

from core.security.shell_paths import normalize_shell_path
from core.tools.base import _GIT_BASH_PATH, ToolResult, _to_bash_path
from core.tools.shell_command import ShellCommandTool

# 静态证据模式（原 ask/deny 黑名单转为判定证据，设计 §3.2.4 / §3.3.2）：
# 命中不再拒绝，而是作为 static_findings 交给 lite 判定器，由判定器结合上下文
# 决定 allow / ask / deny。
# 扫描前先剥掉字符串字面量（见 collect_command_findings），只扫代码部分；
# 双引号内的 $(...) 和 `...` 子表达式会执行，保留参与扫描。
_PATTERNS = [
    (re.compile(r"\brm\s+(-\w+\s+)*-[rf]+\s+/", re.I),         "system-destructive",   "rm -rf / (destructive recursive delete)"),
    (re.compile(r"\brm\s+(-\w+\s+)*-[rf]+\s+\*", re.I),        "system-destructive",   "rm -rf * (destructive wildcard delete)"),
    (re.compile(r"\brm\s+(-\w+\s+)*-[rf]+\s+~", re.I),         "system-destructive",   "rm -rf ~ (destructive home dir delete)"),
    (re.compile(r"\bformat\s+[a-zA-Z]:", re.I),                "system-destructive",   "format (disk format)"),
    (re.compile(r"\bdd\s+.*\bof=/dev/", re.I),                 "system-destructive",   "dd of=/dev/ (device write)"),
    (re.compile(r"\bmkfs\b", re.I),                            "system-destructive",   "mkfs (filesystem format)"),
    (re.compile(r"\bshutdown\b", re.I),                        "system-destructive",   "shutdown"),
    (re.compile(r"\breboot\b", re.I),                          "system-destructive",   "reboot"),
    (re.compile(r":\(\)\s*\{", re.I),                          "system-destructive",   "fork bomb"),
    (re.compile(r"\bcd\s+\.\.\s*&&\s*rm\s", re.I),             "system-destructive",   "cd .. && rm (parent dir delete)"),
    (re.compile(r">\s*/dev/sd[a-z]", re.I),                    "system-destructive",   "redirect to disk device"),
    # Code execution from string: the payload lives in a string literal that
    # string-stripping would hide from the scan, so flag the invocation itself
    (re.compile(r"(?<![a-zA-Z0-9_-])eval\b", re.I),            "code-from-string",     "eval (code execution from string — run the command directly)"),
    # Cross-tool isolation: use the pwsh tool instead of calling from bash
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:powershell|pwsh)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "cross-tool-isolation", "PowerShell invocation from bash (use the pwsh tool instead)"),
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:cmd|wsl)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "cross-tool-isolation", "cmd/WSL invocation from bash (cross-tool isolation)"),
]


class BashTool(ShellCommandTool):
    name = "bash"
    shell_kind = "bash"
    _FINDING_PATTERNS = _PATTERNS
    _action_label = "shell command"
    _command_param_desc = (
        "The shell command to execute (action='run'). Do NOT include 'timeout' in the command "
        "— use the timeout parameter instead."
    )
    _working_dir_desc = "Override the working directory for this command. Defaults to the agent's cwd."

    def _build_description(self) -> str:
        """Build tool description with background task support."""
        return (
            "Execute a shell command via Git Bash and return its output. "
            "Commands run in the agent's working directory. Supports pipes, redirects, and all shell features.\n"
            "Use for: ls, git, npm, curl, system commands, file operations, etc.\n"
            "Do NOT use bash to invoke PowerShell — use the `pwsh` tool instead (cross-tool calls are "
            "flagged by static analysis and usually rejected by the safety judge).\n"
            "Commands that do not match a pre-approved rule go through an automatic safety judgment; "
            "if a command needs user confirmation it will be surfaced to the user. Do NOT reword a "
            "command just to dodge the judgment — follow the suggestion in the error instead.\n"
            f"Paths are in Windows format (e.g., {self.cwd}).\n\n"
            "## Background tasks\n"
            "Long-running commands: action=\"run\", run_in_background=true → task_id. "
            "You will receive an automatic notification when it completes — no polling needed. "
            "Use action=\"read\"/\"kill\"/\"write_stdin\"/\"list\" to manage background tasks."
        )

    def _resolve_working_dir(self, working_dir: str) -> str:
        # bash 语义归一化（/tmp → usertemp 挂载点），policy.resolve 会把
        # 无盘符根路径解析到当前盘根，与 Git Bash 实际 cd 的位置不符
        return normalize_shell_path(working_dir, self.cwd, "bash")

    def _apply_cwd_prefix(self, command: str, initial_dir: str) -> str:
        bash_dir = _to_bash_path(initial_dir)
        return f"cd {self._shell_escape(bash_dir)} && " + command

    def _run_foreground(self, command: str, timeout: int) -> ToolResult:
        return self._run_bash(command, timeout=timeout)

    def _run_background(self, command: str) -> ToolResult:
        return self._start_background_task(command, shell_path=_GIT_BASH_PATH)
