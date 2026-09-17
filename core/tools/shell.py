"""Shell 工具基础设施：venv 路径、shell 探测、命令字符串处理与同步执行 mixin。

从 base.py 抽取的 shell 域：环境常量与查找器、deny-scan 前的字符串剥离、
_run_shell/_run_bash/_run_pwsh 命令执行族（Tool 以 ShellMixin 继承）。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any

from core.config import PROJECT_ROOT
from core.tools.result import ToolResult


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


def _find_pwsh() -> str:
    """Find PowerShell executable path.

    Priority: env var > PS7 install path > PATH(pwsh) > PS 5.1 path > fallback.
    Mirrors deepseek-harness resolve.ts: probe well-known locations before PATH.
    """
    env_path = os.environ.get("PWSH_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    # Probe well-known PowerShell 7 install location (before PATH search)
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    pwsh7 = os.path.join(program_files, "PowerShell", "7", "pwsh.exe")
    if os.path.isfile(pwsh7):
        return pwsh7

    import shutil as _shutil
    # Search PATH for pwsh 7 (e.g. Microsoft Store install)
    pwsh = _shutil.which("pwsh")
    if pwsh:
        return pwsh

    # Probe Windows PowerShell 5.1 explicit path
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    ps51 = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(ps51):
        return ps51

    return "powershell.exe"


_PWSH_PATH = _find_pwsh()
_GIT_BASH_PATH = _find_git_bash()


def _is_word_char(ch: str) -> bool:
    """Shell word character (alnum/underscore) — used to detect adjacent-string gluing."""
    return ch.isalnum() or ch == "_"


def _decode_ansi_c(body: str) -> str:
    """Decode bash ANSI-C quoting ($'...') escape sequences into literal bytes.

    Handles the escapes that matter for command construction: \\ \\' \\" \\n \\t \\r
    \\a \\b \\f \\v \\e \\xHH, octal \\NNN and \\cX; unknown escapes keep the char.
    """
    out: list[str] = []
    i, n = 0, len(body)
    simple = {
        "\\": "\\", "'": "'", '"': '"', "n": "\n", "t": "\t", "r": "\r",
        "a": "\a", "b": "\b", "f": "\f", "v": "\v", "e": "\x1b", "E": "\x1b",
    }
    while i < n:
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            out.append("\\")
            break
        e = body[i]
        i += 1
        if e in simple:
            out.append(simple[e])
        elif e == "x":
            h = body[i:i + 2]
            if len(h) == 2 and all(c in "0123456789abcdefABCDEF" for c in h):
                out.append(chr(int(h, 16)))
                i += 2
            else:
                out.append("x")
        elif e in "01234567":
            o = e
            while i < n and len(o) < 3 and body[i] in "01234567":
                o += body[i]
                i += 1
            out.append(chr(int(o, 8)))
        elif e == "c":
            if i < n:
                out.append(chr(ord(body[i]) & 0x1f))
                i += 1
        else:
            out.append(e)
    return "".join(out)


def _strip_dq_string(text: str, start: int, out: list[str], mode: str, escape: str,
                     keep_content: bool) -> int:
    """Strip a double-quoted string starting at text[start] == '"'. Returns next index.

    Preserves $(...) subexpressions (both shells) and `...` substitutions (bash)
    verbatim — they execute as code, so deny-scan must still see them.
    When keep_content is True (quote glued to a preceding word char, e.g. `ev"al"`),
    the literal text is kept as well so the concatenated word is still caught.
    """
    i, n = start + 1, len(text)
    while i < n:
        c = text[i]
        if c == escape:
            if keep_content:
                out.append(text[i:i + 2])
            i += 2
            continue
        if c == '"':
            return i + 1
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            depth = 0
            j = i + 1
            while j < n:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if mode == "bash" and c == "`":
            j = i + 1
            while j < n and text[j] != "`":
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if keep_content:
            out.append(c)
        i += 1
    out.append(" ")  # unterminated — the command would be a parse error anyway
    return n


def _expand_ansi_c_quotes(text: str) -> str:
    """Replace bash $'...' ANSI-C quoted strings with their decoded bytes.

    Decoded content becomes plain text so the deny-scan sees concatenated
    command words (e.g. $'\\x72\\x6d' -rf / → rm -rf /).
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "$" and i + 1 < n and text[i + 1] == "'":
            j = i + 2
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            out.append(_decode_ansi_c(text[i + 2:j]))
            i = j + 1 if j < n else n
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _strip_shell_strings(text: str, mode: str) -> str:
    """Strip quoted string literals from a shell command before deny-scan.

    mode: "pwsh" (backtick escape) or "bash" (backslash escape).

    Rules:
    - A quoted region not glued to a preceding word char is string DATA:
      replaced by a space so keywords inside it (e.g. `echo "rm -rf /"`) no
      longer trigger deny rules. Unterminated quotes strip to end (the shell
      would reject the command anyway).
    - A quoted region glued to a preceding word char is part of a command word
      (adjacent string concatenation, e.g. `ev"al"`, `r'm'`): its content is
      kept so the concatenated keyword is still caught by the scan.
    - Bash `$'...'` ANSI-C strings are expanded to decoded bytes first
      (e.g. `$'\\x72\\x6d'` == `rm`), so they can form command words.
    - Subexpressions inside double quotes ($( ... ) and bash ` ... `) are
      preserved because they execute.
    """
    if mode == "bash":
        text = _expand_ansi_c_quotes(text)
    out: list[str] = []
    i, n = 0, len(text)
    escape = "`" if mode == "pwsh" else "\\"
    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if mode == "pwsh" and j + 1 < n and text[j + 1] == "'":
                        j += 2  # pwsh: '' is an escaped single quote
                        continue
                    break
                j += 1
            glued = i > 0 and _is_word_char(text[i - 1])
            if j >= n:
                out.append(text[i + 1:] if glued else " ")
                break
            out.append(text[i + 1:j] if glued else " ")
            i = j + 1
        elif c == '"':
            glued = i > 0 and _is_word_char(text[i - 1])
            i = _strip_dq_string(text, i, out, mode, escape, glued)
        else:
            out.append(c)
            i += 1
    return "".join(out)


class ShellMixin:
    """命令同步执行族：_run_shell/_run_bash/_run_pwsh + 进程树终止。

    依赖宿主 Tool 提供的 self.cwd / self.output_file / self.on_output /
    self._emit_output / self._BASH_MAX_RESULT_SIZE_CHARS / self.truncate_middle。
    """

    @staticmethod
    def _kill_process_tree(proc: subprocess.Popen) -> None:
        """Kill a process and all its descendants."""
        pid = proc.pid
        if sys.platform == "win32":
            # Windows: use taskkill /T to kill process tree
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=10,
                )
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        else:
            # Unix: try to kill process group first
            try:
                os.killpg(os.getpgid(pid), 9)
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except Exception:
                    pass

    def _run_shell(self, command: str, timeout: int = 30, stdin: str | None = None,
                   max_chars: int | None = None, output_file: str | None = None,
                   *, shell: str = "bash") -> ToolResult:
        """Execute command via Git Bash or PowerShell with real-time output streaming.

        _run_bash()/_run_pwsh() 的共享实现。命令前缀构造与 Popen argv 因 shell 而异，
        reader 线程、实时流式、截断、超时与 token 预算逻辑共用一份，修复只需改一处。
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
            if shell == "pwsh":
                # PowerShell: UTF-8 编码 preamble + $env: 赋值（Windows 原生路径，; 分隔）
                encoding_preamble = (
                    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                )
                paths = []
                if _VENV_DIR:
                    paths.append(_VENV_DIR)
                if _VENV_SCRIPTS:
                    paths.append(_VENV_SCRIPTS)
                env_setup = ""
                if paths:
                    path_str = ";".join(paths)
                    env_setup = f'$env:PATH = "{path_str};$env:PATH"; '
                env_setup += (
                    f'$env:TEMP = "{_TMP_DIR}"; '
                    f'$env:TMP = "{_TMP_DIR}"; '
                    f'$env:TMPDIR = "{_TMP_DIR}"; '
                    f'$env:LANG = "C.UTF-8"; '
                    f'$env:PYTHONIOENCODING = "utf-8"; '
                    f'$env:PYTHONUTF8 = "1"; '
                )
                full_command = f"{encoding_preamble}{env_setup}{command}"
                argv = [_PWSH_PATH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", full_command]
            else:
                # Git Bash: export 前缀（POSIX 路径，: 分隔；PYTHONUNBUFFERED 仅 bash 侧设置）
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
                        f'LANG=C.UTF-8 PYTHONIOENCODING=utf-8 PYTHONUTF8=1 PYTHONUNBUFFERED=1 '
                        f'&& {command}'
                    )
                else:
                    full_command = (
                        f'export TEMP="{tmp_bash}" TMP="{tmp_bash}" TMPDIR="{tmp_bash}" '
                        f'LANG=C.UTF-8 PYTHONIOENCODING=utf-8 PYTHONUTF8=1 PYTHONUNBUFFERED=1 '
                        f'&& {command}'
                    )
                argv = [_GIT_BASH_PATH, "-c", full_command]

            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # 合并 stderr 到 stdout，确保实时输出可见
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.cwd,
                stdin=subprocess.PIPE if stdin else None,
            )

            # 在独立线程中按块读取 stdout（主线程用 queue.get(timeout) 实现超时控制）
            chunk_queue: queue.Queue[str | None] = queue.Queue()

            def _reader_thread():
                try:
                    # 使用 read(1) 逐字符读取，确保实时流式显示
                    # 这比 read(1024) 更能保证实时性，即使输出没有换行符
                    while True:
                        char = proc.stdout.read(1)
                        if not char:
                            break
                        chunk_queue.put(char)
                except Exception:
                    pass
                finally:
                    chunk_queue.put(None)  # sentinel: EOF

            reader_thread = threading.Thread(target=_reader_thread, daemon=True)
            reader_thread.start()

            # 写入 stdin（如果有的话）
            if stdin:
                try:
                    proc.stdin.write(stdin)
                    proc.stdin.close()
                except Exception:
                    pass

            # 收集输出，同时写入流文件
            output_parts: list[str] = []
            start_time = time.monotonic()
            timed_out = False

            # 打开流文件（如有）：append 模式，UTF-8，逐块写入+flush
            f_out = None
            written_bytes = 0
            if output_file:
                try:
                    f_out = open(output_file, "a", encoding="utf-8")
                    written_bytes = os.path.getsize(output_file)
                except Exception:
                    f_out = None  # 写入失败不影响命令执行
            # 实时输出缓冲：按换行或达到阈值批量 emit，减少事件数又保持实时
            out_buf: list[str] = []

            try:
                while True:
                    try:
                        chunk = chunk_queue.get(timeout=0.5)
                    except queue.Empty:
                        elapsed = time.monotonic() - start_time
                        if elapsed > timeout:
                            timed_out = True
                            self._kill_process_tree(proc)
                            break
                        continue
                    if chunk is None:
                        break  # EOF
                    output_parts.append(chunk)
                    if f_out:
                        try:
                            f_out.write(chunk)
                            f_out.flush()
                        except Exception:
                            pass
                    if self.on_output:
                        out_buf.append(chunk)
                        # 文本模式写 Windows 下 \n -> \r\n（多 1 字节），
                        # 补偿后 written_bytes 才是文件真实字节数（与 /stream 端点契约一致）
                        written_bytes += len(chunk.encode("utf-8", "replace")) + (1 if chunk == "\n" else 0)
                        if chunk in ("\n", "\r") or len(out_buf) >= 32:
                            self._emit_output("".join(out_buf), written_bytes)
                            out_buf.clear()
            finally:
                if self.on_output and out_buf:
                    self._emit_output("".join(out_buf), written_bytes)
                    out_buf.clear()
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
                    self._kill_process_tree(proc)
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

    def _run_bash(self, command: str, timeout: int = 30, stdin: str | None = None,
                  max_chars: int | None = None, output_file: str | None = None) -> ToolResult:
        """Execute command via Git Bash with real-time output streaming.

        The agent Python venv is automatically activated by prepending its
        Scripts directory to PATH. (delegates to _run_shell)
        """
        return self._run_shell(command, timeout=timeout, stdin=stdin,
                               max_chars=max_chars, output_file=output_file, shell="bash")

    def _run_pwsh(self, command: str, timeout: int = 30, stdin: str | None = None,
                  max_chars: int | None = None, output_file: str | None = None) -> ToolResult:
        """Execute command via PowerShell with real-time output streaming.

        Automatically sets UTF-8 encoding and adds the agent Python venv to PATH.
        (delegates to _run_shell)
        """
        return self._run_shell(command, timeout=timeout, stdin=stdin,
                               max_chars=max_chars, output_file=output_file, shell="pwsh")
