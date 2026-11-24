"""Chrome/Edge 进程与端口探测（浏览器服务与运维脚本共用）。"""

from __future__ import annotations

import logging
import os
import random
import signal
import socket
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

DEFAULT_CDP_PORT = 9222
_RANDOM_CDP_PORT_RANGE = (9300, 9900)
_RANDOM_CDP_PORT_TRIES = 50


class ChromeProcessRef:
    """轻量级 Chrome 进程引用，模拟 Popen 接口。

    用于记录已存在但我们没有直接启动的 Chrome 进程。
    只实现必要的方法：pid（属性）和 poll()。
    """

    def __init__(self, pid: int):
        self.pid = pid
        self._killed = False

    def poll(self) -> int | None:
        """检查进程是否还在运行。返回 None 表示运行中，退出码表示已退出。"""
        if self._killed:
            return 0
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {self.pid}"],
                    capture_output=True, text=True, timeout=5,
                )
                # 如果进程不存在，tasklist 输出会包含 "没有运行的任务" 或英文 "INFO:"
                if "没有" in result.stdout or "INFO:" in result.stdout or str(self.pid) not in result.stdout:
                    return 0
                return None
            else:
                # Linux/Mac: 检查 /proc/{pid} 是否存在
                return None if os.path.exists(f"/proc/{self.pid}") else 0
        except Exception:
            # 如果出错，假设进程还在运行（保守策略）
            return None

    def terminate(self) -> None:
        """终止进程。"""
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(self.pid)],
                               capture_output=True, timeout=5)
            else:
                os.kill(self.pid, signal.SIGTERM)
            self._killed = True
        except Exception:
            pass

    def kill(self) -> None:
        """强制杀死进程。"""
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        """等待进程退出。"""
        # 简单实现：轮询检查
        start = time.time()
        while True:
            result = self.poll()
            if result is not None:
                return result
            if timeout and (time.time() - start) > timeout:
                raise subprocess.TimeoutExpired(cmd="chrome", timeout=timeout)
            time.sleep(0.1)


def is_port_listening(port: int) -> bool:
    """检查端口是否在监听（Chrome 是否在运行）。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('localhost', port))
        sock.close()
        return result == 0
    except Exception:
        return False

def find_pid_by_port(port: int) -> int | None:
    """找到监听指定端口（LISTENING）的进程 PID（与 port_guard 同一探测口径）。"""
    from core.process_utils import find_listener_pids

    pids = find_listener_pids(port)
    return pids[0] if pids else None


def pid_uses_profile(pid: int, profile_dir: str) -> bool:
    """判断指定 PID 的进程命令行是否使用本项目 Chrome profile。"""
    try:
        from core.process_utils import get_process_command_line

        cmdline = get_process_command_line(pid)
        if not cmdline:
            return False
        if sys.platform == "win32":
            profile = os.path.abspath(profile_dir).replace("/", "\\").lower()
            return profile in cmdline.lower()
        return os.path.abspath(profile_dir) in cmdline
    except Exception:
        return False


def find_chrome_process_by_profile(profile_dir: str):
    """查找使用指定 profile 的浏览器进程（Chrome 或 Edge）。

    Returns:
        一个模拟 Popen 接口的对象（至少有 pid 和 poll() 方法），如果找不到则返回 None。
    """
    profile_abs = os.path.abspath(profile_dir)
    try:
        if sys.platform == "win32":
            # Windows: 通过 PowerShell 查找使用指定 profile 的浏览器进程
            profile_normalized = profile_abs.replace("/", "\\").lower()
            # 同时查找 chrome.exe 和 msedge.exe
            ps_result = subprocess.run(
                ["powershell", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='msedge.exe'\" | "
                 "Select-Object ProcessId,Name,CommandLine | Format-Table -AutoSize"],
                capture_output=True, text=True, timeout=10,
            )
            for line in ps_result.stdout.splitlines():
                line_lower = line.lower()
                if profile_normalized in line_lower:
                    parts = line.split()
                    if parts and parts[0].isdigit():
                        pid = int(parts[0])
                        logger.debug(f"[BrowserService] Found existing browser PID {pid}")
                        return ChromeProcessRef(pid)
        else:
            # Linux/Mac: 使用 ps 命令
            result = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if profile_abs in line and ("chrome" in line.lower() or "edge" in line.lower()):
                    parts = line.split()
                    if len(parts) > 1 and parts[1].isdigit():
                        pid = int(parts[1])
                        logger.debug(f"[BrowserService] Found existing browser PID {pid}")
                        return ChromeProcessRef(pid)
    except Exception as e:
        logger.warning(f"[BrowserService] _find_chrome_process_by_profile failed: {e}")
    return None


def remove_chrome_locks(profile_dir: str) -> None:
    """删除 Chrome 的 singleton 锁定文件，防止 profile 被锁定。"""
    lock_files = ["SingletonLock", "SingletonSocket", "SingletonCookie"]
    for filename in lock_files:
        filepath = os.path.join(profile_dir, filename)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.debug(f"[BrowserService] Removed lock file: {filename}")
        except Exception as e:
            logger.debug(f"[BrowserService] Failed to remove {filename}: {e}")


def pick_free_port(is_listening) -> int:
    """在随机区间内找一个未监听端口；失败时顺序扫描默认端口之后。"""
    for _ in range(_RANDOM_CDP_PORT_TRIES):
        port = random.randint(*_RANDOM_CDP_PORT_RANGE)
        if not is_listening(port):
            return port
    for port in range(DEFAULT_CDP_PORT + 1, DEFAULT_CDP_PORT + 101):
        if not is_listening(port):
            return port
    logger.warning("[BrowserService] 未找到空闲 CDP 端口，回退到默认端口+1")
    return DEFAULT_CDP_PORT + 1


def kill_process_tree(process) -> None:
    """终止浏览器进程（Windows 用 taskkill /T 杀进程树），失败升级到 terminate/kill。"""
    pid = process.pid
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=10,
            )
            return
        except Exception as e:
            logger.warning(f"[BrowserService] taskkill failed: {e}, trying terminate()")
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            logger.warning("[BrowserService] terminate() timeout, using kill()")
            process.kill()
    else:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("[BrowserService] terminate() timeout, using kill()")
            process.kill()


def probe_cdp(playwright, port: int) -> bool:
    """探测 CDP 端口是否可连接（连接后立即关闭）。"""
    if playwright is None:
        return False
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
        browser.close()
        return True
    except Exception:
        return False


def build_chrome_command(chrome_path: str, port: int, user_data_dir: str) -> list[str]:
    """Chrome 启动命令（远程调试端口 + 共享 profile + 无干扰参数）。"""
    return [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",  # 禁用 GPU 加速，减少问题
        "--disable-dev-shm-usage",  # 避免 /dev/shm 空间不足
    ]


def spawn_chrome(cmd: list[str], env=None):
    """启动 Chrome 子进程（Windows 隐藏控制台窗口）。"""
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return subprocess.Popen(
            cmd,
            startupinfo=startupinfo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
