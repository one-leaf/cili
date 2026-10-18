"""Bash tool - execute shell commands via Git Bash with background task support."""

from __future__ import annotations

import os
import re
from typing import Any

from core.security.approval_rules import is_builtin_readonly
from core.security.path_policy import OP_WRITE, PathTarget
from core.security.shell_paths import extract_shell_targets, normalize_shell_path
from core.tools.approval import (
    META_KEY,
    approval_decision_id,
    approval_placeholder_text,
)
from core.tools.base import (
    _GIT_BASH_PATH,
    _VENV_DIR,
    _VENV_SCRIPTS,
    Tool,
    ToolResult,
    _strip_shell_strings,
    _to_bash_path,
)

# 静态证据模式（原 ask/deny 黑名单转为判定证据，设计 §3.2.4 / §3.3.2）：
# 命中不再拒绝，而是作为 static_findings 交给 lite 判定器，由判定器结合上下文
# 决定 allow / ask / deny。
# 扫描前先剥掉字符串字面量（见 collect_command_findings），只扫代码部分；
# 双引号内的 $(...) 和 `...` 子表达式会执行，保留参与扫描。
_FINDING_PATTERNS = [
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
    # Cross-tool isolation: use pwsh/python tools instead of calling from bash
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:powershell|pwsh)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "cross-tool-isolation", "PowerShell invocation from bash (use the pwsh tool instead)"),
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:python3?|pythonw?)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "python-tool-routing", "Python invocation from bash bypasses the python tool's safety checks (use the python tool instead)"),
    (re.compile(r"(?<![a-zA-Z0-9_\\.-])py(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "py-launcher", "py launcher invocation from bash (use the python tool instead)"),
    (re.compile(r"(?<![a-zA-Z0-9_-])(?:cmd|wsl)(?:\.exe)?(?![a-zA-Z0-9_-])", re.I),
     "cross-tool-isolation", "cmd/WSL invocation from bash (cross-tool isolation)"),
]


class BashTool(Tool):
    name = "bash"

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session=None,
                 approval_store=None, approval_rules=None):
        super().__init__(cwd, workspace_uuid, session, approval_store=approval_store,
                         approval_rules=approval_rules)
        # 动态注入当前工作目录到描述中
        self.description = self._build_description()

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

        initial_dir = self.cwd
        working_targets: list[PathTarget] = []
        # 会话级批准按原始命令 ID 记录（cd 前缀在批准后才拼接，ID 不变）
        did = approval_decision_id(command)

        # Apply working_dir override：目标折叠进统一审批占位（一次授权覆盖命令+越界目录），
        # cd 前缀在批准后、真正执行前才拼接
        if working_dir:
            # bash 语义归一化（/tmp → usertemp 挂载点），policy.resolve 会把
            # 无盘符根路径解析到当前盘根，与 Git Bash 实际 cd 的位置不符
            resolved = normalize_shell_path(working_dir, self.cwd, "bash")
            if not os.path.isdir(resolved):
                return ToolResult(f"Error: working_dir does not exist: {resolved}", error=True)
            initial_dir = resolved
            working_targets = [PathTarget(OP_WRITE, working_dir, "working_dir", resolved=resolved)]

        # ── 审批管道（设计 §3.1）：命令级放行 → 统一判定占位 → 只读快道 ──
        # 路径门与命令门合并为**一张卡**：占位符携带完整命令 + 全部目标（含 working_dir）
        # + 静态证据，一次授权（会话级或落盘规则 + implied_path_rules）同时覆盖两者。
        store = self.approval_store
        store_hit = bool(store and store.is_approved(did))
        rule_hit = bool(self.approval_rules and self.approval_rules.match_command(self.name, command))
        if not (store_hit or rule_hit):
            targets = extract_shell_targets(command, "bash", cwd=self.cwd, initial_dir=initial_dir) + working_targets
            findings = self.collect_command_findings(command)
            policy = self._path_policy()
            # status 返回 deny（store 与 rules 均缺）或 needs_approval（有通道待批）
            # 都视为越界待审；deny 在下方 no-store 分支落地为 fail-closed 拒绝
            needs_path = any(policy.status(t) in ("needs_approval", "deny") for t in targets)
            if findings or needs_path:
                if not store:
                    reason = findings[0]["evidence"] if findings else "写/删目标越界"
                    return ToolResult(
                        f"Error: 命令需审批但无审批通道（fail-closed）：{reason}\n命令：`{command}`",
                        error=True,
                    )
                approval = self._pending_approval(command, did, targets, findings)
                return ToolResult(
                    approval_placeholder_text(approval),
                    completed=False,
                    meta={META_KEY: approval},
                )
            if is_builtin_readonly(self.name, command):
                pass  # 只读快速通道：免判定（越界目标已在 needs_path 拦下）
            elif not store:
                pass  # 无 store 的独立/测试场景：无证据的良性命令保持旧行为直接执行
            else:
                approval = self._pending_approval(command, did, targets, findings)
                return ToolResult(
                    approval_placeholder_text(approval),
                    completed=False,
                    meta={META_KEY: approval},
                )

        if working_dir:
            bash_dir = _to_bash_path(initial_dir)
            command = f"cd {self._shell_escape(bash_dir)} && " + command

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
    def collect_command_findings(command: str) -> list[dict]:
        """静态证据提取（原 deny/ask 黑名单转为判定证据，设计 §3.3.2）。

        返回全部命中项（不截断首个）：id 对应 constraints 条目，evidence 为人读描述。
        String literals are stripped first so that string data (e.g. echo "pwsh works")
        does not trigger findings; subexpressions that execute as code ($( ... ) and
        ` ... `) are preserved.
        """
        code = _strip_shell_strings(command, "bash")
        findings: list[dict] = []
        for pattern, constraint_id, label in _FINDING_PATTERNS:
            if pattern.search(code):
                findings.append({"id": constraint_id, "evidence": label})
        return findings
