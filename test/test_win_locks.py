"""win_locks 占用诊断测试（仅 Windows）。"""

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")

from core.win_locks import describe_holders  # noqa: E402


def test_detect_own_open_file(tmp_path):
    """本进程打开目录内文件时，应诊断出自己以及文件路径。"""
    target = tmp_path / "示例.txt"
    with open(target, "w", encoding="utf-8") as f:
        f.write("x")
        holders = describe_holders(tmp_path)
    assert holders, "应至少报告一个占用进程"
    assert any(f"PID {os.getpid()}" in h and "示例.txt" in h for h in holders)


def test_detect_process_cwd_inside(tmp_path):
    """子进程的工作目录在目录内时，应诊断出该子进程。"""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=str(tmp_path),
    )
    try:
        holders = []
        deadline = time.time() + 20
        while time.time() < deadline:
            holders = describe_holders(tmp_path)
            if any(f"PID {child.pid}" in h for h in holders):
                break
            time.sleep(0.5)
        assert any(f"PID {child.pid}" in h and "工作目录" in h for h in holders)
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_log_holders_only_for_lock_winerrors(monkeypatch, tmp_path):
    """只有 WinError 5/32 的 OSError 才触发占用诊断。"""
    from core import workspace_relocation as relocation

    calls = []
    monkeypatch.setattr(
        relocation, "describe_holders",
        lambda path: calls.append(path) or ["x.exe（PID 1）打开了 y"],
    )
    relocation._log_holders(tmp_path, OSError(28, "磁盘空间不足", None, None))
    assert calls == []
    relocation._log_holders(tmp_path, PermissionError(13, "拒绝访问", None, 5))
    relocation._log_holders(tmp_path, OSError(32, "共享冲突", None, 32))
    assert len(calls) == 2