"""PowerShell tool - execute commands via pwsh with background task support."""

from __future__ import annotations

import re

from core.tools.base import ToolResult
from core.tools.shell_command import COMMON_DESTRUCTIVE_PATTERNS, ShellCommandTool

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

_PATTERNS = [
    (_RECURSIVE_FORCE_DELETE,
     "system-destructive", "Remove-Item -Recurse -Force on drive path (destructive recursive delete)"),
    (re.compile(r"\bFormat-Volume\b", re.I), "system-destructive", "Format-Volume (disk format)"),
    (re.compile(r"\bClear-Disk\b", re.I), "system-destructive", "Clear-Disk (disk wipe)"),
    (re.compile(r"\bInitialize-Disk\b", re.I), "system-destructive", "Initialize-Disk (disk initialize)"),
    (re.compile(r"\bdiskpart\b", re.I), "system-destructive", "diskpart (disk management)"),
    (re.compile(r"\bStop-Computer\b", re.I), "system-destructive", "Stop-Computer (shutdown)"),
    (re.compile(r"\bRestart-Computer\b", re.I), "system-destructive", "Restart-Computer (reboot)"),
    (re.compile(r"\bStop-Process\s+-Id\s+0\b", re.I), "system-destructive", "Stop-Process -Id 0 (system process)"),
    # Code execution from string: the payload lives in a string literal that
    # string-stripping would hide from the scan, so flag the invocation itself
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:Invoke-Expression|iex)(?![a-zA-Z0-9_-])", re.I),
     "code-from-string", "Invoke-Expression (code execution from string — run the command directly)"),
    # Cross-tool isolation: use dedicated tools instead of calling from pwsh
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:bash|sh)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "cross-tool-isolation", "Bash invocation from PowerShell (use the bash tool instead)"),
    # UAC 提权（Start-Process powershell -Verb RunAs）是合法需求；嵌套 -Command 载荷
    # 不参与本扫描，判定器会提示用户确认完整命令
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:powershell|pwsh)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "cross-tool-isolation", "PowerShell re-invocation (nested -Command payload bypasses the safety scan; verify before approving)"),
] + COMMON_DESTRUCTIVE_PATTERNS


class PwshTool(ShellCommandTool):
    name = "pwsh"
    shell_kind = "pwsh"
    _FINDING_PATTERNS = _PATTERNS
    _action_label = "PowerShell command"
    _command_param_desc = (
        "The PowerShell command to execute (action='run'). Do NOT include timeout in the command "
        "— use the timeout parameter instead."
    )
    _working_dir_desc = (
        "Override the working directory for this command. Defaults to the agent's cwd. "
        "Use Windows paths (e.g., C:\\Projects)."
    )

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

    def _resolve_working_dir(self, working_dir: str) -> str:
        return self._path_policy().resolve(working_dir)

    def _apply_cwd_prefix(self, command: str, initial_dir: str) -> str:
        # 目录名可能含单引号，pwsh 单引号内用 '' 转义
        escaped_dir = initial_dir.replace("'", "''")
        return f"Set-Location '{escaped_dir}'; " + command

    def _run_foreground(self, command: str, timeout: int) -> ToolResult:
        return self._run_pwsh(command, timeout=timeout)

    def _run_background(self, command: str) -> ToolResult:
        return self._start_background_task(command, shell="pwsh")
