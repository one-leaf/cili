"""Python tool - execute Python code in the agent's managed environment.

basic code execution, package install, env info.
"""

from __future__ import annotations

import os
from typing import Any

from core.tools.shared.base import Tool, ToolResult, _VENV_SCRIPTS


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
            "## Background Tasks:\n"
            "For long-running Python scripts, use `run_in_background: true` to run in background.\n"
            "- `read_task(task_id)`: Read accumulated output\n"
            "- `kill_task(task_id)`: Terminate the background task\n"
            "- `write_stdin(task_id, text)`: Send input to stdin\n"
            "- `list_tasks()`: List all background tasks\n\n"
            "## Note:\n"
            "The virtual environment is also activated in bash (python/pip are in PATH), "
            "so simple commands can be run directly via bash. Use this tool for code execution, "
            "package installation, and environment info."
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
        python_exe = os.path.join(_VENV_SCRIPTS, "python.exe")
        path = self._resolve_path(file)
        if not os.path.isfile(path):
            return ToolResult(f"Error: script file not found: {file}", error=True)

        cmd = f'PYTHONIOENCODING=utf-8 "{python_exe}" "{path}"'
        if args:
            cmd += f" {args}"

        if run_in_background:
            return self._start_background_task(cmd, shell_path=_GIT_BASH_PATH)
        return self._run_bash(cmd, timeout=300)

    def _execute_code(self, code: str, run_in_background: bool = False) -> ToolResult:
        """Execute Python code (no LLM injection — main agent is the LLM itself)."""
        python_exe = os.path.join(_VENV_SCRIPTS, "python.exe")

        if run_in_background:
            # For background execution, write code to temp file and execute
            import tempfile
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as f:
                f.write(code)
                temp_path = f.name
            cmd = f'PYTHONIOENCODING=utf-8 "{python_exe}" "{temp_path}"'
            return self._start_background_task(cmd, shell_path=_GIT_BASH_PATH)

        return self._run_bash(f'PYTHONIOENCODING=utf-8 "{python_exe}" -', timeout=300, stdin=code)

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

    def _install_packages(self, packages: str) -> ToolResult:
        """Install Python packages using pip."""
        # 每个包单独引号，防止 "pkg1 pkg2" 被 pip 当成单个包名
        quoted = " ".join(f'"{p}"' for p in packages.split())
        mirror = self._get_pip_mirror()
        mirror_arg = f'-i {mirror} ' if mirror else ''
        return self._run_pip(f'install {mirror_arg}{quoted}')

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
        python_exe = os.path.join(_VENV_SCRIPTS, "python.exe")
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
        python_exe = os.path.join(_VENV_SCRIPTS, "python.exe")

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
from core.tools.shared.base import _GIT_BASH_PATH
