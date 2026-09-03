"""Tool base class and interface."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT


# ── 后台任务管理 ──────────────────────────────────────────────────────────────

@dataclass
class BackgroundTask:
    """A background task (shell command or SubAgent).

    Supports two task types:
    - "shell": subprocess.Popen (command execution)
    - "subagent": SubAgent instance (autonomous agent loop)
    """
    task_id: str
    task_type: str = "shell"  # "shell" or "subagent"
    command: str = ""  # For shell tasks
    process: subprocess.Popen | None = None  # For shell tasks
    output_file: str | None = None
    output_queue: queue.Queue[str | None] = field(default_factory=queue.Queue)
    reader_thread: threading.Thread | None = None
    status: str = "running"  # running, completed, killed, error
    exit_code: int | None = None
    created_at: float = field(default_factory=time.time)
    stdin_pipe: Any = None  # subprocess.PIPE for write_stdin

    # SubAgent-specific fields
    subagent: Any = None  # SubAgent instance
    session_manager: Any = None  # SessionManager reference
    result: dict | None = None  # SubAgent execution result


class BackgroundTaskManager:
    """Manages background tasks (shell commands and SubAgents) across all tool instances.

    Thread-safe singleton that maintains a registry of background processes.
    """
    _tasks: dict[str, BackgroundTask] = {}
    _counter: int = 0
    _lock = threading.Lock()

    @classmethod
    def allocate_task_id(cls, prefix: str = "bg") -> str:
        """Allocate a unique task ID with prefix."""
        with cls._lock:
            cls._counter += 1
            return f"{prefix}-{cls._counter}"

    @classmethod
    def register(cls, task: BackgroundTask) -> None:
        """Register a background task."""
        with cls._lock:
            cls._tasks[task.task_id] = task

    @classmethod
    def get(cls, task_id: str) -> BackgroundTask | None:
        """Get a task by ID."""
        return cls._tasks.get(task_id)

    @classmethod
    def remove(cls, task_id: str) -> None:
        """Remove a task from registry."""
        with cls._lock:
            cls._tasks.pop(task_id, None)

    @classmethod
    def list_tasks(cls) -> list[dict[str, Any]]:
        """List all background tasks with their status."""
        with cls._lock:
            result = []
            for task_id, task in cls._tasks.items():
                # Check status
                if task.task_type == "shell" and task.process:
                    if task.process.poll() is not None:
                        task.status = "completed"
                        task.exit_code = task.process.returncode
                elif task.task_type == "subagent" and task.subagent:
                    if not task.subagent._running and task.status == "running":
                        task.status = "completed"

                result.append({
                    "task_id": task_id,
                    "task_type": task.task_type,
                    "command": task.command or (task.subagent.task[:100] if task.subagent else ""),
                    "status": task.status,
                    "exit_code": task.exit_code,
                    "created_at": task.created_at,
                })
            return result


class ToolResult:
    """Result of a tool execution.

    Supports both old and new interfaces for backward compatibility:

    New interface (recommended):
        blocks: list[ContentBlock] - typed content blocks
        is_error: bool - whether this is an error result
        meta: dict - optional structured metadata for UI
        completed: bool | None - placeholder lifecycle:
            None (default): normal tool result, no loop pause
            False: placeholder mode, agent loop exits (waiting for external input)
            True: placeholder completed (result written back)

    Old interface (backward compat):
        output: str - plain text output (converted to [TextBlock(text=output)])
        error: bool - alias for is_error
        content: list[dict] - legacy multimodal content (deprecated)
        wait_for_user: bool - deprecated alias for completed=False

    Examples:
        # Normal tool result
        ToolResult(output="some text")
        ToolResult(output="error", error=True)

        # Placeholder mode (ask_user, subagent)
        ToolResult(output="执行中...", completed=False, meta={"exec_id": "..."})
    """

    def __init__(
        self,
        output: str = "",
        error: bool = False,
        content: list[dict] | None = None,
        # New interface
        blocks: list | None = None,
        is_error: bool = False,
        meta: dict | None = None,
        completed: bool | None = None,
        # Deprecated alias (backward compat)
        wait_for_user: bool = False,
    ):
        # Normalize error flags
        self.is_error = is_error or error

        # Handle both old (wait_for_user) and new (completed) interface
        # wait_for_user=True → completed=False (not yet done, loop should exit)
        if completed is not None:
            self.completed = completed
        elif wait_for_user:
            self.completed = False
        else:
            self.completed = None

        # Convert old interface to new interface
        if blocks is not None:
            self.blocks = blocks
        elif content:
            from core.llm.types import block_from_dict
            self.blocks = [block_from_dict(b) for b in content if isinstance(b, dict)]
            # If output also provided (old-style multimodal), prepend as text description
            if output:
                from core.llm.types import TextBlock
                self.blocks.insert(0, TextBlock(text=output))
        elif output:
            from core.llm.types import TextBlock
            self.blocks = [TextBlock(text=output)]
        else:
            self.blocks = []

        # Store meta
        self.meta = meta

    @property
    def output(self) -> str:
        """Backward compat: extract text from blocks."""
        from core.llm.types import TextBlock
        return "".join(
            block.text for block in self.blocks
            if isinstance(block, TextBlock)
        )

    @property
    def error(self) -> bool:
        """Backward compat alias for is_error."""
        return self.is_error

    @property
    def wait_for_user(self) -> bool:
        """Backward compat: completed=False means wait_for_user=True."""
        return self.completed is False

    def __repr__(self) -> str:
        return f"ToolResult(blocks={self.blocks!r}, is_error={self.is_error})"


# Python venv path: data/deps/python (relative to project root)
_PROJECT_ROOT = str(PROJECT_ROOT)
_VENV_DIR = os.path.join(_PROJECT_ROOT, "data", "deps", "python")
_VENV_SCRIPTS = os.path.join(_VENV_DIR, "Scripts")
_TMP_DIR = os.path.join(_PROJECT_ROOT, "data", "tmp")


def _to_bash_path(path: str) -> str:
    """Convert Windows path to Git Bash format.

    E.g., 'E:\\AI\\cili' -> '/e/AI/cili'
    """
    if not path:
        return path
    # Replace backslashes with forward slashes
    result = path.replace("\\", "/")
    # Convert drive letter: E:/ -> /e/
    if len(result) >= 2 and result[1] == ":":
        drive = result[0].lower()
        result = "/" + drive + result[2:]
    return result


def _find_git_bash() -> str:
    """Find Git Bash executable path.

    Uses the deps directory path directly (set by start.ps1/main.py).
    """
    # Prefer the path set by main.py via environment variable
    env_path = os.environ.get("GIT_BASH_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    # Fallback: use deps directory path directly
    deps_bash = os.path.join(_PROJECT_ROOT, "data", "deps", "git", "bin", "bash.exe")
    if os.path.isfile(deps_bash):
        return deps_bash

    # Final fallback: assume bash is in PATH
    return "bash"


_GIT_BASH_PATH = _find_git_bash()


class Tool:
    """Base class for all tools."""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}  # JSON Schema

    # Directories to skip during file search operations
    IGNORE_DIRS: set[str] = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
        ".egg-info", ".next", ".nuxt", "target",
    }

    # ── 工具输出限制 ──────────────────────────────────────────────────────
    # 默认单工具结果上限（字符数），各工具可按需覆盖
    MAX_TOOL_RESULT_SIZE_CHARS: int = 50_000

    # Bash 工具专用限制（更严格，防止命令输出撑爆上下文）
    _BASH_MAX_RESULT_SIZE_CHARS: int = 30_000
    _BASH_MAX_OUTPUT_LINES: int = 2000

    # Token 预算常量（4 字节 ≈ 1 token）
    BYTES_PER_TOKEN: int = 4

    @staticmethod
    def approx_token_count(text: str) -> int:
        """估算文本的 token 数（4 字节 ≈ 1 token）。"""
        byte_len = len(text.encode("utf-8", errors="replace"))
        return (byte_len + Tool.BYTES_PER_TOKEN - 1) // Tool.BYTES_PER_TOKEN

    @staticmethod
    def truncate_middle(text: str, max_tokens: int) -> str:
        """Token 预算截断：保留开头和结尾，删除中间内容。

        与 Codex 的 truncate_middle_with_token_budget 策略一致：
        - 若文本在预算内，直接返回
        - 否则保留前 40% + 后 40%，中间用标记替代
        """
        tokens = Tool.approx_token_count(text)
        if tokens <= max_tokens:
            return text

        max_bytes = max_tokens * Tool.BYTES_PER_TOKEN
        # 保留前 40% 和后 40%，中间 20% 用标记替代
        head_bytes = int(max_bytes * 0.4)
        tail_bytes = int(max_bytes * 0.4)

        # 按 UTF-8 安全截断
        head = text.encode("utf-8", errors="replace")[:head_bytes].decode("utf-8", errors="ignore")
        tail_bytes_data = text.encode("utf-8", errors="replace")[-tail_bytes:]
        tail = tail_bytes_data.decode("utf-8", errors="ignore")
        # 确保 tail 从完整字符开始（跳过可能的截断字符）
        if tail and tail[0].encode("utf-8", errors="replace") != tail_bytes_data[:len(tail[0].encode("utf-8", errors="replace"))]:
            tail = tail[1:]

        removed_tokens = tokens - max_tokens
        marker = f"\n\n…{removed_tokens:,} tokens truncated…\n\n"
        return head + marker + tail

    @staticmethod
    def truncate_result(text: str, max_chars: int) -> str:
        """按字符数截断工具结果（保留开头，末尾加提示）。

        用于 Edit/Write 等工具的结果输出限制。
        """
        if len(text) <= max_chars:
            return text
        truncated = text[:max_chars]
        last_newline = truncated.rfind('\n')
        if last_newline > max_chars * 0.9:
            truncated = truncated[:last_newline]
        return truncated + f"\n\n... (truncated from {len(text):,} to {max_chars:,} chars)"

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session_manager=None):
        self.cwd = os.path.abspath(cwd)
        self.workspace_uuid = workspace_uuid
        self.session_manager = session_manager  # For accessing session info (e.g., in python tool)
        # 工具输出文件路径：由 agent 在 execute() 前设置
        # _run_bash() 逐行写入此文件（实时流式），前端可轮询读取
        # save_output_to_file() 兜底确保所有工具输出都落盘
        self.output_file: str | None = None

    def _resolve_path(self, path: str) -> str:
        """Resolve a file path to absolute, relative to cwd."""
        if not os.path.isabs(path):
            path = os.path.join(self.cwd, path)
        return os.path.abspath(path)

    def save_output_to_file(self, result: ToolResult) -> None:
        """统一保存工具输出到外部文件。

        由 agent 的 _execute_tool() 在工具执行后统一调用。
        如果文件已存在且有内容（_run_bash 实时写入过），则跳过避免覆盖。

        支持两种格式：
        - 纯文本：保存为 .txt（现有格式）
        - 多模态：保存为 .json（包含图片和文本块）
        """
        if not self.output_file:
            return

        # 检查文件是否已有内容（_run_bash 实时写入）
        if os.path.exists(self.output_file):
            try:
                if os.path.getsize(self.output_file) > 0:
                    return  # _run_bash 已实时写入，不覆盖
            except Exception:
                pass

        try:
            from core.llm.types import ImageBlock

            # 检查是否有图片块
            has_images = any(isinstance(block, ImageBlock) for block in result.blocks)

            if has_images:
                # 多模态内容：保存为 json（替换 .txt 后缀为 .json）
                if self.output_file.endswith(".txt"):
                    json_path = self.output_file[:-4] + ".json"
                else:
                    json_path = self.output_file + ".json"
                data = {
                    "type": "multimodal",
                    "blocks": []
                }
                for block in result.blocks:
                    if hasattr(block, 'to_dict'):
                        data["blocks"].append(block.to_dict())
                    elif hasattr(block, '__dict__'):
                        # 简单转换
                        block_dict = {}
                        for k, v in block.__dict__.items():
                            if not k.startswith('_'):
                                block_dict[k] = v
                        data["blocks"].append({"type": block.__class__.__name__.lower().replace('block', ''), **block_dict})

                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

                # 更新 _output_path 为 json 文件（供 agent 知道实际文件路径）
                # 通过修改 output_file 属性实现
                self.output_file = json_path
            else:
                # 纯文本：保持 txt
                with open(self.output_file, "w", encoding="utf-8") as f:
                    f.write(result.output)
        except Exception:
            pass  # 写入失败不影响工具执行

    def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with given parameters. Override in subclass."""
        raise NotImplementedError

    def to_schema(self) -> dict[str, Any]:
        """Convert to Anthropic tool schema format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def validate_input(self, kwargs: dict[str, Any]) -> ToolResult | None:
        """校验 LLM 传入的参数是否符合 parameters schema。

        检查项：
        - required 字段是否存在
        - enum 值是否合法
        - 基本类型约束（string/integer/number/boolean/array/object）

        注意：此方法应在 coerce_input 完成类型转换后调用。

        Returns:
            None: 校验通过
            ToolResult(error=True): 校验失败，错误信息已格式化
        """
        if not self.parameters:
            return None

        props = self.parameters.get("properties", {})
        required = set(self.parameters.get("required", []))

        # 1. 检查必填字段
        missing = required - set(kwargs.keys())
        if missing:
            return ToolResult(
                f"Error: 缺少必填参数: {', '.join(sorted(missing))}",
                error=True,
            )

        # 2. 逐字段校验（只校验 LLM 实际传了的字段）
        for key, value in kwargs.items():
            if key not in props:
                continue
            prop_schema = props[key]
            prop_type = prop_schema.get("type", "")

            # enum 校验
            if "enum" in prop_schema and value not in prop_schema["enum"]:
                allowed = prop_schema["enum"]
                return ToolResult(
                    f"Error: 参数 '{key}' 值无效。允许值: {allowed}",
                    error=True,
                )

            # 类型校验（跳过 None 值，让默认值生效）
            if value is None:
                continue

            if prop_type == "string" and not isinstance(value, str):
                return ToolResult(
                    f"Error: 参数 '{key}' 应为 string 类型，实际为 {type(value).__name__}",
                    error=True,
                )
            elif prop_type in ("integer", "number") and not isinstance(value, (int, float)):
                return ToolResult(
                    f"Error: 参数 '{key}' 应为 {prop_type} 类型，实际为 {type(value).__name__}",
                    error=True,
                )
            elif prop_type == "boolean" and not isinstance(value, bool):
                return ToolResult(
                    f"Error: 参数 '{key}' 应为 boolean 类型，实际为 {type(value).__name__}",
                    error=True,
                )
            elif prop_type == "array" and not isinstance(value, list):
                return ToolResult(
                    f"Error: 参数 '{key}' 应为 array 类型，实际为 {type(value).__name__}",
                    error=True,
                )
            elif prop_type == "object" and not isinstance(value, dict):
                return ToolResult(
                    f"Error: 参数 '{key}' 应为 object 类型，实际为 {type(value).__name__}",
                    error=True,
                )

        return None

    def coerce_input(self, kwargs: dict[str, Any]) -> dict[str, Any] | ToolResult:
        """根据 parameters schema 校验并修正 LLM 传入的参数。

        流程：
        1. 类型转换 — integer/boolean 等类型修正（LLM 常将数字传为字符串）
        2. validate_input() — 参数合法性校验

        Returns:
            dict: 校验并转换后的参数
            ToolResult(error=True): 校验失败
        """
        if not self.parameters:
            return kwargs

        # 第一步：类型转换（在 validate_input 之前，因为 LLM 常把数字传为字符串）
        props = self.parameters.get("properties", {})
        required = set(self.parameters.get("required", []))
        coerced = {}

        for key, value in (kwargs or {}).items():
            prop_schema = props.get(key, {})
            prop_type = prop_schema.get("type", "")

            # 数值类型转换
            if prop_type in ("integer", "number") and isinstance(value, str):
                try:
                    value = int(value) if prop_type == "integer" else float(value)
                except (ValueError, TypeError):
                    pass  # 无法转换则保留原值，让 validate_input 报错

            # 布尔类型转换（"true"/"false" 字符串 → bool）
            elif prop_type == "boolean" and isinstance(value, str):
                if value.lower() == "true":
                    value = True
                elif value.lower() == "false":
                    value = False

            # 跳过缺失的可选参数（让默认值生效）
            if value is None and key not in required:
                continue

            coerced[key] = value

        # 第二步：参数校验（在类型转换之后）
        validation_error = self.validate_input(coerced)
        if validation_error is not None:
            return validation_error

        return coerced

    @staticmethod
    def _shell_escape(s: str) -> str:
        """Escape string for shell single-quoting."""
        return "'" + s.replace("'", "'\"'\"'") + "'"

    @staticmethod
    def _clean_surrogates(s: str) -> str:
        """Remove surrogate characters invalid in UTF-8."""
        return ''.join(c for c in s if not ('\ud800' <= c <= '\udfff'))

    def _run_bash(self, command: str, timeout: int = 30, stdin: str | None = None,
                  max_chars: int | None = None, output_file: str | None = None) -> ToolResult:
        """Execute command via Git Bash with real-time output streaming.

        The agent Python venv is automatically activated by prepending its
        Scripts directory to PATH.

        Args:
            max_chars: Character limit for output. Defaults to BASH_MAX_OUTPUT_LENGTH
                       env var (default 30,000), capped at _BASH_MAX_RESULT_SIZE_CHARS (30,000).
            output_file: Path to write output to, line by line, in real-time.
                         Used for frontend polling. Falls back to self.output_file.
        """
        # Bash 默认输出上限，可通过环境变量调整，但不超过硬上限
        default_chars = int(os.environ.get("BASH_MAX_OUTPUT_LENGTH", "30000"))
        if max_chars is None:
            max_chars = min(default_chars, self._BASH_MAX_RESULT_SIZE_CHARS)
        else:
            max_chars = min(max_chars, self._BASH_MAX_RESULT_SIZE_CHARS)

        # 使用实例属性作为 fallback
        if output_file is None:
            output_file = self.output_file

        proc = None
        try:
            # Build PATH prefix for the command
            # Add both _VENV_DIR (python.exe) and _VENV_SCRIPTS (pip.exe) to PATH
            paths = []
            if _VENV_DIR:
                paths.append(_to_bash_path(_VENV_DIR))
            if _VENV_SCRIPTS:
                paths.append(_to_bash_path(_VENV_SCRIPTS))

            # 统一临时目录（bash 格式）
            tmp_bash = _to_bash_path(_TMP_DIR)

            if paths:
                path_str = ":".join(paths)
                full_command = (
                    f'export PATH="{path_str}:$PATH" '
                    f'TEMP="{tmp_bash}" TMP="{tmp_bash}" TMPDIR="{tmp_bash}" '
                    f'&& {command}'
                )
            else:
                full_command = (
                    f'export TEMP="{tmp_bash}" TMP="{tmp_bash}" TMPDIR="{tmp_bash}" '
                    f'&& {command}'
                )

            proc = subprocess.Popen(
                [_GIT_BASH_PATH, "-c", full_command],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # 合并 stderr 到 stdout，确保实时输出可见
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.cwd,
                stdin=subprocess.PIPE if stdin else None,
            )

            # 在独立线程中逐行读取 stdout（主线程用 queue.get(timeout) 实现超时控制）
            stdout_lines: list[str] = []
            line_queue: queue.Queue[str | None] = queue.Queue()

            def _reader_thread():
                try:
                    for line in proc.stdout:
                        line_queue.put(line)
                except Exception:
                    pass
                finally:
                    line_queue.put(None)  # sentinel: EOF

            reader_thread = threading.Thread(target=_reader_thread, daemon=True)
            reader_thread.start()

            # 写入 stdin（如果有的话）
            if stdin:
                try:
                    proc.stdin.write(stdin)
                    proc.stdin.close()
                except Exception:
                    pass

            # 逐行收集输出，同时写入流文件
            output_parts: list[str] = []
            start_time = time.monotonic()
            timed_out = False

            # 打开流文件（如有）：append 模式，UTF-8，逐行写入+flush
            f_out = None
            if output_file:
                try:
                    f_out = open(output_file, "a", encoding="utf-8")
                except Exception:
                    f_out = None  # 写入失败不影响命令执行

            try:
                while True:
                    try:
                        line = line_queue.get(timeout=0.5)
                    except queue.Empty:
                        elapsed = time.monotonic() - start_time
                        if elapsed > timeout:
                            timed_out = True
                            proc.kill()
                            break
                        continue
                    if line is None:
                        break  # EOF
                    output_parts.append(line)
                    if f_out:
                        try:
                            f_out.write(line)
                            f_out.flush()
                        except Exception:
                            pass
            finally:
                if f_out:
                    try:
                        f_out.close()
                    except Exception:
                        pass

            # 等待线程和进程结束
            reader_thread.join(timeout=2)
            if not timed_out:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

            output = "".join(output_parts).strip() or "(no output)"

            # Truncate by character count (primary limit for context window)
            if len(output) > max_chars:
                # Find a safe truncation point at line boundary
                truncated = output[:max_chars]
                last_newline = truncated.rfind('\n')
                if last_newline > max_chars * 0.9:  # Only use if close to limit
                    truncated = truncated[:last_newline]
                output = truncated + f"\n\n... (truncated from {len(output):,} to {max_chars:,} chars)"
            else:
                # Also truncate by line count if under char limit
                lines = output.split('\n')
                if len(lines) > self._BASH_MAX_OUTPUT_LINES:
                    truncated_count = len(lines) - self._BASH_MAX_OUTPUT_LINES
                    output = '\n'.join(lines[:self._BASH_MAX_OUTPUT_LINES])
                    output += f"\n\n... ({truncated_count} lines truncated, {len(lines)} total)"

            # Token budget check: 确保输出不超 token 预算（Bash 默认 10K tokens）
            bash_token_budget = int(os.environ.get("BASH_MAX_OUTPUT_TOKENS", "10000"))
            output = self.truncate_middle(output, bash_token_budget)

            if proc.returncode != 0:
                output = f"[exit code: {proc.returncode}]\n{output}"

            return ToolResult(output, error=(proc.returncode != 0))
        except subprocess.TimeoutExpired:
            # Kill the process and all children
            if proc:
                proc.kill()
                proc.wait()
            return ToolResult(f"Error: command timed out after {timeout} seconds", error=True)
        except Exception as e:
            if proc:
                try:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass
            return ToolResult(f"Error executing command: {e}", error=True)

    # ── 后台任务管理方法 ──────────────────────────────────────────────────────

    def _start_background_task(
        self,
        command: str,
        shell_path: str | None = None,
        env_prefix: str = "",
    ) -> ToolResult:
        """Start a command in background and return task_id.

        Args:
            command: Shell command to execute.
            shell_path: Path to shell executable. Defaults to Git Bash.
            env_prefix: Environment setup prefix (e.g., PATH export).
        """
        task_id = BackgroundTaskManager.allocate_task_id()

        # Determine shell
        if shell_path is None:
            shell_path = _GIT_BASH_PATH

        # Build full command with environment prefix
        if env_prefix:
            full_command = f"{env_prefix} && {command}"
        else:
            full_command = command

        # Determine output file
        output_file = self.output_file

        try:
            # Start process with stdin pipe for write_stdin support
            proc = subprocess.Popen(
                [shell_path, "-c", full_command],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,  # Keep stdin open for write_stdin
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.cwd,
            )

            # Create output queue and reader thread
            output_queue: queue.Queue[str | None] = queue.Queue()

            def reader_thread():
                try:
                    for line in proc.stdout:
                        output_queue.put(line)
                        # Write to output file in real-time
                        if output_file:
                            try:
                                with open(output_file, "a", encoding="utf-8") as f:
                                    f.write(line)
                                    f.flush()
                            except Exception:
                                pass
                except Exception:
                    pass
                finally:
                    output_queue.put(None)  # Sentinel: EOF

            thread = threading.Thread(target=reader_thread, daemon=True)
            thread.start()

            # Register task
            task = BackgroundTask(
                task_id=task_id,
                command=command,
                process=proc,
                output_file=output_file,
                output_queue=output_queue,
                reader_thread=thread,
                stdin_pipe=proc.stdin,
            )

            BackgroundTaskManager.register(task)

            return ToolResult(
                f"Background task started.\n"
                f"Task ID: {task_id}\n"
                f"Command: {command}\n\n"
                f"Use read_task(\"{task_id}\") to check output.\n"
                f"Use kill_task(\"{task_id}\") to terminate."
            )

        except Exception as e:
            return ToolResult(f"Error starting background task: {e}", error=True)

    def _read_background_task(self, task_id: str) -> ToolResult:
        """Read accumulated output from a background task (non-blocking)."""
        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        # Drain queue (non-blocking)
        output_parts = []
        try:
            while True:
                line = task.output_queue.get_nowait()
                if line is None:
                    # EOF - process completed
                    break
                output_parts.append(line)
        except queue.Empty:
            pass

        # Check process status
        if task.process.poll() is not None:
            # Process completed
            task.status = "completed"
            task.exit_code = task.process.returncode

            # Drain any remaining output
            try:
                while True:
                    line = task.output_queue.get_nowait()
                    if line is None:
                        break
                    output_parts.append(line)
            except queue.Empty:
                pass

            # Clean up completed task
            BackgroundTaskManager.remove(task_id)

            output = "".join(output_parts).strip()
            if output:
                return ToolResult(
                    f"[Task {task_id} completed with exit code {task.exit_code}]\n{output}"
                )
            else:
                return ToolResult(
                    f"Task {task_id} completed with exit code {task.exit_code}"
                )

        # Still running
        output = "".join(output_parts).strip()
        if output:
            return ToolResult(f"[Task {task_id} still running]\n{output}")
        else:
            return ToolResult(f"Task {task_id} still running (no new output)")

    def _kill_background_task(self, task_id: str) -> ToolResult:
        """Terminate a background task."""
        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        try:
            # Kill process
            task.process.kill()
            task.process.wait(timeout=5)
            task.status = "killed"

            # Clean up
            BackgroundTaskManager.remove(task_id)

            return ToolResult(f"Task {task_id} terminated")

        except subprocess.TimeoutExpired:
            # Force kill
            try:
                task.process.kill()
            except Exception:
                pass
            BackgroundTaskManager.remove(task_id)
            return ToolResult(f"Task {task_id} force terminated")

        except Exception as e:
            return ToolResult(f"Error killing task {task_id}: {e}", error=True)

    def _write_stdin_to_task(self, task_id: str | None, text: str) -> ToolResult:
        """Send input to a running background task's stdin."""
        if not task_id:
            return ToolResult("Error: task_id is required", error=True)

        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        # Check if process is still running
        if task.process.poll() is not None:
            task.status = "completed"
            BackgroundTaskManager.remove(task_id)
            return ToolResult(
                f"Error: task '{task_id}' has already completed", error=True
            )

        try:
            # Write to stdin
            task.process.stdin.write(text)
            task.process.stdin.flush()
            return ToolResult(f"Sent input to task {task_id}")
        except Exception as e:
            return ToolResult(f"Error writing to task {task_id}: {e}", error=True)

    def _list_background_tasks(self) -> ToolResult:
        """List all background tasks with their status."""
        tasks = BackgroundTaskManager.list_tasks()
        if not tasks:
            return ToolResult("No background tasks")

        lines = ["Background tasks:"]
        for t in tasks:
            task_type = t.get("task_type", "shell")
            status = t["status"]
            if status == "completed" and t.get("exit_code") is not None:
                status += f" (exit {t['exit_code']})"
            lines.append(f"  {t['task_id']}: [{task_type}][{status}] {t['command']}")
        return ToolResult("\n".join(lines))

    def _start_background_subagent(
        self,
        subagent: Any,
        session_manager: Any,
        exec_id: str,
        task_summary: str,
    ) -> ToolResult:
        """Start a SubAgent in background and return task_id.

        Args:
            subagent: SubAgent instance to run in background.
            session_manager: SessionManager reference.
            exec_id: Execution ID.
            task_summary: Task summary for display.
        """
        task_id = BackgroundTaskManager.allocate_task_id(prefix="subagent")

        def run_subagent():
            """Run SubAgent in background thread."""
            try:
                result = subagent.run()
                task.result = result
                task.status = "completed"

                # Save SubAgent log
                if session_manager and exec_id:
                    from datetime import datetime
                    try:
                        session_manager.save_subagent_log(
                            exec_id=exec_id,
                            task=subagent.task,
                            messages=subagent.messages,
                            metadata={
                                "started_at": subagent._started_at.strftime("%Y-%m-%d %H:%M:%S") if subagent._started_at else "",
                                "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "duration_seconds": subagent._elapsed_seconds(),
                                "status": result.get("status", "completed"),
                                "iterations": result.get("iterations", 0),
                                "max_iterations": subagent.max_iterations,
                            },
                            summary=result.get("summary", ""),
                        )
                        session_manager.save()
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning(
                            f"Failed to save SubAgent log for {task_id}: {e}"
                        )

            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Background SubAgent {task_id} error: {e}")
                task.status = "error"
                task.result = {"status": "error", "message": str(e)}

        # Create background task entry
        task = BackgroundTask(
            task_id=task_id,
            task_type="subagent",
            command=task_summary,
            subagent=subagent,
            session_manager=session_manager,
            status="running",
        )

        BackgroundTaskManager.register(task)

        # Start SubAgent in background thread
        thread = threading.Thread(target=run_subagent, daemon=True)
        thread.start()
        task.reader_thread = thread

        return ToolResult(
            f"Background SubAgent started.\n"
            f"Task ID: {task_id}\n"
            f"Task: {task_summary}\n\n"
            f"Use read_task(\"{task_id}\") to check status.\n"
            f"Use kill_task(\"{task_id}\") to terminate."
        )

    def _read_background_subagent(self, task_id: str) -> ToolResult:
        """Read status and output from a background SubAgent."""
        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        if task.task_type != "subagent":
            return ToolResult(f"Error: task '{task_id}' is not a SubAgent", error=True)

        subagent = task.subagent
        if not subagent:
            return ToolResult(f"Error: SubAgent for '{task_id}' not found", error=True)

        # Check status
        if task.result:
            # Completed
            result = task.result
            status = result.get("status", "unknown")
            summary = result.get("summary", "")
            iterations = result.get("iterations", 0)

            # Clean up completed task
            BackgroundTaskManager.remove(task_id)

            if summary:
                return ToolResult(
                    f"SubAgent {task_id} completed.\n"
                    f"Status: {status}\n"
                    f"Iterations: {iterations}\n\n"
                    f"Summary:\n{summary}"
                )
            else:
                return ToolResult(
                    f"SubAgent {task_id} completed.\n"
                    f"Status: {status}\n"
                    f"Iterations: {iterations}"
                )
        else:
            # Still running
            iterations = len(subagent.messages) // 2  # Rough estimate
            return ToolResult(
                f"SubAgent {task_id} still running.\n"
                f"Estimated iterations: {iterations}\n"
                f"Current tool: {getattr(subagent, '_current_tool', 'none')}"
            )

    def _kill_background_subagent(self, task_id: str) -> ToolResult:
        """Terminate a background SubAgent."""
        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        if task.task_type != "subagent":
            return ToolResult(f"Error: task '{task_id}' is not a SubAgent", error=True)

        subagent = task.subagent
        if not subagent:
            return ToolResult(f"Error: SubAgent for '{task_id}' not found", error=True)

        try:
            # Set stop flag
            subagent._stopped = True
            task.status = "killed"

            # Clean up
            BackgroundTaskManager.remove(task_id)

            return ToolResult(f"SubAgent {task_id} terminated")

        except Exception as e:
            return ToolResult(f"Error killing SubAgent {task_id}: {e}", error=True)
