"""Python 工具测试"""

import sys


class TestPythonTool:
    """Python 工具测试"""

    def test_python_execute_basic(self, tools):
        """测试 python 工具 - 基本执行"""
        from core.tools import get_tool_by_name

        python_tool = get_tool_by_name(tools, "python")
        result = python_tool.execute(
            action="execute",
            code="print('Hello from Python')"
        )
        assert not result.error
        assert "Hello from Python" in result.output

    def test_python_execute_with_import(self, tools):
        """测试 python 工具 - 带导入的执行"""
        from core.tools import get_tool_by_name

        python_tool = get_tool_by_name(tools, "python")
        code = """
import sys
print(f"Python {sys.version_info.major}.{sys.version_info.minor}")
"""
        result = python_tool.execute(action="execute", code=code)
        assert not result.error
        assert "Python" in result.output

    def test_python_info(self, tools):
        """测试 python 工具 - 环境信息"""
        from core.tools import get_tool_by_name

        python_tool = get_tool_by_name(tools, "python")
        result = python_tool.execute(action="info")
        assert not result.error
        assert "Python" in result.output
        assert "Installed packages" in result.output

    def test_python_install_package(self, tools):
        """测试 python 工具 - 安装包"""
        from core.tools import get_tool_by_name

        python_tool = get_tool_by_name(tools, "python")
        # 安装一个小包用于测试
        result = python_tool.execute(action="install", packages="six")
        assert not result.error

        # 验证包已安装
        code = "import six; print(six.__version__)"
        result = python_tool.execute(action="execute", code=code)
        assert not result.error


class TestPythonDeny:
    """python 工具 AST 静态分析：拦截动态执行与 shell 逃逸。"""

    @staticmethod
    def _check(code):
        from core.tools.python_tool import PythonTool
        return PythonTool._check_python_deny(code)

    def test_eval_blocked(self):
        assert self._check("eval('1+1')")

    def test_exec_blocked(self):
        assert self._check("exec('print(1)')")

    def test_import_blocked(self):
        assert self._check("__import__('os')")

    def test_os_system_blocked(self):
        assert self._check("import os; os.system('ls')")

    def test_os_system_alias_blocked(self):
        assert self._check("import os as o; o.system('ls')")

    def test_os_popen_blocked(self):
        assert self._check("import os; os.popen('ls')")

    def test_subprocess_shell_true_blocked(self):
        assert self._check("import subprocess; subprocess.run('ls', shell=True)")

    def test_subprocess_bash_blocked(self):
        assert self._check("import subprocess; subprocess.run(['bash', '-c', 'ls'])")

    def test_dynamic_import_concat_blocked(self):
        """字符串拼接绕过：__import__('o'+'s').system(...) 在 AST 层被 __import__ 规则拦截。"""
        assert self._check("__import__('o' + 's').system('ls')")

    def test_getattr_blocked(self):
        """getattr 动态取方法：getattr(os, 'system')('ls') 被拦截。"""
        assert self._check("import os; getattr(os, 'system')('ls')")

    def test_from_import_blocked(self):
        assert self._check("from os import system; system('ls')")

    def test_valid_code_passes(self):
        assert not self._check("import sys; print(sys.version)")

    def test_plain_subprocess_passes(self):
        """非 shell 的子进程调用（不指向 bash/pwsh）放行。"""
        assert not self._check("import subprocess; subprocess.run(['python', '-c', 'print(1)'])")

    def test_builtins_eval_blocked(self):
        """import builtins 后 builtins.eval 动态执行被拦截。"""
        assert self._check("import builtins; builtins.eval('1+1')")

    def test_builtins_alias_blocked(self):
        """builtins 别名访问动态执行被拦截。"""
        assert self._check("import builtins as b; b.exec('print(1)')")

    def test_from_builtins_eval_blocked(self):
        """from builtins import eval 后调用被拦截。"""
        assert self._check("from builtins import eval; eval('1+1')")

    def test_importlib_import_module_blocked(self):
        """importlib.import_module('os') 动态导入逃逸被拦截。"""
        assert self._check("import importlib; importlib.import_module('os').system('ls')")

    def test_importlib_alias_blocked(self):
        """importlib 别名动态导入被拦截。"""
        assert self._check("import importlib as il; il.import_module('os')")

    def test_from_importlib_import_module_blocked(self):
        """from importlib import import_module 后调用被拦截。"""
        assert self._check("from importlib import import_module; import_module('os')")

    def test_builtins_direct_blocked(self):
        """无需 import 的 __builtins__ 全局访问动态执行被拦截。"""
        assert self._check("__builtins__.eval('1+1')")

    def test_getattr_builtins_blocked(self):
        """getattr(__builtins__, 'eval') 动态取函数被拦截。"""
        assert self._check("getattr(__builtins__, 'eval')('1+1')")

    def test_globals_dict_call_blocked(self):
        """globals()['eval'](...) 经全局字典动态取函数被拦截。"""
        assert self._check("globals()['eval']('1+1')")

    def test_star_import_blocked(self):
        """from os import * 无法静态审计，被拦截。"""
        assert self._check("from os import *; system('ls')")

    def test_builtins_open_allowed(self):
        """builtins.open 是正常文件操作，不应被拦截。"""
        assert not self._check("import builtins; builtins.open('f.txt')")

    def test_syntax_error_reported(self):
        assert "语法错误" in (self._check("def broken(") or "")
