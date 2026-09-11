"""Browser Service - 全局浏览器服务，管理唯一的 Playwright 实例和 Chrome 进程。

设计背景：
- Playwright Sync API 在同一进程内只允许一个实例运行
- 将 Playwright 管理集中到服务层，工具层只负责调用
- 遵循 CronScheduler 的模块级单例模式
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import queue
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import urlparse

from core.config import PROJECT_ROOT
from core.tools.base import ToolResult, UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END

logger = logging.getLogger(__name__)

# Default Chrome DevTools Protocol port
DEFAULT_CDP_PORT = 9222

# Tab idle timeout: auto-close tabs inactive for this many seconds (10 minutes)
TAB_IDLE_TIMEOUT = 600

# Try to import stealth
try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

# navigate 仅允许 http/https scheme（拒绝 file://、data:、javascript: 等）
_NAVIGATE_SAFE_SCHEMES = frozenset({"http", "https"})


def _validate_navigate_url(url: str) -> str | None:
    """校验浏览器导航 URL（A30/SEC-17）。

    Returns:
        None 表示可导航；否则返回拒绝原因（供 ToolResult error 展示）。

    策略：
    - scheme 白名单：仅 http/https，硬拒绝 file://、data:、javascript: 等
    - 私网/环回/链路本地/保留地址拦截（SSRF 防护）：字面 IP 判定 + localhost 特判。
      配合网页内容提示注入，防止恶意网页诱导浏览器读取本地文件或探测内网。
    """
    parsed = urlparse(url)
    if parsed.scheme not in _NAVIGATE_SAFE_SCHEMES:
        scheme = parsed.scheme or "(空)"
        return f"URL scheme '{scheme}' 不允许，仅支持 http/https"
    host = parsed.hostname
    if not host:
        return "URL 缺少主机名"
    if host.lower() == "localhost":
        return "localhost（环回地址）不允许导航（SSRF 防护）"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None  # 域名，交由浏览器 DNS 解析（无法静态预判，不拦截）
    if not addr.is_global:
        return (
            f"{host} 不是公网地址（环回/私网/链路本地等），不允许导航（SSRF 防护）"
        )
    return None


# aria_snapshot YAML 行解析（ref 定位模型）
# 行形态：
#   - banner:                 容器（无 name）
#   - button "Sign in"        带名元素（可带内联属性如 [level=1] 或子节点冒号）
#   - paragraph: some text    文本形式（role: content）
#   - /url: /home             属性行（保留显示，不分配 ref）
_SNAP_EL_RE = re.compile(
    r'^(\s*)-\s+(\w+)(?:\s+"((?:[^"\\]|\\.)*)")?(?:\s+\[([^\]]*)\])?:?\s*$'
)
_SNAP_TEXT_RE = re.compile(r"^(\s*)-\s+(\w+):\s+(.+?)\s*$")

# 单次 snapshot 最多分配的 ref 数（超出的元素保留显示但不给 ref）
SNAPSHOT_MAX_REFS = 400

# 每 tab 的 console/request 缓冲上限
BUFFER_MAXLEN = 200


def _unescape_quoted(s: str) -> str:
    """反转义 aria_snapshot 引号 name 中的 \" 和 \\。"""
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _parse_aria_snapshot(yaml_text: str, max_refs: int = SNAPSHOT_MAX_REFS):
    """解析 aria_snapshot YAML，为可交互元素分配 ref。

    Returns:
        lines: list[tuple[str, str]] — (indent, 带 ref 标注的重建行)
        ref_map: dict[str, dict] — ref → {"role", "name", "index", "mode"}
            index: 同 (role, name) 元素出现次序（0 起），用于 get_by_role().nth()
            mode: "role" 用 get_by_role 定位，"text" 用 get_by_text 定位
    """
    ref_map: dict[str, dict] = {}
    lines_out: list[tuple[str, str]] = []
    seen: dict[tuple, int] = {}
    ref_counter = 0
    for line in yaml_text.splitlines():
        if not line.strip():
            continue
        m = _SNAP_EL_RE.match(line)
        if m:
            indent, role, name, _attrs = m.groups()
            if name:
                key = (role, name)
                idx = seen.get(key, 0)
                seen[key] = idx + 1
                if ref_counter < max_refs:
                    ref_counter += 1
                    ref = f"r{ref_counter}"
                    ref_map[ref] = {
                        "role": role,
                        "name": _unescape_quoted(name),
                        "index": idx,
                        "mode": "role",
                    }
                    line = line.rstrip() + f" [ref={ref}]"
            lines_out.append((indent, line))
            continue
        m = _SNAP_TEXT_RE.match(line)
        if m:
            indent, role, text = m.groups()
            key = (role, text)
            idx = seen.get(key, 0)
            seen[key] = idx + 1
            if ref_counter < max_refs:
                ref_counter += 1
                ref = f"r{ref_counter}"
                ref_map[ref] = {
                    "role": role,
                    "name": text,
                    "index": idx,
                    "mode": "text",
                }
                line = line.rstrip() + f" [ref={ref}]"
            lines_out.append((indent, line))
            continue
        # 属性行（/url 等）原样保留，不分配 ref
        lines_out.append(("", line))
    return lines_out, ref_map


class _ChromeProcessRef:
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
        import time
        start = time.time()
        while True:
            result = self.poll()
            if result is not None:
                return result
            if timeout and (time.time() - start) > timeout:
                raise subprocess.TimeoutExpired(cmd="chrome", timeout=timeout)
            time.sleep(0.1)




class BrowserService:
    """全局浏览器服务 — 管理唯一的 Playwright 实例和 Chrome 进程。

    生命周期：
    - start(): 设置标志位（Playwright 延迟到首次操作时启动）
    - stop(): 完整清理（断连接 -> 停 Playwright -> 杀 Chrome）
    - disconnect(): 仅断连接，保持 Chrome 运行

    线程安全：所有公共操作通过专用线程执行，避免 greenlet 线程绑定问题
    """

    def __init__(self):
        # Playwright 连接（全局唯一）
        self._playwright = None
        self._browser = None
        self._context = None

        # Tab 池：tab_index → (page, 最后活动时间戳)
        self._page_pool: dict[int, tuple] = {}
        self._next_tab_index = 1
        self._active_tab_index: int | None = None

        # Chrome 进程（全局唯一）
        self._chrome_process: subprocess.Popen | None = None

        # CDP 调试端口（默认 9222；被外部进程/非本项目 Chrome 占用时切换到随机端口）
        self._cdp_port: int = DEFAULT_CDP_PORT

        # 线程安全
        self._lock = threading.Lock()

        # 状态
        self._running = False

        # 项目路径
        self._project_root = PROJECT_ROOT

        # Chrome profile 目录（缓存避免重复拼接）
        self._chrome_profile_dir = os.path.join(self._project_root, "data", "deps", "browser")

        # 环境变量（用于 Chrome 启动）
        self._env = os.environ.copy()

        # 专用工作线程（所有 Playwright 操作在此线程执行）
        self._worker_thread: threading.Thread | None = None
        self._task_queue: queue.Queue = queue.Queue()
        # 每次 _run_in_worker 分配独立结果槽位（task_id → Event / 结果），
        # 多线程并发调用互不串扰（禁止共享单槽，否则结果会串）。
        self._task_lock = threading.Lock()
        self._task_events: dict[int, threading.Event] = {}
        self._task_results: dict[int, tuple[bool, Any]] = {}
        self._next_task_id_counter = 0

        # ref 定位：tab_index → {ref: {role, name, index, mode}}
        self._snapshot_refs: dict[int, dict[str, dict]] = {}
        # console / 网络请求缓冲：tab_index → deque[(type, text)] / deque[(method, url, status)]
        self._console_buffers: dict[int, deque] = {}
        self._request_buffers: dict[int, deque] = {}
        # 已挂监听器的 page（按 id(page) 去重）
        self._listener_pages: set[int] = set()

    # ==================== 生命周期方法 ====================

    def _start_worker_thread(self) -> None:
        """启动专用工作线程。"""
        if self._worker_thread and self._worker_thread.is_alive():
            return

        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="BrowserService-Worker",
            daemon=True
        )
        self._worker_thread.start()
        logger.debug("[BrowserService] Worker thread started")

    def _worker_loop(self) -> None:
        """工作线程主循环，处理任务队列中的任务。

        每次执行任务前，自动清理超时的 tab（> TAB_IDLE_TIMEOUT 无活动）。
        """
        while True:
            try:
                item = self._task_queue.get()
                if item is None or (isinstance(item, tuple) and item and item[0] is None):
                    break  # 退出信号

                task_id, task_func, task_args, task_kwargs = item

                # Lazy cleanup: close tabs idle for > TAB_IDLE_TIMEOUT
                try:
                    self._cleanup_expired_tabs()
                except Exception as e:
                    logger.debug(f"[BrowserService] Cleanup error (non-fatal): {e}")

                try:
                    result = task_func(*task_args, **task_kwargs)
                    task_result = (True, result)
                except Exception as e:
                    logger.warning(f"[BrowserService] Worker task error: {e}")
                    task_result = (False, e)

                # 存储结果并通知对应调用方；若调用方已超时放弃则不存（避免泄漏）
                with self._task_lock:
                    event = self._task_events.pop(task_id, None)
                    if event is not None:
                        self._task_results[task_id] = task_result
                if event is not None:
                    event.set()
            except Exception as e:
                logger.error(f"[BrowserService] Worker loop error: {e}", exc_info=True)

    def _next_task_id(self) -> int:
        """分配递增任务 ID（线程安全）。"""
        with self._task_lock:
            self._next_task_id_counter += 1
            return self._next_task_id_counter

    def _run_in_worker(self, func, *args, **kwargs):
        """在专用工作线程中执行函数。

        每次调用分配独立的结果槽位（Event + 结果），多线程并发调用互不串扰。
        注意：此方法不能在工作线程内部调用，否则会死锁！
        """
        # 检查是否在工作线程内部调用
        if threading.current_thread() == self._worker_thread:
            # 直接执行，不通过队列
            return func(*args, **kwargs)

        # 确保工作线程已启动
        self._start_worker_thread()

        # 为本次调用分配独立结果槽位
        task_id = self._next_task_id()
        event = threading.Event()
        with self._task_lock:
            self._task_events[task_id] = event

        # 提交任务到队列
        self._task_queue.put((task_id, func, args, kwargs))

        # 等待结果
        if not event.wait(timeout=60):
            with self._task_lock:
                self._task_events.pop(task_id, None)
            raise TimeoutError("Task execution timeout (60s)")

        with self._task_lock:
            success, result = self._task_results.pop(task_id)
        if not success:
            raise result
        return result

    def _ensure_playwright(self) -> bool:
        """确保 Playwright 已启动（延迟初始化）。"""
        if self._playwright is not None:
            return True

        def _start_playwright():
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._running = True
            logger.debug("[BrowserService] Playwright started successfully")
            return True

        try:
            return self._run_in_worker(_start_playwright)
        except Exception as e:
            logger.error(f"[BrowserService] Failed to start Playwright: {e}")
            return False

    def start(self) -> None:
        """启动 Playwright 服务（兼容旧调用）。

        注意：此方法现在只是设置标志位，Playwright 会在首次操作时延迟启动。
        """
        self._running = True

    def stop(self) -> None:
        """完整清理：断连接 -> 停 Playwright -> 杀 Chrome。"""
        with self._lock:
            def _cleanup():
                self._disconnect_internal()

                if self._playwright:
                    try:
                        self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None

                self._kill_chrome_internal()
                self._running = False
                logger.debug("[BrowserService] Service stopped")
                return None

            try:
                self._run_in_worker(_cleanup)
            except Exception as e:
                logger.warning(f"[BrowserService] Error during cleanup: {e}")

            # 停止工作线程
            if self._worker_thread:
                self._task_queue.put(None)  # 退出信号
                self._worker_thread.join(timeout=5)
                self._worker_thread = None

    def disconnect(self) -> None:
        """断开浏览器连接，但保持 Chrome 进程运行。

        下次操作时会重新连接并复用现有的 Chrome 和 tab。
        """
        with self._lock:
            def _disconnect():
                self._disconnect_internal()
                return None

            try:
                self._run_in_worker(_disconnect)
            except Exception as e:
                logger.warning(f"[BrowserService] Error during disconnect: {e}")

    def is_running(self) -> bool:
        """检查服务是否可用。

        返回 True 表示服务实例存在（Playwright 会在首次操作时启动）。
        """
        return self._running

    # ==================== 内部方法 ====================

    def _disconnect_internal(self) -> None:
        """内部方法：断开连接（不持锁）。"""
        # 关闭池中所有 page
        for tab_index, (page, _) in list(self._page_pool.items()):
            try:
                page.close()
            except Exception:
                pass
        self._page_pool.clear()
        self._active_tab_index = None

        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass

        # 重置所有连接相关引用
        self._context = None
        self._browser = None

    def _find_browser(self) -> str | None:
        """获取配置的浏览器可执行文件路径。"""
        from core.config import load_config

        try:
            config = load_config()
            browser_path = config.system.browser_path or ""
        except Exception:
            browser_path = ""

        if browser_path and os.path.exists(browser_path):
            return browser_path
        return None

    def _is_port_listening(self, port: int) -> bool:
        """检查端口是否在监听（Chrome 是否在运行）。"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('localhost', port))
            sock.close()
            return result == 0
        except Exception:
            return False

    def _pick_free_port(self) -> int:
        """在 [9300, 9900] 内找一个当前未监听的端口，作为 CDP 端口替代。"""
        for _ in range(50):
            port = random.randint(9300, 9900)
            if not self._is_port_listening(port):
                return port
        return DEFAULT_CDP_PORT + 1

    def _find_pid_by_port(self, port: int) -> int | None:
        """找到监听指定端口（LISTENING）的进程 PID。"""
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["netstat", "-ano", "-p", "tcp"],
                    capture_output=True, text=True, timeout=10,
                )
                needle = f":{port}"
                for line in result.stdout.splitlines():
                    if needle in line and "LISTENING" in line:
                        parts = line.split()
                        if parts and parts[-1].isdigit():
                            return int(parts[-1])
            else:
                result = subprocess.run(
                    ["lsof", "-ti", f"tcp:{port}", "-s", "tcp:LISTEN"],
                    capture_output=True, text=True, timeout=10,
                )
                for pid in result.stdout.splitlines():
                    if pid.isdigit():
                        return int(pid)
        except Exception as e:
            logger.debug(f"[BrowserService] _find_pid_by_port({port}) failed: {e}")
        return None

    def _pid_uses_our_profile(self, pid: int) -> bool:
        """判断指定 PID 的进程命令行是否使用本项目 Chrome profile。"""
        try:
            profile_abs = os.path.abspath(self._chrome_profile_dir)
            if sys.platform == "win32":
                profile_normalized = profile_abs.replace("/", "\\").lower()
                result = subprocess.run(
                    ["powershell", "-Command",
                     f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"],
                    capture_output=True, text=True, timeout=10,
                )
                return profile_normalized in result.stdout.lower()
            else:
                result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True, text=True, timeout=5,
                )
                return profile_abs in result.stdout
        except Exception:
            return False

    def _try_cdp_connect(self) -> bool:
        """尝试通过 CDP 连接到 Chrome。返回 True 表示成功。"""
        if self._playwright is None:
            return False

        try:
            browser = self._playwright.chromium.connect_over_cdp(
                f"http://localhost:{self._cdp_port}"
            )
            browser.close()
            return True
        except Exception:
            return False

    def _find_chrome_process_by_profile(self) -> subprocess.Popen | None:
        """查找使用指定 profile 的浏览器进程（Chrome 或 Edge）。

        Returns:
            一个模拟 Popen 接口的对象（至少有 pid 和 poll() 方法），如果找不到则返回 None。
        """
        profile_abs = os.path.abspath(self._chrome_profile_dir)
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
                            return _ChromeProcessRef(pid)
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
                            return _ChromeProcessRef(pid)
        except Exception as e:
            logger.warning(f"[BrowserService] _find_chrome_process_by_profile failed: {e}")
        return None

    def _start_chrome(self, port: int) -> tuple[bool, str]:
        """启动 Chrome 进程（带远程调试端口）。

        Returns:
            tuple[bool, str]: (成功标志, 错误信息)
        """
        # 检查 Chrome 是否已在运行
        if self._chrome_process is not None:
            if self._chrome_process.poll() is None:
                # 进程还在运行
                return True, ""
            else:
                # 进程已退出，清理引用
                logger.debug(f"[BrowserService] Chrome process exited with code {self._chrome_process.returncode}")
                self._chrome_process = None

        # 即使 _chrome_process 为 None，端口可能仍在监听
        # （例如：之前启动但引用丢失的 Chrome，或用户自己的 Chrome 恰好占用 9222）
        if self._is_port_listening(port):
            # 端口在监听 - 验证 CDP 是否可用
            if self._try_cdp_connect():
                # 尝试找到并记录这个 Chrome 进程的 PID
                self._chrome_process = self._find_chrome_process_by_profile()
                if self._chrome_process:
                    logger.debug(f"[BrowserService] Reconnected to existing Chrome PID {self._chrome_process.pid}")
                    return True, ""
                # W12: 端口上是非本项目 profile 的 Chrome（很可能是用户个人 Chrome）。
                # 不共享其登录态/cookies，切换到随机端口启动本项目自己的 Chrome。
                logger.warning(
                    f"[BrowserService] Port {port} is occupied by a non-project Chrome "
                    f"(foreign profile), switching to a random port"
                )
                self._cdp_port = self._pick_free_port()
                port = self._cdp_port
            else:
                # W11: CDP 连接失败，端口被占用。找到占用进程的 PID；
                # 仅当确认是本项目 profile 的 Chrome 才 kill（避免误杀用户浏览器/其他程序）。
                if self._chrome_process is None:
                    pid = self._find_pid_by_port(port)
                    if pid is not None and self._pid_uses_our_profile(pid):
                        self._chrome_process = _ChromeProcessRef(pid)
                if self._chrome_process:
                    logger.warning(
                        f"[BrowserService] Port {port} is listening but CDP connection failed, "
                        f"killing stale Chrome PID {self._chrome_process.pid}"
                    )
                    self._kill_chrome_internal()
                    # 等待端口释放
                    for i in range(20):
                        time.sleep(0.5)
                        if not self._is_port_listening(port):
                            logger.debug(f"[BrowserService] Port {port} released after {(i+1)*0.5:.1f}s")
                            break
                    else:
                        return False, f"Port {port} still in use after killing stale Chrome (waited 10s)"
                else:
                    # 占用端口的不是本项目 Chrome（其他程序/用户浏览器），不误杀，改用随机端口
                    logger.warning(
                        f"[BrowserService] Port {port} is occupied by a foreign process, "
                        f"switching to a random port"
                    )
                    self._cdp_port = self._pick_free_port()
                    port = self._cdp_port

        chrome_path = self._find_browser()
        if not chrome_path:
            return False, "Chrome executable not found"

        try:
            # 使用 data/deps/browser/ 作为共享的 Chrome profile
            user_data_dir = os.path.join(self._project_root, "data", "deps", "browser")
            os.makedirs(user_data_dir, exist_ok=True)

            # 启动前清理锁文件
            self._remove_chrome_locks(user_data_dir)

            # 启动 Chrome，带远程调试端口
            cmd = [
                chrome_path,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",  # 禁用 GPU 加速，减少问题
                "--disable-dev-shm-usage",  # 避免 /dev/shm 空间不足
            ]

            logger.debug(f"[BrowserService] Starting Chrome: {chrome_path}")
            logger.debug(f"[BrowserService] Profile: {user_data_dir}, Port: {port}")
            logger.debug(f"[BrowserService] Command: {' '.join(cmd)}")

            if sys.platform == "win32":
                # Windows: 使用 STARTUPINFO 隐藏控制台窗口
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                self._chrome_process = subprocess.Popen(
                    cmd,
                    startupinfo=startupinfo,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=self._env,
                )
            else:
                self._chrome_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=self._env,
                )

            logger.debug(f"[BrowserService] Chrome started, PID: {self._chrome_process.pid}")

            # 等待 Chrome 启动并监听 CDP 端口（最多 20 秒）
            for i in range(20):
                time.sleep(1)

                # 先检查进程是否还活着
                if self._chrome_process.poll() is not None:
                    return False, f"Chrome exited immediately with code {self._chrome_process.returncode}"

                # 尝试 CDP 连接
                if self._try_cdp_connect():
                    logger.debug(f"[BrowserService] Chrome CDP connected after {i+1}s")
                    return True, ""

                # 每秒输出进度
                if i % 5 == 4:
                    logger.debug(f"[BrowserService] Waiting for CDP... {i+1}s")

            # 超时 - 检查进程状态
            if self._chrome_process.poll() is not None:
                return False, f"Chrome exited with code {self._chrome_process.returncode} after timeout"
            else:
                return False, f"Chrome running (PID {self._chrome_process.pid}) but CDP not ready after 20s"

        except Exception as e:
            return False, f"Failed to start Chrome: {e}"

    def _kill_chrome_internal(self) -> None:
        """内部方法：终止 Chrome 进程（不持锁）。

        只杀死 self._chrome_process 记录的进程，不会影响其他 cili 实例的进程。
        """
        if not self._chrome_process:
            # 没有我们自己的进程，只清理锁文件并等待端口释放
            self._remove_chrome_locks(self._chrome_profile_dir)
            return

        try:
            pid = self._chrome_process.pid
            logger.debug(f"[BrowserService] Terminating Chrome PID {pid}")

            # Windows: 使用 taskkill /T 杀死进程树（包括子进程）
            if sys.platform == "win32":
                try:
                    result = subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(pid)],
                        capture_output=True, text=True, timeout=10
                    )
                    logger.debug(f"[BrowserService] taskkill result: {result.returncode}")
                except Exception as e:
                    logger.warning(f"[BrowserService] taskkill failed: {e}, trying terminate()")
                    self._chrome_process.terminate()
                    try:
                        self._chrome_process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        logger.warning(f"[BrowserService] terminate() timeout, using kill()")
                        self._chrome_process.kill()
            else:
                self._chrome_process.terminate()
                try:
                    self._chrome_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning(f"[BrowserService] terminate() timeout, using kill()")
                    self._chrome_process.kill()

            logger.debug(f"[BrowserService] Chrome PID {pid} killed")
        except Exception as e:
            logger.warning(f"[BrowserService] Error killing Chrome: {e}")
        finally:
            self._chrome_process = None

        # 删除 Chrome 的 singleton 锁定文件
        self._remove_chrome_locks(self._chrome_profile_dir)

        # 等待端口释放
        for i in range(20):
            time.sleep(0.5)
            if not self._is_port_listening(self._cdp_port):
                logger.debug(f"[BrowserService] Port {self._cdp_port} released after {(i+1)*0.5:.1f}s")
                break
        else:
            logger.warning(f"[BrowserService] Port {self._cdp_port} still listening after 10s")

    def _remove_chrome_locks(self, profile_dir: str) -> None:
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

    def _connect_browser(self) -> bool:
        """尝试通过 CDP 连接到 Chrome。返回 True 表示成功。"""
        if self._playwright is None:
            return False

        def _do_connect():
            # 先关闭旧的 browser 对象（如果存在）
            if self._browser:
                try:
                    self._browser.close()
                except Exception:
                    pass
                self._browser = None
                # 重置 context（page 池由 _disconnect_internal 清理）
                self._context = None

            self._browser = self._playwright.chromium.connect_over_cdp(
                f"http://localhost:{self._cdp_port}"
            )
            logger.debug(f"[BrowserService] CDP connected successfully")
            return True

        try:
            return self._run_in_worker(_do_connect)
        except Exception as e:
            logger.debug(f"[BrowserService] CDP connect failed: {e}")
            self._browser = None
            return False

    def _ensure_connected(self) -> ToolResult | None:
        """确保浏览器已连接，必要时启动 Chrome。失败时返回错误 ToolResult。"""
        # 确保 Playwright 已启动（延迟初始化）
        if not self._ensure_playwright():
            return ToolResult(
                "Error: Failed to start Playwright. "
                "Make sure playwright is installed: pip install playwright\n"
                "Try: pip install playwright && playwright install chromium",
                error=True,
            )

        # 检查现有的 browser 对象是否还有效（在工作线程中检查）
        def _check_connection():
            if self._browser:
                try:
                    return self._browser.is_connected()
                except Exception:
                    return False
            return False

        try:
            is_valid = self._run_in_worker(_check_connection)
            if is_valid:
                logger.debug(f"[BrowserService] Existing browser connection is valid")
                return None  # 已有有效连接
            else:
                logger.debug(f"[BrowserService] Existing browser connection invalid, reconnecting...")
                # 连接已失效，清理后重新连接
                self._context = None
                self._browser = None
        except Exception as e:
            logger.warning(f"[BrowserService] Error checking connection: {e}")
            self._context = None
            self._browser = None

        # 尝试连接到现有的 Chrome
        if self._connect_browser():
            return None  # 已连接

        # 连接失败 - 检查 Chrome 是否已在运行
        if self._is_port_listening(self._cdp_port):
            logger.debug(f"[BrowserService] Port {self._cdp_port} listening but CDP failed, retrying...")
            # Chrome 在运行但连不上 - 重试连接
            for attempt in range(3):
                time.sleep(0.5)
                logger.debug(f"[BrowserService] CDP retry attempt {attempt + 1}/3")
                if self._connect_browser():
                    return None
            # 全部失败，杀死并重启
            logger.warning(f"[BrowserService] CDP retry failed, restarting Chrome")
            self._kill_chrome_internal()
            # _kill_chrome_internal 已等待端口释放并清理锁文件
        else:
            # 没有 Chrome 在运行，先清理可能残留的锁文件
            self._remove_chrome_locks(self._chrome_profile_dir)

        # 没有 Chrome 在运行（或已杀死旧进程）- 启动新的
        success, error_msg = self._start_chrome(self._cdp_port)
        if not success:
            chrome_path = self._find_browser()
            return ToolResult(
                f"Error: Failed to start/connect Chrome.\n"
                f"Detail: {error_msg}\n"
                f"Chrome path: {chrome_path}\n"
                f"Profile: {self._chrome_profile_dir}\n"
                f"Port: {self._cdp_port}\n"
                f"Playwright: {'OK' if self._playwright else 'NOT STARTED'}\n"
                f"Chrome process: {self._chrome_process.pid if self._chrome_process else 'None'}\n"
                f"Port listening: {self._is_port_listening(self._cdp_port)}",
                error=True,
            )

        # 再次尝试连接
        if not self._connect_browser():
            return ToolResult(
                f"Error: Chrome started but CDP connection failed.\n"
                f"Port: {self._cdp_port}\n"
                f"Chrome PID: {self._chrome_process.pid if self._chrome_process else 'None'}\n"
                f"Chrome alive: {self._chrome_process.poll() is None if self._chrome_process else False}\n"
                f"Port listening: {self._is_port_listening(self._cdp_port)}",
                error=True,
            )

        return None  # 成功

    def _get_page(self, tab_index: int | None = None) -> "Page | None":
        """获取指定 tab_index 的 page，或当前活跃 page。

        Args:
            tab_index: 指定 tab 编号，None 表示使用活跃 tab

        Returns:
            Page 对象，无效时返回 None
        """
        if tab_index is not None:
            entry = self._page_pool.get(tab_index)
            if entry:
                page, _ = entry
                try:
                    if not page.is_closed():
                        return page
                except Exception:
                    pass
            return None

        # 使用活跃 tab
        if self._active_tab_index is not None:
            entry = self._page_pool.get(self._active_tab_index)
            if entry:
                page, _ = entry
                try:
                    if not page.is_closed():
                        return page
                except Exception:
                    pass
        return None

    def _ensure_page(self, tab_index: int | None = None) -> int:
        """确保有可用的 page（tab），返回 tab_index。

        策略：
        - 如果指定了 tab_index，切换到该 tab（无效则报错）
        - 如果 _active_tab_index 有效，复用它
        - 否则创建新 tab

        Returns:
            int: 使用的 tab_index
        """
        # 如果指定了 tab_index，检查并切换
        if tab_index is not None:
            page = self._get_page(tab_index)
            if page is None:
                raise ValueError(f"Tab {tab_index} not found or invalid")
            self._touch_page(tab_index, page)
            return tab_index

        # 使用活跃 tab
        if self._active_tab_index is not None:
            page = self._get_page(self._active_tab_index)
            if page is not None:
                self._touch_page(self._active_tab_index, page)
                return self._active_tab_index

        # 无有效 tab，创建新的
        return self._open_new_page()

    def _open_new_page(self) -> int:
        """创建新 tab，加入池并设为活跃 tab。

        Returns:
            int: 新 tab 的 index
        """
        # 获取或创建 context
        if self._context is None:
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
            else:
                self._context = self._browser.new_context()

        new_page = self._context.new_page()
        tab_index = self._next_tab_index
        self._next_tab_index += 1

        self._touch_page(tab_index, new_page)
        return tab_index

    def _ensure_listeners(self, page, tab_index: int) -> None:
        """为 page 挂上 console / response 监听器（幂等），供 console/requests 动作读取。

        监听器回调在 Playwright driver 线程运行，deque.append 是原子的，无锁安全。
        """
        if id(page) in self._listener_pages:
            return
        self._listener_pages.add(id(page))
        console_buf: deque = deque(maxlen=BUFFER_MAXLEN)
        request_buf: deque = deque(maxlen=BUFFER_MAXLEN)
        self._console_buffers[tab_index] = console_buf
        self._request_buffers[tab_index] = request_buf
        try:
            page.on("console", lambda msg: console_buf.append((msg.type, msg.text)))
            page.on(
                "response",
                lambda resp: request_buf.append(
                    (resp.request.method, resp.url, resp.status)
                ),
            )
        except Exception:
            pass

    def _find_tab_index(self, page) -> int | None:
        """根据 page 对象反查它在 tab 池中的 index。"""
        for idx, (pool_page, _ts) in self._page_pool.items():
            if pool_page is page:
                return idx
        return self._active_tab_index

    def _resolve_ref(self, tab_index: int, ref: str) -> dict:
        """把 ref 解析为定位条目；未知 ref 报错提示先跑 snapshot。"""
        ref_map = self._snapshot_refs.get(tab_index) or {}
        entry = ref_map.get(ref)
        if not entry:
            raise ValueError(
                f"Unknown ref '{ref}' for tab {tab_index}. "
                f"Run the 'snapshot' action first to get fresh refs."
            )
        return entry

    def _locator_for(self, page, entry: dict):
        """根据 ref 定位条目生成 Playwright locator。

        mode=role 用 get_by_role(role, name).nth(index)；
        mode=text（paragraph/generic 等文本节点）用 get_by_text(name).nth(index)。
        """
        if entry["mode"] == "text":
            return page.get_by_text(entry["name"], exact=True).nth(entry["index"])
        return page.get_by_role(
            entry["role"], name=entry["name"], exact=True
        ).nth(entry["index"])

    def _build_snapshot(self, page, tab_index: int) -> tuple[list[tuple[str, str]], dict]:
        """生成页面 aria snapshot 并更新该 tab 的 ref 映射。

        Returns:
            (lines, ref_map)：lines 用于文本展示，ref_map 存入 _snapshot_refs[tab_index]
        """
        yaml_text = page.locator("body").aria_snapshot()
        lines, ref_map = _parse_aria_snapshot(yaml_text)
        self._snapshot_refs[tab_index] = ref_map
        return lines, ref_map

    def _touch_page(self, tab_index: int, page) -> None:
        """更新 page 的最后活动时间，设为活跃 tab。"""
        self._page_pool[tab_index] = (page, time.time())
        self._active_tab_index = tab_index
        self._ensure_listeners(page, tab_index)

    def _cleanup_expired_tabs(self) -> None:
        """关闭超过 TAB_IDLE_TIMEOUT 无活动的 tab。

        保留活跃 tab，只回收非活跃的旧 tab。
        """
        now = time.time()
        tabs_to_close = [
            (idx, page) for idx, (page, ts) in self._page_pool.items()
            if now - ts > TAB_IDLE_TIMEOUT and idx != self._active_tab_index
        ]

        for tab_index, page in tabs_to_close:
            self._close_page_internal(tab_index, page)

        if tabs_to_close:
            logger.debug(f"[BrowserService] Closed {len(tabs_to_close)} idle tab(s), "
                        f"{len(self._page_pool)} remaining")

    def _close_page_internal(self, tab_index: int, page) -> None:
        """关闭单个 page 并从池中移除。"""
        self._page_pool.pop(tab_index, None)
        if self._active_tab_index == tab_index:
            self._active_tab_index = None
        self._listener_pages.discard(id(page))
        self._snapshot_refs.pop(tab_index, None)
        self._console_buffers.pop(tab_index, None)
        self._request_buffers.pop(tab_index, None)
        try:
            if not page.is_closed():
                page.close()
        except Exception as e:
            logger.debug(f"[BrowserService] Error closing page: {e}")

    def _apply_stealth(self) -> None:
        """应用反检测补丁，避免被网站识别为机器人。"""
        if not STEALTH_AVAILABLE:
            return
        page = self._get_page()
        if not page:
            return
        try:
            stealth_sync(page)
        except Exception:
            # Stealth 失败，继续
            pass

    # ==================== 公共操作方法（线程安全）====================

    def _execute_operation(self, operation_name: str, func, tab_index: int | None = None) -> ToolResult:
        """执行浏览器操作的通用包装。

        提供统一的错误处理和详细错误信息。

        Args:
            operation_name: 操作名称（用于错误消息）
            func: 实际操作函数（接收 page 参数）
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        def _do_operation():
            error = self._ensure_connected()
            if error:
                return error

            actual_tab_index = self._ensure_page(tab_index)
            page = self._get_page(actual_tab_index)
            if not page:
                return ToolResult(f"Browser {operation_name} failed: no valid page", error=True)

            self._apply_stealth()

            try:
                return func(page)
            except Exception as e:
                error_type = type(e).__name__
                error_msg = str(e)

                # 收集诊断信息
                page_info = "unknown"
                try:
                    page_info = f"url={page.url}, closed={page.is_closed()}"
                except Exception:
                    page_info = "error getting page info"

                browser_info = "unknown"
                if self._browser:
                    try:
                        browser_info = f"connected={self._browser.is_connected()}"
                    except Exception:
                        browser_info = "error getting browser info"

                chrome_info = "unknown"
                if self._chrome_process:
                    chrome_info = f"pid={self._chrome_process.pid}, alive={self._chrome_process.poll() is None}"
                else:
                    chrome_info = "no process"

                return ToolResult(
                    f"Browser {operation_name} failed.\n"
                    f"Error type: {error_type}\n"
                    f"Error message: {error_msg}\n\n"
                    f"Diagnostics:\n"
                    f"  Tab: {actual_tab_index}, Page: {page_info}\n"
                    f"  Browser: {browser_info}\n"
                    f"  Chrome: {chrome_info}\n"
                    f"  Tab pool: {len(self._page_pool)} page(s)\n"
                    f"  Port listening: {self._is_port_listening(self._cdp_port)}\n\n"
                    f"Suggestion: Try kill_chrome action to restart browser, then retry.",
                    error=True,
                )

        try:
            return self._run_in_worker(_do_operation)
        except Exception as e:
            return ToolResult(f"Browser {operation_name} failed with worker error: {e}", error=True)

    def navigate(self, url: str, tab_index: int | None = None) -> ToolResult:
        """导航到 URL 并返回页面文本内容。

        Args:
            url: 目标 URL
            tab_index: 指定 tab 编号，None 表示创建新 tab

        Returns:
            ToolResult，data 包含 tab_index 字段
        """
        # A30: scheme/私网过滤（SSRF 防护），先于任何浏览器操作快速失败
        block_reason = _validate_navigate_url(url)
        if block_reason:
            return ToolResult(
                f"Error: 导航被拒绝 — {block_reason}", error=True
            )
        def _do_navigate(page, current_tab_index):
            page.goto(url, wait_until="load", timeout=60000)
            # 等待 JavaScript 渲染和重定向
            time.sleep(1)
            title = page.title()

            # 提取页面文本内容（而非原始 HTML）
            try:
                text = page.inner_text("body")
            except Exception:
                # fallback: 如果 inner_text 失败（如页面结构异常），取 HTML
                text = page.content()
            if len(text) > 10000:
                text = text[:10000] + "\n... (truncated)"

            return ToolResult(
                f"Navigated to {url}\n"
                f"Title: {title}\n"
                f"Content length: {len(text)} chars\n"
                f"Tab index: {current_tab_index}\n\n"
                f"--- Page content ---\n{UNTRUSTED_DATA_BEGIN}{text}{UNTRUSTED_DATA_END}",
                meta={"tab_index": current_tab_index}
            )

        def _do_navigate_tab():
            error = self._ensure_connected()
            if error:
                return error
            # 创建新 tab 或使用指定 tab
            if tab_index is not None:
                actual_tab_index = self._ensure_page(tab_index)
            else:
                actual_tab_index = self._open_new_page()
            page = self._get_page(actual_tab_index)
            if not page:
                return ToolResult(f"Browser navigate failed: could not open tab", error=True)
            self._apply_stealth()
            return _do_navigate(page, actual_tab_index)

        try:
            return self._run_in_worker(_do_navigate_tab)
        except Exception as e:
            return ToolResult(f"Browser navigate failed with worker error: {e}", error=True)

    def screenshot(self, path: str, tab_index: int | None = None) -> ToolResult:
        """截取当前页面的截图。

        Args:
            path: 截图保存路径
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        def _do_screenshot(page):
            if not os.path.isabs(path):
                abs_path = os.path.join(self._project_root, path)
            else:
                abs_path = path
            page.screenshot(path=abs_path, full_page=True)
            filename = os.path.basename(abs_path)
            markdown_link = f"![Screenshot](/api/files/{filename})"
            return ToolResult(f"Screenshot saved to {abs_path}\n\n{markdown_link}")

        return self._execute_operation(f"screenshot({path})", _do_screenshot, tab_index=tab_index)

    def save_pdf(self, path: str, tab_index: int | None = None) -> ToolResult:
        """将当前页面保存为 PDF 文件。

        注意：PDF 导出仅对非 headless Chrome 且支持打印的页面有效。

        Args:
            path: PDF 保存路径
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        def _do_save_pdf(page):
            if not os.path.isabs(path):
                abs_path = os.path.join(self._project_root, path)
            else:
                abs_path = path
            page.pdf(path=abs_path, format="A4", print_background=True)
            return ToolResult(f"PDF saved to {abs_path}")

        return self._execute_operation(f"save_pdf({path})", _do_save_pdf, tab_index=tab_index)

    def execute_script(self, script: str, tab_index: int | None = None) -> ToolResult:
        """在页面上执行 JavaScript 代码。

        Args:
            script: JavaScript 代码
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        def _do_execute(page):
            exec_result = page.evaluate(script)
            result_str = json.dumps(exec_result, ensure_ascii=False, indent=2)
            if len(result_str) > 10000:
                result_str = result_str[:10000] + "\n... (truncated)"
            return ToolResult(f"JavaScript result:\n{result_str}")

        return self._execute_operation("execute_script", _do_execute, tab_index=tab_index)

    def wait_for(self, selector: str, timeout: int = 10000, tab_index: int | None = None) -> ToolResult:
        """等待 CSS 选择器出现。

        Args:
            selector: CSS 选择器
            timeout: 超时时间（毫秒）
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        def _do_wait(page):
            page.wait_for_selector(selector, timeout=timeout)
            return ToolResult(f"Element '{selector}' found")

        return self._execute_operation(f"wait_for({selector})", _do_wait, tab_index=tab_index)

    def get_text(self, tab_index: int | None = None) -> ToolResult:
        """获取页面的所有文本内容。

        Args:
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        def _do_get_text(page):
            text = page.inner_text("body")
            if len(text) > 20000:
                text = text[:20000] + "\n... (truncated)"
            return ToolResult(f"Page text:\n{text}")

        return self._execute_operation("get_text", _do_get_text, tab_index=tab_index)

    def get_links(self, tab_index: int | None = None) -> ToolResult:
        """获取页面上的所有链接。

        Args:
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        def _do_get_links(page):
            links = page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => ({text: a.textContent?.trim() || '', href: a.href}))
                    .filter(l => l.href && !l.href.startsWith('javascript:'))
                    .slice(0, 100)
            """)
            if not links:
                return ToolResult("No links found on page")
            lines = [f"- [{l['text'][:50]}]({l['href']})" for l in links]
            return ToolResult(f"Found {len(links)} links:\n\n" + "\n".join(lines))

        return self._execute_operation("get_links", _do_get_links, tab_index=tab_index)

    # ==================== ref 定位交互（snapshot/find/click/fill/type/press） ====================

    def snapshot(self, tab_index: int | None = None) -> ToolResult:
        """获取页面 accessibility snapshot，为可交互元素分配 ref。

        返回的 YAML 树中带 [ref=rN] 的元素可用 click/fill/type/press 定位；
        ref 仅在本次返回的 snapshot 有效，页面变化后请重新 snapshot。
        """
        def _do_snapshot(page):
            actual_tab = self._find_tab_index(page) or 0
            lines, ref_map = self._build_snapshot(page, actual_tab)
            if not lines:
                return ToolResult("Snapshot empty: no accessible elements found", error=True)
            body = "\n".join(line for _indent, line in lines)
            if len(body) > 20000:
                body = body[:20000] + "\n... (truncated)"
            return ToolResult(
                f"Accessibility snapshot (tab {actual_tab}, {len(ref_map)} refs):\n"
                f"---\n{body}\n---\n"
                f"Use ref to interact: click(ref=\"r1\"), fill(ref=\"r2\", text=\"...\"), "
                f"type(ref=\"r2\", text=\"...\"), press(ref=\"r3\", key=\"Enter\")",
                meta={"refs": ref_map},
            )

        return self._execute_operation("snapshot", _do_snapshot, tab_index=tab_index)

    def find(self, pattern: str, tab_index: int | None = None) -> ToolResult:
        """在页面 accessibility snapshot 中搜索匹配 role 或 name 的元素。

        Args:
            pattern: 正则表达式或子串（大小写不敏感），匹配 role 或 name
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        def _do_find(page):
            actual_tab = self._find_tab_index(page) or 0
            lines, ref_map = self._build_snapshot(page, actual_tab)
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error:
                rx = None

            matches: list[int] = []
            for i, (_indent, line) in enumerate(lines):
                if "[ref=" not in line:
                    continue
                if rx:
                    if rx.search(line):
                        matches.append(i)
                elif pattern.lower() in line.lower():
                    matches.append(i)

            if not matches:
                return ToolResult(
                    f"No element matches '{pattern}' in tab {actual_tab} "
                    f"({len(ref_map)} refs). Try the 'snapshot' action."
                )

            out_lines = []
            for idx in matches:
                start = max(0, idx - 1)
                end = min(len(lines), idx + 2)
                if out_lines:
                    out_lines.append("  ...")
                for j in range(start, end):
                    marker = ">" if j == idx else " "
                    out_lines.append(f"{marker} {lines[j][1]}")
            return ToolResult(
                f"Found {len(matches)} element(s) matching '{pattern}' (tab {actual_tab}):\n"
                + "\n".join(out_lines),
                meta={"refs": ref_map},
            )

        return self._execute_operation("find", _do_find, tab_index=tab_index)

    def click(self, ref: str, tab_index: int | None = None) -> ToolResult:
        """点击 snapshot 中指定 ref 的元素。"""
        def _do_click(page):
            actual_tab = self._find_tab_index(page) or 0
            entry = self._resolve_ref(actual_tab, ref)
            loc = self._locator_for(page, entry)
            loc.click(timeout=10000)
            return ToolResult(
                f"Clicked [{ref}] ({entry['role']} \"{entry['name']}\")",
                meta={"tab_index": actual_tab},
            )

        return self._execute_operation("click", _do_click, tab_index=tab_index)

    def fill(self, ref: str, text: str, tab_index: int | None = None) -> ToolResult:
        """向 snapshot 中指定 ref 的输入框填入文本（触发一次 input 事件）。"""
        def _do_fill(page):
            actual_tab = self._find_tab_index(page) or 0
            entry = self._resolve_ref(actual_tab, ref)
            loc = self._locator_for(page, entry)
            loc.fill(text, timeout=10000)
            return ToolResult(
                f"Filled [{ref}] ({entry['role']} \"{entry['name']}\") with {len(text)} chars",
                meta={"tab_index": actual_tab},
            )

        return self._execute_operation("fill", _do_fill, tab_index=tab_index)

    def type(self, ref: str, text: str, tab_index: int | None = None) -> ToolResult:
        """向 snapshot 中指定 ref 的元素逐键输入文本（触发按键级事件）。"""
        def _do_type(page):
            actual_tab = self._find_tab_index(page) or 0
            entry = self._resolve_ref(actual_tab, ref)
            loc = self._locator_for(page, entry)
            loc.press_sequentially(text, delay=20)
            return ToolResult(
                f"Typed {len(text)} chars into [{ref}] ({entry['role']} \"{entry['name']}\")",
                meta={"tab_index": actual_tab},
            )

        return self._execute_operation("type", _do_type, tab_index=tab_index)

    def press(self, ref: str | None, key: str, tab_index: int | None = None) -> ToolResult:
        """按指定键。

        Args:
            ref: snapshot 中的元素 ref；None 表示在页面全局按（page.keyboard.press）
            key: 按键名（Enter/Tab/Escape/ArrowDown/Backspace 等）
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        def _do_press(page):
            actual_tab = self._find_tab_index(page) or 0
            if ref:
                entry = self._resolve_ref(actual_tab, ref)
                loc = self._locator_for(page, entry)
                loc.press(key)
                return ToolResult(
                    f"Pressed '{key}' on [{ref}] ({entry['role']} \"{entry['name']}\")",
                    meta={"tab_index": actual_tab},
                )
            page.keyboard.press(key)
            return ToolResult(f"Pressed '{key}' (page level)", meta={"tab_index": actual_tab})

        return self._execute_operation("press", _do_press, tab_index=tab_index)

    # ==================== 导航（go_back/go_forward/reload） ====================

    def go_back(self, tab_index: int | None = None) -> ToolResult:
        """返回上一页。"""
        def _do_go_back(page):
            resp = page.go_back(wait_until="load", timeout=30000)
            return ToolResult(f"Navigated back to {resp.url}" if resp else "No history to go back to")

        return self._execute_operation("go_back", _do_go_back, tab_index=tab_index)

    def go_forward(self, tab_index: int | None = None) -> ToolResult:
        """前进到下一页。"""
        def _do_go_forward(page):
            resp = page.go_forward(wait_until="load", timeout=30000)
            return ToolResult(f"Navigated forward to {resp.url}" if resp else "No forward history")

        return self._execute_operation("go_forward", _do_go_forward, tab_index=tab_index)

    def reload(self, tab_index: int | None = None) -> ToolResult:
        """重新加载当前页面。"""
        def _do_reload(page):
            page.reload(wait_until="load", timeout=30000)
            return ToolResult(f"Reloaded page: {page.url}")

        return self._execute_operation("reload", _do_reload, tab_index=tab_index)

    # ==================== 诊断（console/requests） ====================

    def console(self, clear: bool = False, tab_index: int | None = None) -> ToolResult:
        """返回该 tab 捕获的浏览器 console 消息（自挂载以来累积）。"""
        def _do_console(page):
            actual_tab = self._find_tab_index(page) or 0
            buf = self._console_buffers.get(actual_tab)
            if not buf:
                return ToolResult("No console messages captured yet.")
            msgs = list(buf)
            if clear:
                buf.clear()
            if not msgs:
                return ToolResult("No console messages captured.")
            lines = [f"[{t}] {m}" for t, m in msgs]
            return ToolResult(
                f"Console messages (tab {actual_tab}, {len(lines)}):\n" + "\n".join(lines)
            )

        return self._execute_operation("console", _do_console, tab_index=tab_index)

    def requests(self, clear: bool = False, tab_index: int | None = None) -> ToolResult:
        """返回该 tab 捕获的网络请求（方法、URL、状态码）。"""
        def _do_requests(page):
            actual_tab = self._find_tab_index(page) or 0
            buf = self._request_buffers.get(actual_tab)
            if not buf:
                return ToolResult("No network requests captured yet.")
            reqs = list(buf)
            if clear:
                buf.clear()
            if not reqs:
                return ToolResult("No network requests captured.")
            lines = [f"{method} {url} {status}" for method, url, status in reqs]
            return ToolResult(
                f"Network requests (tab {actual_tab}, {len(lines)}):\n" + "\n".join(lines)
            )

        return self._execute_operation("requests", _do_requests, tab_index=tab_index)

    def switch_tab(self, tab_index: int) -> ToolResult:
        """切换到指定 tab 并设为活跃。

        Args:
            tab_index: 要切换到的 tab 编号

        Returns:
            ToolResult 包含切换结果和当前 URL
        """
        def _do_switch():
            page = self._get_page(tab_index)
            if not page:
                return ToolResult(f"Tab {tab_index} not found", error=True)
            self._touch_page(tab_index, page)
            url = page.url
            title = page.title() if not page.is_closed() else "(closed)"
            return ToolResult(
                f"Switched to tab {tab_index}\n"
                f"URL: {url}\n"
                f"Title: {title}"
            )

        try:
            return self._run_in_worker(_do_switch)
        except Exception as e:
            return ToolResult(f"Browser switch_tab failed with worker error: {e}", error=True)

    def list_tabs(self) -> ToolResult:
        """列出所有打开的 tab。

        Returns:
            ToolResult 包含每个 tab 的信息（index, url, title, active）
        """
        def _do_list():
            if not self._page_pool:
                return ToolResult("No tabs open")
            lines = []
            for idx, (page, ts) in sorted(self._page_pool.items()):
                active = " [ACTIVE]" if idx == self._active_tab_index else ""
                try:
                    if page.is_closed():
                        status = "closed"
                        url = "(closed)"
                        title = ""
                    else:
                        status = "open"
                        url = page.url
                        title = page.title()
                except Exception:
                    status = "error"
                    url = "(error)"
                    title = ""
                idle_seconds = int(time.time() - ts)
                lines.append(
                    f"  tab {idx}: {status}, idle={idle_seconds}s{active}\n"
                    f"    URL: {url}\n"
                    f"    Title: {title}"
                )
            return ToolResult(f"Tabs ({len(self._page_pool)}):\n\n" + "\n".join(lines))

        try:
            return self._run_in_worker(_do_list)
        except Exception as e:
            return ToolResult(f"Browser list_tabs failed with worker error: {e}", error=True)

    def close_tab(self, tab_index: int) -> ToolResult:
        """关闭指定 tab。

        Args:
            tab_index: 要关闭的 tab 编号

        Returns:
            ToolResult 包含关闭结果
        """
        def _do_close():
            entry = self._page_pool.get(tab_index)
            if not entry:
                return ToolResult(f"Tab {tab_index} not found", error=True)
            page, _ = entry
            url = page.url if not page.is_closed() else "(already closed)"
            self._close_page_internal(tab_index, page)
            return ToolResult(f"Closed tab {tab_index}\nURL: {url}")

        try:
            return self._run_in_worker(_do_close)
        except Exception as e:
            return ToolResult(f"Browser close_tab failed with worker error: {e}", error=True)

    def get_current_tab_index(self) -> int | None:
        """获取当前活跃 tab 的 index（同步方法，不走 worker）。"""
        return self._active_tab_index

    def kill_chrome(self) -> None:
        """终止 Chrome 进程（公共方法）。

        只杀死属于当前 cili 实例的进程，不会影响其他实例。
        """
        def _do_kill():
            self._kill_chrome_internal()
            return None

        try:
            self._run_in_worker(_do_kill)
        except Exception as e:
            logger.warning(f"[BrowserService] Error killing Chrome: {e}")


# ==================== 模块级 API（遵循 CronScheduler 模式）====================

_service: BrowserService | None = None
_service_lock = threading.Lock()


def get_service() -> BrowserService:
    """获取全局浏览器服务实例。

    如果服务未启动，会自动创建并启动（容错设计）。
    双重检查加锁，避免多线程并发首次调用时重复创建实例和 Chrome 进程（SEC-21）。
    """
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = BrowserService()
                _service.start()
    return _service


def start_browser_service() -> BrowserService:
    """启动全局浏览器服务。"""
    return get_service()


def stop_browser_service() -> None:
    """停止全局浏览器服务。"""
    global _service
    if _service:
        _service.stop()
        _service = None
