# -*- coding: utf-8 -*-
"""静态证据提取测试（原 pwsh/bash 危险命令拦截转为判定证据，设计 §3.3.2）。

三类场景：
1. 原 deny/ask 用例的命中项以 static_findings 形式被识别（不再断言工具层拒绝）
2. 绕过形态：参数顺序/别名缩写/正斜杠/环境变量目标 仍被识别
3. 字符串字面量中的关键字不再触发；$(...)/`...` 子表达式仍参与扫描
"""

from core.tools.bash import BashTool
from core.tools.pwsh import PwshTool


def _bash_findings(cmd: str):
    return BashTool.collect_command_findings(cmd)


def _pwsh_findings(cmd: str):
    return PwshTool.collect_command_findings(cmd)


def _ids(findings):
    return {f["id"] for f in findings}


class TestBashFindings:
    def test_destructive(self):
        for cmd in ["rm -rf /", "rm -rf *", "rm -rf ~", "format C:",
                    "dd if=/dev/zero of=/dev/sda", "mkfs.ext4 /dev/sda1",
                    "shutdown -h now", "reboot", ":(){ :|:& };:", "> /dev/sda"]:
            assert "system-destructive" in _ids(_bash_findings(cmd)), cmd

    def test_architectural(self):
        assert "code-from-string" in _ids(_bash_findings("eval 'x'"))
        assert "cross-tool-isolation" in _ids(_bash_findings("pwsh -Command foo"))
        assert "cross-tool-isolation" in _ids(_bash_findings("powershell -Command foo"))
        assert "cross-tool-isolation" in _ids(_bash_findings("cmd /c dir"))
        assert "cross-tool-isolation" in _ids(_bash_findings("wsl ls /"))

    def test_python_invocation_allowed(self):
        """python/py 直接调用不再强制路由到 python 工具"""
        assert _bash_findings("python x.py") == []
        assert _bash_findings("python3 x.py") == []
        assert _bash_findings("pythonw x.py") == []
        assert _bash_findings("python.exe x.py") == []
        assert _bash_findings("py x.py") == []
        assert _bash_findings("py -3 x.py") == []

    def test_multiple_findings_all_returned(self):
        ids = _ids(_bash_findings("eval 'x' && wsl ls"))
        assert "code-from-string" in ids and "cross-tool-isolation" in ids

    def test_benign_no_findings(self):
        assert _bash_findings("ls -la") == []
        assert _bash_findings("git status && npm ls") == []

    def test_string_literals_not_flagged(self):
        assert _bash_findings('echo "rm -rf / works"') == []
        assert _bash_findings('echo "use python tool"') == []

    def test_subexpressions_still_scanned(self):
        assert "system-destructive" in _ids(_bash_findings('echo "$(rm -rf /)"'))
        assert "code-from-string" in _ids(_bash_findings('echo "`eval x`"'))


class TestPwshFindings:
    @staticmethod
    def _check(cmd: str):
        return _pwsh_findings(cmd)

    def test_baseline(self):
        for cmd in ["Remove-Item -Recurse -Force C:\\Users",
                    "Format-Volume -DriveLetter C",
                    "Clear-Disk -Number 0",
                    "Initialize-Disk -Number 1",
                    "Stop-Computer -Force",
                    "Restart-Computer -Force",
                    "shutdown /s /t 0",
                    "reboot",
                    "format C:",
                    "Stop-Process -Id 0",
                    "Start-Process powershell -ArgumentList foo"]:
            assert "system-destructive" in _ids(self._check(cmd)) \
                or "cross-tool-isolation" in _ids(self._check(cmd)), cmd

    def test_python_invocation_allowed(self):
        """python/py 直接调用不再强制路由到 python 工具"""
        assert self._check("python script.py") == []
        assert self._check("py -3 x.py") == []

    def test_isolation_ids(self):
        assert "code-from-string" in _ids(self._check("iex 'Get-Process'"))
        assert "code-from-string" in _ids(self._check("Invoke-Expression 'Get-Process'"))
        assert "cross-tool-isolation" in _ids(self._check("bash -c 'echo hi'"))
        assert "cross-tool-isolation" in _ids(self._check("wsl ls"))
        assert "cross-tool-isolation" in _ids(self._check('cmd /c "rd /s /q C:\\Windows"'))

    def test_recursive_force_delete_variants(self):
        for cmd in ["Remove-Item -Force -Recurse C:\\Users",
                    "Remove-Item -Recurse -Force C:/Users",
                    "rm -r -fo C:\\Users",
                    "ri -r -fo C:\\Users",
                    "del -r -fo C:\\Users",
                    "Remove-Item -rec -forc C:\\Users",
                    "Remove-Item -Recurse -Force $env:SystemRoot",
                    "Remove-Item -Recurse -Force HKLM:\\SOFTWARE"]:
            assert "system-destructive" in _ids(self._check(cmd)), cmd

    def test_string_literals_not_flagged(self):
        assert self._check('Write-Output "pwsh tool works!"') == []
        assert self._check("Write-Host 'use python tool'") == []
        assert self._check('Write-Output "run cmd /c to clean"') == []
