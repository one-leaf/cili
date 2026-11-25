"""pwsh 可执行文件解析优先级测试。

优先级：PWSH_PATH > 便携版 deps > 系统 PS7 > PATH > Windows PowerShell 5.1。
"""

import shutil

import core.tools.shell as shell_mod


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return str(path)


def _isolate(monkeypatch, tmp_path):
    """清空外部干扰，只保留测试构造的路径。"""
    monkeypatch.delenv("PWSH_PATH", raising=False)
    monkeypatch.setattr(shell_mod, "_PROJECT_ROOT", str(tmp_path / "project"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "ProgramFiles"))
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.setattr(shutil, "which", lambda name: None)


def test_env_path_wins(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    env_exe = _touch(tmp_path / "custom" / "pwsh.exe")
    _touch(tmp_path / "project" / "data" / "deps" / "pwsh" / "pwsh.exe")
    monkeypatch.setenv("PWSH_PATH", env_exe)

    assert shell_mod._find_pwsh() == env_exe


def test_deps_beats_system_ps7(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    deps_exe = _touch(tmp_path / "project" / "data" / "deps" / "pwsh" / "pwsh.exe")
    _touch(tmp_path / "ProgramFiles" / "PowerShell" / "7" / "pwsh.exe")

    assert shell_mod._find_pwsh() == deps_exe


def test_system_ps7_used_without_deps(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    ps7 = _touch(tmp_path / "ProgramFiles" / "PowerShell" / "7" / "pwsh.exe")

    assert shell_mod._find_pwsh() == ps7


def test_path_pwsh_when_no_known_install(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    which_exe = _touch(tmp_path / "store" / "pwsh.exe")
    monkeypatch.setattr(
        shutil, "which", lambda name: which_exe if name == "pwsh" else None
    )

    assert shell_mod._find_pwsh() == which_exe


def test_windows_powershell_51_fallback(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    ps51 = _touch(
        tmp_path / "Windows" / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )

    assert shell_mod._find_pwsh() == ps51


def test_last_resort_string(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    assert shell_mod._find_pwsh() == "powershell.exe"
