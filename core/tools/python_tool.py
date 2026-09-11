"""Python tool - execute Python code in the agent's managed environment.

basic code execution, package install, env info.
"""

from __future__ import annotations

import ast
import os
import shlex
import tempfile
import time
from typing import Any

from core.tools.base import Tool, ToolResult, _VENV_DIR, _VENV_SCRIPTS


# 后台执行临时脚本的前缀（用于过期清理）
_BG_SCRIPT_PREFIX = "cili_bg_"
_BG_SCRIPT_MAX_AGE_SECONDS = 24 * 3600


# Cross-tool isolation: block Python code from invoking bash/pwsh.
# 用 AST 静态分析（而非正则），字符串拼接/别名/动态 getattr 都无法绕过。
_DENY_DIRECT_CALLS = {"eval", "exec", "__import__"}
_OS_SHELL_ATTRS = {"system", "popen"}  # os.system / os.popen 必然拉起 shell
_SUBPROCESS_CALLS = {
    "Popen", "call", "run", "check_call", "check_output",
    "getoutput", "getstatusoutput",
}
_SHELL_TOKENS = ("bash", "pwsh", "powershell")


class PythonTool(Tool):
    name = "python"

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session_manager=None, config=None):
        super().__init__(cwd, workspace_uuid, session_manager)
        self._config = config
        self.description = self._build_description()

    def _build_description(self) -> str:
        """Build tool description with background task support."""
        return (
            "**Execute Python code or manage Python packages.**\n"
            "This tool runs Python in the agent's managed virtual environment.\n\n"
            "## Actions:\n"
            "- **execute**: Run Python code (action='execute', code='...')\n"
            "- **execute_file**: Run a Python script file (action='execute_file', file='path/to/script.py')\n"
            "- **install**: Install packages using pip (action='install', packages='...')\n"
            "- **uninstall**: Remove packages (action='uninstall', packages='...')\n"
            "- **upgrade**: Upgrade packages to latest version (action='upgrade', packages='...')\n"
            "- **check**: Check if a package is installed and its version (action='check', packages='...')\n"
            "- **info**: Show environment info and installed packages (action='info')\n\n"
            "## Pre-installed packages:\n"
            "requests, httpx, beautifulsoup4, lxml, numpy, pandas, pyyaml, toml, Pillow, pytest\n\n"
            "## Matplotlib CJK & Math Support:\n"
            "**CJK fonts are pre-configured. No manual font setup needed.**\n"
            "- ❌ Do NOT set `plt.rcParams['font.sans-serif'] = [...]`\n"
            "- ❌ Do NOT set `plt.rcParams['font.family'] = '...'`\n"
            "- ✅ Just use CJK text directly, e.g., `plt.title('中文标题')`\n"
            "- ⚠️ For subscript/superscript/math symbols, use mathtext `$...$`: `$x_1$`, `$P^{-1}$`, `$\\lambda_1$`\n"
            "- ❌ Do NOT use Unicode subscripts/superscripts (₁₂³⁻⁰), they render as boxes (□)\n\n"
            "## Restrictions:\n"
            "- eval() and exec() are not allowed (security risk)\n"
            "- Do not invoke bash/pwsh from Python code (use bash/pwsh tools directly)\n"
            "- This is the ONLY way to run Python — do NOT use bash or pwsh to invoke python\n\n"
            "## Background Tasks:\n"
            "For long-running Python scripts, use `run_in_background: true` to run in background.\n"
            "- `read_task(task_id)`: Read accumulated output\n"
            "- `kill_task(task_id)`: Terminate the background task\n"
            "- `write_stdin(task_id, text)`: Send input to stdin\n"
            "- `list_tasks()`: List all background tasks"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """Dynamic parameters schema."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["execute", "execute_file", "install", "uninstall", "upgrade", "check", "info"],
                    "description": "Action to perform.",
                    "default": "execute",
                },
                "code": {
                    "type": "string",
                    "description": "Python code to execute. Required when action='execute'.",
                },
                "file": {
                    "type": "string",
                    "description": "Path to a Python script file to run. Required when action='execute_file'.",
                },
                "args": {
                    "type": "string",
                    "description": "Command-line arguments to pass to the script. Optional, used with action='execute_file'. Example: '--input data.txt --verbose'",
                },
                "packages": {
                    "type": "string",
                    "description": "Space-separated list of packages. Used with 'install', 'uninstall', 'upgrade', and 'check' actions. Example: 'flask sqlalchemy requests-html'",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "If true, run the Python code/script in background and return immediately "
                        "with a task_id. Only works with 'execute' and 'execute_file' actions."
                    ),
                },
                "read_task": {
                    "type": "string",
                    "description": "Task ID to read output from (e.g., 'bg-1').",
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
                            "description": "Text to send to stdin (e.g., 'data\\n').",
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

    def execute(
        self,
        action: str = "execute",
        code: str | None = None,
        file: str | None = None,
        args: str | None = None,
        packages: str | None = None,
        run_in_background: bool | None = None,
        read_task: str | None = None,
        kill_task: str | None = None,
        write_stdin: dict | None = None,
        list_tasks: bool | None = None,
    ) -> ToolResult:
        """Execute Python action or manage background tasks."""

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

        # Regular actions
        if action == "execute":
            if not code:
                return ToolResult("Error: 'code' is required for 'execute' action", error=True)
            return self._execute_code(code, run_in_background=bool(run_in_background))
        elif action == "execute_file":
            if not file:
                return ToolResult("Error: 'file' is required for 'execute_file' action", error=True)
            return self._execute_file(file, args, run_in_background=bool(run_in_background))
        elif action == "install":
            packages = (packages or "").strip()
            if not packages:
                return ToolResult("Error: 'packages' is required for 'install' action", error=True)
            return self._install_packages(packages)
        elif action == "uninstall":
            packages = (packages or "").strip()
            if not packages:
                return ToolResult("Error: 'packages' is required for 'uninstall' action", error=True)
            return self._uninstall_packages(packages)
        elif action == "upgrade":
            packages = (packages or "").strip()
            if not packages:
                return ToolResult("Error: 'packages' is required for 'upgrade' action", error=True)
            return self._upgrade_packages(packages)
        elif action == "check":
            packages = (packages or "").strip()
            if not packages:
                return ToolResult("Error: 'packages' is required for 'check' action", error=True)
            return self._check_packages(packages)
        elif action == "info":
            return self._show_info()
        else:
            return ToolResult(f"Error: unknown action '{action}'", error=True)

    def _execute_file(self, file: str, args: str | None = None, run_in_background: bool = False) -> ToolResult:
        """Execute a Python script file."""
        python_exe = os.path.join(_VENV_DIR, "python.exe")
        path = self._resolve_path(file)
        if not os.path.isfile(path):
            return ToolResult(f"Error: script file not found: {file}", error=True)

        # Check file content for cross-tool invocations（读取失败 fail-closed，不跳过检查）
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return ToolResult(f"Error: cannot read script for safety check: {e}", error=True)
        deny_msg = self._check_python_deny(content)
        if deny_msg:
            return ToolResult(f"Error: code blocked by safety check — {deny_msg}", error=True)

        # Set MPLCONFIGDIR to use Cili's matplotlib config
        mpl_config_dir = os.path.join(_VENV_DIR, "matplotlib")
        cmd = f'MPLCONFIGDIR="{mpl_config_dir}" PYTHONIOENCODING=utf-8 "{python_exe}" "{path}"'
        if args:
            cmd += f" {shlex.quote(args)}"

        if run_in_background:
            return self._start_background_task(cmd, shell_path=_GIT_BASH_PATH)
        return self._run_bash(cmd, timeout=300)

    def _execute_code(self, code: str, run_in_background: bool = False) -> ToolResult:
        """Execute Python code (no LLM injection — main agent is the LLM itself)."""
        # Check for cross-tool invocations
        deny_msg = self._check_python_deny(code)
        if deny_msg:
            return ToolResult(f"Error: code blocked by safety check — {deny_msg}", error=True)

        python_exe = os.path.join(_VENV_DIR, "python.exe")
        # Set MPLCONFIGDIR to use Cili's matplotlib config
        mpl_config_dir = os.path.join(_VENV_DIR, "matplotlib")

        if run_in_background:
            # For background execution, write code to temp file and execute
            tmp_dir = os.environ.get("CILI_TMP")
            self._cleanup_stale_bg_scripts(tmp_dir)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8",
                dir=tmp_dir, prefix=_BG_SCRIPT_PREFIX,
            ) as f:
                f.write(code)
                temp_path = f.name
            cmd = f'MPLCONFIGDIR="{mpl_config_dir}" PYTHONIOENCODING=utf-8 "{python_exe}" "{temp_path}"'
            return self._start_background_task(cmd, shell_path=_GIT_BASH_PATH)

        return self._run_bash(f'MPLCONFIGDIR="{mpl_config_dir}" PYTHONIOENCODING=utf-8 "{python_exe}" -', timeout=300, stdin=code)

    @staticmethod
    def _cleanup_stale_bg_scripts(tmp_dir: str | None) -> None:
        """清理过期的后台执行临时脚本。

        后台脚本 delete=False 且随任务结束删除困难，每次执行会残留一个 .py；
        借新任务启动时清理超过 24 小时的旧文件，避免无限累积。
        """
        if not tmp_dir or not os.path.isdir(tmp_dir):
            return
        cutoff = time.time() - _BG_SCRIPT_MAX_AGE_SECONDS
        try:
            for name in os.listdir(tmp_dir):
                if not (name.startswith(_BG_SCRIPT_PREFIX) and name.endswith(".py")):
                    continue
                try:
                    path = os.path.join(tmp_dir, name)
                    if os.path.getmtime(path) < cutoff:
                        os.unlink(path)
                except OSError:
                    pass
        except OSError:
            pass

    def _get_pip_mirror(self) -> str:
        """Load pip mirror from config."""
        config = self._config
        if config is None:
            from core.config import load_config
            config = load_config()
        return config.system.pip_mirror

    @staticmethod
    def _filter_notices(result: ToolResult) -> ToolResult:
        """Strip pip [notice] lines from a ToolResult in place, returning it."""
        if '[notice]' not in result.output:
            return result
        lines = result.output.split('\n')
        filtered = [l for l in lines if not l.strip().startswith('[notice]')]
        result.output = '\n'.join(filtered)
        result.output = result.output.replace(
            '\n--- stderr ---\n\n--- end stderr ---', ''
        ).replace(
            '--- stderr ---\n\n--- end stderr ---\n', ''
        ).strip()
        result.output = result.output or '(no output)'
        return result

    def _run_pip(self, subcmd: str, timeout: int = 300) -> ToolResult:
        """Run a pip command with mirror + notice filtering."""
        pip_exe = os.path.join(_VENV_SCRIPTS, "pip.exe")
        cmd = f'"{pip_exe}" {subcmd} --disable-pip-version-check'
        return self._filter_notices(self._run_bash(cmd, timeout=timeout))

    @staticmethod
    def _check_python_deny(code: str) -> str | None:
        """AST 静态分析 Python 代码，拦截动态执行与 shell 逃逸。

        Returns:
            拦截原因；None 表示通过。
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return f"无法解析代码（语法错误）：{e}"

        # Pass 1: 收集模块别名与危险局部名（from os import system 等）
        os_aliases: set[str] = set()
        subprocess_aliases: set[str] = set()
        dangerous_locals: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "os":
                        os_aliases.add(alias.asname or "os")
                    elif alias.name == "subprocess":
                        subprocess_aliases.add(alias.asname or "subprocess")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "os":
                    for alias in node.names:
                        if alias.name in _OS_SHELL_ATTRS or alias.name == "*":
                            dangerous_locals.add(alias.asname or alias.name)
                elif node.module == "subprocess":
                    for alias in node.names:
                        if alias.name in _SUBPROCESS_CALLS or alias.name == "*":
                            dangerous_locals.add(alias.asname or alias.name)

        def _string_literals(node: ast.AST) -> list[str]:
            """收集表达式树内所有常量字符串（含 f-string 的常量片段）。"""
            return [
                sub.value for sub in ast.walk(node)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
            ]

        # Pass 2: 检查所有函数调用
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func

            if isinstance(func, ast.Name):
                if func.id in _DENY_DIRECT_CALLS:
                    return f"{func.id}() 动态代码执行被禁止"
                # getattr(os, "system") 动态取模块方法
                if func.id == "getattr" and node.args:
                    target = node.args[0]
                    if isinstance(target, ast.Name) and target.id in os_aliases | subprocess_aliases:
                        return "getattr() 动态访问 os/subprocess 方法被禁止"
                if func.id in dangerous_locals:
                    return f"{func.id}()（shell/子进程入口）被禁止，请改用 bash/pwsh 工具"

            if isinstance(func, ast.Attribute):
                attr = func.attr
                obj = func.value
                if isinstance(obj, ast.Name):
                    if obj.id in os_aliases and attr in _OS_SHELL_ATTRS:
                        return f"os.{attr}() 被禁止，请改用 bash/pwsh 工具"
                    if obj.id in subprocess_aliases and attr in _SUBPROCESS_CALLS:
                        shell_kw = [
                            kw for kw in node.keywords
                            if kw.arg == "shell"
                            and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
                        ]
                        if shell_kw:
                            return "subprocess 调用 shell=True 被禁止，请改用 bash/pwsh 工具"
                        strings = _string_literals(node)
                        if any(tok in s.lower() for s in strings for tok in _SHELL_TOKENS):
                            return f"subprocess 调用 {attr}() 指向 bash/pwsh 被禁止，请改用 bash/pwsh 工具"

        return None

    def _install_packages(self, packages: str) -> ToolResult:
        """Install Python packages using pip."""
        # 每个包单独引号，防止 "pkg1 pkg2" 被 pip 当成单个包名
        quoted = " ".join(f'"{p}"' for p in packages.split())
        mirror = self._get_pip_mirror()
        mirror_arg = f'-i {mirror} ' if mirror else ''
        return self._run_pip(f'install {mirror_arg}{quoted}', timeout=600)

    def _uninstall_packages(self, packages: str) -> ToolResult:
        """Uninstall Python packages."""
        quoted = " ".join(f'"{p}"' for p in packages.split())
        return self._run_pip(f'uninstall -y {quoted}')

    def _upgrade_packages(self, packages: str) -> ToolResult:
        """Upgrade Python packages to the latest version."""
        quoted = " ".join(f'"{p}"' for p in packages.split())
        mirror = self._get_pip_mirror()
        mirror_arg = f'-i {mirror} ' if mirror else ''
        return self._run_pip(f'install --upgrade {mirror_arg}{quoted}')

    def _check_packages(self, packages: str) -> ToolResult:
        """Check if packages are installed and show their versions."""
        python_exe = os.path.join(_VENV_DIR, "python.exe")
        pkg_list = packages.split()
        check_names = ', '.join(f'"{p}"' for p in pkg_list)
        code = f"""
import importlib.metadata

targets = [{check_names}]
for name in targets:
    try:
        dist = importlib.metadata.distribution(name)
        print(f"{{dist.metadata['Name']}} {{dist.version}}  [installed]")
    except importlib.metadata.PackageNotFoundError:
        print(f"{{name}}  [not installed]")
"""
        return self._run_bash(f'PYTHONIOENCODING=utf-8 "{python_exe}" -', timeout=30, stdin=code)

    def _show_info(self) -> ToolResult:
        """Show Python environment information."""
        python_exe = os.path.join(_VENV_DIR, "python.exe")

        # Get Python version and installed packages
        code = """
import sys, io, platform

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

print(f"Python: {sys.version}")
print(f"Platform: {platform.platform()}")
print(f"Executable: {sys.executable}")
print(f"Virtual Environment: {sys.prefix}")

try:
    import importlib.metadata
    packages = sorted(importlib.metadata.distributions(), key=lambda d: d.metadata['Name'].lower())
    print(f"\\nInstalled packages ({len(packages)}):")
    for pkg in packages:
        name = pkg.metadata['Name']
        version = pkg.version
        print(f"  - {name} {version}")
except Exception as e:
    print(f"Error listing packages: {e}")
"""
        result = self._run_bash(f'"{python_exe}" -', timeout=30, stdin=code)

        # Also show pip version
        pip_exe = os.path.join(_VENV_SCRIPTS, "pip.exe")
        pip_version = self._run_bash(f'"{pip_exe}" --version', timeout=10)

        if not result.error:
            output = result.output
            if not pip_version.error:
                output = f"pip: {pip_version.output.strip()}\n\n{output}"
            return ToolResult(output)
        return result


# Import for background execution
from core.tools.base import _GIT_BASH_PATH
