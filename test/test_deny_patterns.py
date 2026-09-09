# -*- coding: utf-8 -*-
"""pwsh/bash 危险命令拦截测试。

三类场景：
1. 基线：原有拦截行为不回退
2. 绕过封堵：参数顺序/别名缩写/正斜杠/环境变量目标/cmd/wsl/py/diskpart/iex
3. 误拦修复：字符串字面量中的关键字不再触发拦截；$(...)/`...` 子表达式仍参与扫描
"""

from core.tools.shared.bash import BashTool
from core.tools.shared.pwsh import PwshTool


def _assert_blocked(check, cmd: str):
    reason = check(cmd)
    assert reason is not None, f"should be blocked: {cmd!r}"


def _assert_allowed(check, cmd: str):
    reason = check(cmd)
    assert reason is None, f"should pass but blocked by {reason!r}: {cmd!r}"


class TestPwshDeny:
    """pwsh 工具拦截策略。"""

    @staticmethod
    def _check(cmd: str):
        return PwshTool._check_deny_patterns(cmd)

    def test_baseline_still_blocked(self):
        """原有拦截行为不回退。"""
        for cmd in [
            "Remove-Item -Recurse -Force C:\\Users",
            "Format-Volume -DriveLetter C",
            "Clear-Disk -Number 0",
            "Initialize-Disk -Number 1",
            "Stop-Computer -Force",
            "Restart-Computer -Force",
            "shutdown /s /t 0",
            "reboot",
            "format C:",
            "python script.py",
            "bash -c 'echo hi'",
            "Start-Process powershell -ArgumentList foo",
        ]:
            _assert_blocked(self._check, cmd)

    def test_recursive_force_delete_order_independent(self):
        """参数顺序无关：-Force 在 -Recurse 之前也拦截。"""
        _assert_blocked(self._check, "Remove-Item -Force -Recurse C:\\Users")

    def test_recursive_force_delete_forward_slash(self):
        """正斜杠路径也拦截。"""
        _assert_blocked(self._check, "Remove-Item -Recurse -Force C:/Users")

    def test_recursive_force_delete_aliases(self):
        """Remove-Item 全部别名（含参数缩写）都拦截。"""
        for cmd in [
            "rm -r -fo C:\\Users",
            "ri -r -fo C:\\Users",
            "del -r -fo C:\\Users",
            "erase -r -fo C:\\Users",
            "rd -r -fo C:\\Users",
            "rmdir -r -fo C:\\Users",
            "Remove-Item -r -fo C:\\Users",
            "Remove-Item -rec -forc C:\\Users",
        ]:
            _assert_blocked(self._check, cmd)

    def test_recursive_force_delete_env_target(self):
        """环境变量目标也拦截。"""
        _assert_blocked(self._check, "Remove-Item -Recurse -Force $env:SystemRoot")

    def test_recursive_force_delete_registry(self):
        """注册表 HKLM 递归强删也拦截。"""
        _assert_blocked(self._check, "Remove-Item -Recurse -Force HKLM:\\SOFTWARE")

    def test_new_isolation_rules(self):
        """cmd/wsl/diskpart/py/pythonw/sh/iex 隔离。"""
        for cmd in [
            'cmd /c "rd /s /q C:\\Windows"',
            "cmd.exe /c ver",
            "wsl rm -rf /mnt/c/Users",
            "diskpart /s wipe.txt",
            "py -3 script.py",
            "pythonw.exe script.py",
            "sh -c 'echo hi'",
            "iex 'Get-Process'",
            "Invoke-Expression 'Get-Process'",
        ]:
            _assert_blocked(self._check, cmd)

    def test_string_literals_not_blocked(self):
        """字符串字面量中的关键字不再误拦。"""
        for cmd in [
            'Write-Output "pwsh tool works!"',
            'Write-Host "use python tool"',
            "Write-Host 'powershell is great'",
            'Write-Output "run cmd /c to clean"',
        ]:
            _assert_allowed(self._check, cmd)

    def test_subexpressions_still_scanned(self):
        """双引号内的 $(...) 会执行，保留扫描。"""
        _assert_blocked(self._check, 'Write-Output "$(Remove-Item -r -fo C:\\Windows)"')
        _assert_allowed(self._check, 'Write-Output "$(Get-Date)"')

    def test_legit_commands_pass(self):
        """正常运维命令不受影响。"""
        for cmd in [
            "Remove-Item -Recurse -Force .\\build",
            "Remove-Item -Recurse C:\\temp\\build",
            "Remove-Item -Force C:\\temp\\file.txt",
            "pip install requests",
            "Get-Process | Sort-Object CPU -Descending",
            "git status",
        ]:
            _assert_allowed(self._check, cmd)


class TestBashDeny:
    """bash 工具拦截策略。"""

    @staticmethod
    def _check(cmd: str):
        return BashTool._check_deny_patterns(cmd)

    def test_baseline_still_blocked(self):
        """原有拦截行为不回退。"""
        for cmd in [
            "rm -rf /",
            "rm -rf *",
            "rm -rf ~",
            "format C:",
            "dd if=/dev/zero of=/dev/sda",
            "mkfs.ext4 /dev/sda1",
            "shutdown -h now",
            "reboot",
            ":(){ :|:& };:",
            "cd .. && rm -rf x",
            "> /dev/sda",
            "python script.py",
            "pwsh -Command foo",
        ]:
            _assert_blocked(self._check, cmd)

    def test_new_isolation_rules(self):
        """cmd/wsl/py 隔离。"""
        for cmd in [
            'cmd /c "rd /s /q C:\\Windows"',
            "wsl ls /mnt/c",
            "py -3 script.py",
        ]:
            _assert_blocked(self._check, cmd)

    def test_eval_blocked(self):
        """eval 从字符串执行代码，拦截。"""
        _assert_blocked(self._check, "eval 'rm -rf /'")

    def test_string_literals_not_blocked(self):
        """字符串字面量中的关键字不再误拦。"""
        for cmd in [
            'echo "use pwsh instead"',
            'echo "run cmd here"',
        ]:
            _assert_allowed(self._check, cmd)

    def test_subexpressions_still_scanned(self):
        """双引号内的 $(...) 和 `...` 会执行，保留扫描。"""
        _assert_blocked(self._check, 'echo "$(rm -rf /)"')
        _assert_blocked(self._check, 'echo "`rm -rf /`"')
        _assert_allowed(self._check, 'echo "today is $(date)"')

    def test_legit_commands_pass(self):
        """正常命令不受影响。"""
        for cmd in [
            "ls -la",
            "pip install pyyaml",
            "git commit -m 'fix bug'",
        ]:
            _assert_allowed(self._check, cmd)
