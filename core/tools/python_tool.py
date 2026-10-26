"""Python tool - execute Python code in the agent's managed environment.

basic code execution, package install, env info.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import sys
import tempfile
import time
from typing import Any

from core.security.python_paths import collect_python_targets
from core.tools.permission import (
    META_KEY,
    denied_rule_text,
    no_channel_text,
    permission_decision_id,
    permission_placeholder_text,
)
from core.tools.base import _GIT_BASH_PATH, _VENV_DIR, Tool, ToolResult


def _resolve_python_exe() -> str:
    """deps Python（embeddable 部署）存在时优先，否则回落当前解释器（CI/开发机）。"""
    exe = os.path.join(_VENV_DIR, "python.exe")
    return exe if os.path.isfile(exe) else sys.executable


def _resolve_mpl_config_dir() -> str:
    """matplotlib 配置目录：deps 布局存在时用 deps，否则用系统临时目录。"""
    if os.path.isdir(_VENV_DIR):
        return os.path.join(_VENV_DIR, "matplotlib")
    return os.path.join(tempfile.gettempdir(), "cili-matplotlib")


# pip 包名白名单（PEP 508 名称字符集）：双引号挡不住 bash 的 `$(...)` 命令
# 替换，也挡不住 pip 把 "--index-url" 当选项（审查 P2-工具层），源头校验
_PKG_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


def _first_invalid_package(packages: str) -> str | None:
    """返回第一个非法包名；全部合法返回 None。"""
    for p in packages.split():
        if not _PKG_NAME_RE.match(p):
            return p
    return None

# 后台执行临时脚本的前缀（用于过期清理）
_BG_SCRIPT_PREFIX = "cili_bg_"
_BG_SCRIPT_MAX_AGE_SECONDS = 24 * 3600


# Cross-tool isolation: block Python code from invoking bash/pwsh.
# 用 AST 静态分析（而非正则），字符串拼接/别名/动态 getattr 都无法绕过。
# 注意：这是护栏而非沙箱——有意的全面绕过（如 sys._getframe / marshal 构造）
# 无法完全阻止，目标是挡住 agent 在提示注入下随手写出的常见逃逸写法。
_DENY_DIRECT_CALLS = {"eval", "exec", "compile", "__import__"}
_OS_SHELL_ATTRS = {"system", "popen"}  # os.system / os.popen 必然拉起 shell
_SUBPROCESS_CALLS = {
    "Popen", "call", "run", "check_call", "check_output",
    "getoutput", "getstatusoutput",
}
_BUILTINS_DENY = _DENY_DIRECT_CALLS  # 与 _DENY_DIRECT_CALLS 必须同值，别名引用防止漂移出漏洞
_IMPORTLIB_DENY = {"import_module"}
# 危险模块 → 需拦截的方法：import os as o / builtins.eval / importlib.import_module
_DANGEROUS_MODULES = {
    "os": _OS_SHELL_ATTRS,
    "subprocess": _SUBPROCESS_CALLS,
    "builtins": _BUILTINS_DENY,
    "importlib": _IMPORTLIB_DENY,
}
# 无需显式 import 即存在的全局名（__builtins__ 是内建全局）
_ALWAYS_DANGEROUS = {"__builtins__"}
_SHELL_TOKENS = ("bash", "pwsh", "powershell")


class PythonTool(Tool):
    name = "python"

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session=None, config=None,
                 permission_store=None, permission_rules=None, permission_policy=None,
                 approval_store=None, approval_rules=None):
        super().__init__(
            cwd, workspace_uuid, session,
            permission_store=permission_store, permission_rules=permission_rules,
            permission_policy=permission_policy,
            approval_store=approval_store, approval_rules=approval_rules,
        )
        self._config = config
        self.description = self._build_description()

    def _build_description(self) -> str:
        """Build tool description with background task support."""
        return (
            "**Execute Python code or manage packages** in the managed venv.\n"
            "## Actions:\n"
            "- execute / execute_file: run code (code='...') or a script file (script_file='path/script.py')\n"
            "- install / uninstall / upgrade / check: pip package management (packages='pkg1 pkg2')\n"
            "- info: show environment info and installed packages\n\n"
            "## Pre-installed:\n"
            "requests, httpx, beautifulsoup4, lxml, numpy, pandas, pyyaml, toml, Pillow, pytest\n\n"
            "## Matplotlib CJK:\n"
            "CJK fonts are pre-configured — do NOT set plt.rcParams['font.sans-serif'] or font.family. "
            "Use CJK text directly (e.g., plt.title('中文标题')); for sub/superscripts and math use "
            "mathtext `$...$` (`$x_1$`, `$P^{-1}$`), not Unicode ₁₂³ (renders as boxes).\n\n"
            "## Rules:\n"
            "- Avoid eval()/exec() and shell escapes (os.system, subprocess) — static analysis flags them "
            "and the safety judge usually rejects them.\n"
            "- Do not invoke bash/pwsh from Python code.\n"
            "- This is the ONLY way to run Python — never use bash or pwsh to call python.\n"
            "- There is NO 'timeout' parameter here (unlike bash): execution timeout is fixed. "
            "For long-running scripts use action=\"execute\", run_in_background=true → task_id. "
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
                    "enum": ["execute", "execute_file", "install", "uninstall", "upgrade", "check", "info",
                             "read", "kill", "write_stdin", "list"],
                    "description": (
                        "Action to perform. "
                        "Code execution: 'execute' (default), 'execute_file'. "
                        "Package management: 'install', 'uninstall', 'upgrade', 'check'. "
                        "Environment: 'info'. "
                        "Background tasks: 'read' (read output), 'kill' (terminate), "
                        "'write_stdin' (send input), 'list' (show all tasks). "
                        "'read'/'kill'/'write_stdin' require 'task_id'."
                    ),
                    "default": "execute",
                },
                "code": {
                    "type": "string",
                    "description": "Python code to execute. Required when action='execute'.",
                },
                "script_file": {
                    "type": "string",
                    "description": "Path to a Python script file to run. Required when action='execute_file'.",
                },
                "args": {
                    "type": "string",
                    "description": "Space-separated command-line arguments to pass to the script. Optional, used with action='execute_file'. Example: '--input data.txt --verbose'",
                },
                "packages": {
                    "type": "string",
                    "description": "Space-separated list of packages. Used with 'install', 'uninstall', 'upgrade', and 'check' actions. Example: 'flask sqlalchemy requests-html'",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "action='execute'/'execute_file' only. If true, run in background and return immediately "
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
                    "description": "Text to send to stdin. Required for action='write_stdin' (e.g. 'data\\n').",
                },
            },
            "required": [],  # All parameters are optional; execute() validates
        }

    def execute(
        self,
        action: str = "execute",
        code: str | None = None,
        script_file: str | None = None,
        args: str | None = None,
        packages: str | None = None,
        run_in_background: bool | None = None,
        task_id: str | None = None,
        text: str | None = None,
    ) -> ToolResult:
        """Execute Python action or manage background tasks.

        Dispatch logic:
        - action='execute' (default): run Python code
        - action='execute_file': run a Python script file
        - action='install'/'uninstall'/'upgrade'/'check': package management
        - action='info': show environment info
        - action='read': read background task output (requires task_id)
        - action='kill': terminate a background task (requires task_id)
        - action='write_stdin': send input to a background task (requires task_id + text)
        - action='list': list all background tasks
        """
        # Handle background task operations
        if action == "list":
            return self._list_background_tasks()
        if action == "read":
            if not task_id:
                return ToolResult("Error: action='read' requires 'task_id' parameter (e.g. 'bg-1')", error=True)
            return self._read_background_task(task_id)
        if action == "kill":
            if not task_id:
                return ToolResult("Error: action='kill' requires 'task_id' parameter (e.g. 'bg-1')", error=True)
            return self._kill_background_task(task_id)
        if action == "write_stdin":
            if not task_id:
                return ToolResult("Error: action='write_stdin' requires 'task_id' and 'text' parameters", error=True)
            return self._write_stdin_to_task(task_id, text or "")

        # Regular actions
        if action == "execute":
            if not code:
                return ToolResult("Error: 'code' is required for 'execute' action", error=True)
            return self._execute_code(code, run_in_background=bool(run_in_background))
        elif action == "execute_file":
            if not script_file:
                return ToolResult("Error: 'script_file' is required for 'execute_file' action", error=True)
            return self._execute_file(script_file, args, run_in_background=bool(run_in_background))
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

    def _execute_file(self, script_file: str, args: str | None = None, run_in_background: bool = False) -> ToolResult:
        """Execute a Python script file."""
        python_exe = _resolve_python_exe()
        path = self._resolve_path(script_file, read_only=True)
        if not os.path.isfile(path):
            return ToolResult(f"Error: script file not found: {script_file}", error=True)

        # Check file content for cross-tool invocations（读取失败 fail-closed，不跳过检查）
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return ToolResult(f"Error: cannot read script for safety check: {e}", error=True)
        gate = self._read_gate([path])
        if gate:
            return gate
        findings = self._analyze_python_code(content)
        targets = collect_python_targets(content, cwd=self.cwd)
        gate = self._python_permission_gate(content, targets, findings)
        if gate:
            return gate

        # Set MPLCONFIGDIR to use Cili's matplotlib config
        mpl_config_dir = _resolve_mpl_config_dir()
        cmd = f'MPLCONFIGDIR="{mpl_config_dir}" PYTHONIOENCODING=utf-8 "{python_exe}" "{path}"'
        if args:
            # 逐个 token 引用：整体 quote 会把整串当成单个参数传给脚本
            cmd += " " + " ".join(shlex.quote(a) for a in args.split())

        if run_in_background:
            return self._start_background_task(cmd, shell_path=_GIT_BASH_PATH)
        return self._run_bash(cmd, timeout=300)

    def _execute_code(self, code: str, run_in_background: bool = False) -> ToolResult:
        """Execute Python code (no LLM injection — main agent is the LLM itself)."""
        findings = self._analyze_python_code(code)
        targets = collect_python_targets(code, cwd=self.cwd)
        gate = self._python_permission_gate(code, targets, findings)
        if gate:
            return gate

        python_exe = _resolve_python_exe()
        # Set MPLCONFIGDIR to use Cili's matplotlib config
        mpl_config_dir = _resolve_mpl_config_dir()

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
        """Strip pip [notice] lines from a ToolResult, returning it.

        ``output`` 是只读 property（由 blocks 推导），故重写 blocks 而非赋值 output。
        """
        if '[notice]' not in result.output:
            return result
        from core.llm.types import TextBlock

        lines = result.output.split('\n')
        filtered = [line for line in lines if not line.strip().startswith('[notice]')]
        text = '\n'.join(filtered).replace(
            '\n--- stderr ---\n\n--- end stderr ---', ''
        ).replace(
            '--- stderr ---\n\n--- end stderr ---\n', ''
        ).strip()
        # 保留非文本块，文本统一收敛为单块
        result.blocks = [TextBlock(text=text or '(no output)')] + [
            b for b in result.blocks if not isinstance(b, TextBlock)
        ]
        return result

    def _run_pip(self, subcmd: str, timeout: int = 300) -> ToolResult:
        """Run a pip command with mirror + notice filtering."""
        cmd = f'"{_resolve_python_exe()}" -m pip {subcmd} --disable-pip-version-check'
        return self._filter_notices(self._run_bash(cmd, timeout=timeout))

    @classmethod
    def _analyze_python_code(cls, code: str) -> list[dict]:
        """AST 静态分析：动态执行与 shell 逃逸转为判定证据（设计 §3.3.2）。

        原 deny 拦截已移除——命中项作为 static_findings 交给 lite 判定器
        （constraints 的 python-dynamic-exec 引导拒绝）。语法错误不再作为安全证据
        （代码自然执行失败）。
        """
        try:
            ast.parse(code)
        except SyntaxError:
            return []
        reason = cls._find_dynamic_exec(code)
        return [{"id": "python-dynamic-exec", "evidence": reason}] if reason else []

    def _python_permission_gate(self, code: str, targets: list, findings: list) -> ToolResult | None:
        policy = self._permission_policy()
        did = permission_decision_id(code)
        # 纯路径目标走路径门（path:write/delete 卡片）；有静态证据才走命令级判定
        if not findings and targets:
            return self._path_gate(targets)
        if findings:
            verdict = policy.command_verdict(self.name, code, targets, findings, did)
            if verdict.action != "allow":
                approval = self._python_approval(
                    code, targets, findings,
                    mode=verdict.action, rule=verdict.rule,
                    reason=verdict.reason or "代码命中安全证据，需要权限判定",
                )
                if not self._permission_has_channel():
                    if verdict.action == "deny":
                        return ToolResult(denied_rule_text(approval, verdict.rule), error=True)
                    return ToolResult(no_channel_text(approval), error=True)
                return ToolResult(
                    permission_placeholder_text(approval),
                    completed=False,
                    meta={META_KEY: approval},
                )
            return None
        # 无证据/无越界目标：ai 不白等一次 LLM；ask 按权限设置出卡；allow 执行
        if policy.config.default_mode == "ask":
            approval = self._python_approval(
                code, targets, findings, mode="ask", reason="默认策略：人工询问",
            )
            if not self._permission_has_channel():
                return ToolResult(no_channel_text(approval), error=True)
            return ToolResult(
                permission_placeholder_text(approval),
                completed=False,
                meta={META_KEY: approval},
            )
        return None

    def _python_approval(self, code: str, targets: list, findings: list,
                         mode: str = "ai", rule: dict | None = None,
                         reason: str = "代码命中动态执行/shell 逃逸证据，需要权限判定") -> dict:
        display = code if len(code) <= 2000 else code[:2000] + f"\n...（共 {len(code)} 字符）"
        return self._pending_permission(
            display, permission_decision_id(code), targets, findings,
            reason=reason, mode=mode, rule=rule,
        )

    def _pending_python_result(self, code: str, targets: list, findings: list,
                               mode: str = "ai", rule: dict | None = None,
                               reason: str = "代码命中动态执行/shell 逃逸证据，需要权限判定") -> ToolResult:
        """构造 python 代码的判定占位结果（判定编号按完整代码哈希，卡片截断展示）。"""
        approval = self._python_approval(code, targets, findings, mode=mode, rule=rule, reason=reason)
        return ToolResult(permission_placeholder_text(approval), completed=False, meta={META_KEY: approval})

    @staticmethod
    def _find_dynamic_exec(code: str) -> str | None:
        """AST 扫描动态执行 / shell 逃逸写法。Returns: 命中原因；None 表示未命中。"""
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return f"无法解析代码（语法错误）：{e}"

        # Pass 1: 收集危险模块别名与危险局部名
        #   import os as o        → aliases["os"] = {"o"}
        #   from builtins import eval → dangerous_locals = {"eval"}
        aliases: dict[str, set[str]] = {mod: set() for mod in _DANGEROUS_MODULES}
        dangerous_locals: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _DANGEROUS_MODULES:
                        aliases[alias.name].add(alias.asname or alias.name)
                    elif alias.name == "__builtins__":
                        aliases["builtins"].add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module in _DANGEROUS_MODULES:
                    for alias in node.names:
                        if alias.name == "*":
                            return f"from {node.module} import * 被禁止（无法静态审计具体名称）"
                        if alias.name in _DANGEROUS_MODULES[node.module]:
                            dangerous_locals.add(alias.asname or alias.name)

        def _string_literals(node: ast.AST) -> list[str]:
            """收集表达式树内所有常量字符串（含 f-string 的常量片段）。"""
            return [
                sub.value for sub in ast.walk(node)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
            ]

        def _deny_msg(mod: str, attr: str) -> str:
            if mod == "builtins":
                return f"{mod}.{attr}() 动态代码执行被禁止"
            return f"{mod}.{attr}() 被禁止，请改用 bash/pwsh 工具"

        # Pass 2: 检查所有函数调用
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func

            if isinstance(func, ast.Name):
                if func.id in _DENY_DIRECT_CALLS:
                    return f"{func.id}() 动态代码执行被禁止"
                if func.id in dangerous_locals:
                    return f"{func.id}()（shell/子进程/动态执行入口）被禁止，请改用 bash/pwsh 工具"
                # getattr(os, "system") / getattr(builtins, "eval") 动态取模块方法
                if func.id == "getattr" and node.args:
                    target = node.args[0]
                    if isinstance(target, ast.Name) and (
                        target.id in _ALWAYS_DANGEROUS
                        or any(target.id in aliases[mod] for mod in _DANGEROUS_MODULES)
                    ):
                        return "getattr() 动态访问危险模块方法被禁止"

            elif isinstance(func, ast.Attribute):
                attr = func.attr
                obj = func.value
                if isinstance(obj, ast.Name):
                    obj_id = obj.id
                    # __builtins__.eval(...) / __builtins__["eval"] 未显式 import
                    if obj_id in _ALWAYS_DANGEROUS and attr in _BUILTINS_DENY:
                        return f"{obj_id}.{attr}() 动态代码执行被禁止"
                    for mod, attrs in _DANGEROUS_MODULES.items():
                        if obj_id not in aliases[mod] or attr not in attrs:
                            continue
                        if mod == "subprocess":
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
                            break  # subprocess 非 shell 调用放行
                        return _deny_msg(mod, attr)

            # globals()['eval'](...) / vars()['system'](...) / __builtins__['eval'](...)
            # 经全局字典动态取函数后调用
            if isinstance(func, ast.Subscript):
                sub_val = func.value
                resolved = (
                    sub_val.id
                    if isinstance(sub_val, ast.Name)
                    else (
                        sub_val.func.id
                        if isinstance(sub_val, ast.Call) and isinstance(sub_val.func, ast.Name)
                        else None
                    )
                )
                if resolved in ("globals", "vars", "locals", "__builtins__"):
                    return "通过全局字典动态取函数调用被禁止"

        return None

    def _install_packages(self, packages: str) -> ToolResult:
        """Install Python packages using pip."""
        bad = _first_invalid_package(packages)
        if bad:
            return ToolResult(f"Error: invalid package name '{bad}'", error=True)
        # 每个包单独引号，防止 "pkg1 pkg2" 被 pip 当成单个包名
        quoted = " ".join(f'"{p}"' for p in packages.split())
        mirror = self._get_pip_mirror()
        mirror_arg = f'-i {mirror} ' if mirror else ''
        return self._run_pip(f'install {mirror_arg}{quoted}', timeout=600)

    def _uninstall_packages(self, packages: str) -> ToolResult:
        """Uninstall Python packages."""
        bad = _first_invalid_package(packages)
        if bad:
            return ToolResult(f"Error: invalid package name '{bad}'", error=True)
        quoted = " ".join(f'"{p}"' for p in packages.split())
        return self._run_pip(f'uninstall -y {quoted}')

    def _upgrade_packages(self, packages: str) -> ToolResult:
        """Upgrade Python packages to the latest version."""
        bad = _first_invalid_package(packages)
        if bad:
            return ToolResult(f"Error: invalid package name '{bad}'", error=True)
        quoted = " ".join(f'"{p}"' for p in packages.split())
        mirror = self._get_pip_mirror()
        mirror_arg = f'-i {mirror} ' if mirror else ''
        return self._run_pip(f'install --upgrade {mirror_arg}{quoted}')

    def _check_packages(self, packages: str) -> ToolResult:
        """Check if packages are installed and show their versions."""
        python_exe = _resolve_python_exe()
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
        python_exe = _resolve_python_exe()

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
        pip_version = self._run_bash(f'"{_resolve_python_exe()}" -m pip --version', timeout=10)

        if not result.error:
            output = result.output
            if not pip_version.error:
                output = f"pip: {pip_version.output.strip()}\n\n{output}"
            return ToolResult(output)
        return result

