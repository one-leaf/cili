"""Browser Service - 全局浏览器服务，管理唯一的 Playwright 实例和 Chrome 进程。

设计背景：
- Playwright Sync API 在同一进程内只允许一个实例运行
- 将 Playwright 管理集中到服务层，工具层只负责调用
- 遵循 CronScheduler 的模块级单例模式
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from core.config import PROJECT_ROOT
from core.tools.shared.base import ToolResult

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
        self._result_event = threading.Event()
        self._task_result = None

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
        logger.info("[BrowserService] Worker thread started")

    def _worker_loop(self) -> None:
        """工作线程主循环，处理任务队列中的任务。

        每次执行任务前，自动清理超时的 tab（> TAB_IDLE_TIMEOUT 无活动）。
        """
        while True:
            try:
                task_func, task_args, task_kwargs = self._task_queue.get()
                if task_func is None:  # 退出信号
                    break

                # Lazy cleanup: close tabs idle for > TAB_IDLE_TIMEOUT
                try:
                    self._cleanup_expired_tabs()
                except Exception as e:
                    logger.debug(f"[BrowserService] Cleanup error (non-fatal): {e}")

                try:
                    result = task_func(*task_args, **task_kwargs)
                    self._task_result = (True, result)
                except Exception as e:
                    logger.warning(f"[BrowserService] Worker task error: {e}")
                    self._task_result = (False, e)
                finally:
                    self._result_event.set()
            except Exception as e:
                logger.error(f"[BrowserService] Worker loop error: {e}", exc_info=True)

    def _run_in_worker(self, func, *args, **kwargs):
        """在专用工作线程中执行函数。

        注意：此方法不能在工作线程内部调用，否则会死锁！
        """
        # 检查是否在工作线程内部调用
        if threading.current_thread() == self._worker_thread:
            # 直接执行，不通过队列
            return func(*args, **kwargs)

        # 确保工作线程已启动
        self._start_worker_thread()

        # 清空之前的结果
        self._result_event.clear()
        self._task_result = None

        # 提交任务到队列
        self._task_queue.put((func, args, kwargs))

        # 等待结果
        if not self._result_event.wait(timeout=60):
            raise TimeoutError("Task execution timeout (60s)")

        if self._task_result is None:
            raise RuntimeError("Task result is None")

        success, result = self._task_result
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
            logger.info("[BrowserService] Playwright started successfully")
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
                logger.info("[BrowserService] Service stopped")
                return None

            try:
                self._run_in_worker(_cleanup)
            except Exception as e:
                logger.warning(f"[BrowserService] Error during cleanup: {e}")

            # 停止工作线程
            if self._worker_thread:
                self._task_queue.put((None, None, None))  # 退出信号
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

    def _try_cdp_connect(self) -> bool:
        """尝试通过 CDP 连接到 Chrome。返回 True 表示成功。"""
        if self._playwright is None:
            return False

        try:
            browser = self._playwright.chromium.connect_over_cdp(
                f"http://localhost:{DEFAULT_CDP_PORT}"
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
                            logger.info(f"[BrowserService] Found existing browser PID {pid}")
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
                            logger.info(f"[BrowserService] Found existing browser PID {pid}")
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
                logger.info(f"[BrowserService] Chrome process exited with code {self._chrome_process.returncode}")
                self._chrome_process = None

        # 即使 _chrome_process 为 None，端口可能仍在监听
        # （例如：之前启动但引用丢失的 Chrome）
        if self._is_port_listening(port):
            # 端口在监听 - 验证 CDP 是否可用
            if self._try_cdp_connect():
                # 尝试找到并记录这个 Chrome 进程的 PID
                self._chrome_process = self._find_chrome_process_by_profile()
                if self._chrome_process:
                    logger.info(f"[BrowserService] Reconnected to existing Chrome PID {self._chrome_process.pid}")
                else:
                    logger.warning("[BrowserService] Connected to Chrome but could not find process PID")
                return True, ""
            else:
                logger.warning(f"[BrowserService] Port {port} is listening but CDP connection failed, killing stale Chrome")
                self._kill_chrome_internal()
                # 等待端口释放
                for i in range(20):
                    time.sleep(0.5)
                    if not self._is_port_listening(port):
                        logger.info(f"[BrowserService] Port {port} released after {(i+1)*0.5:.1f}s")
                        break
                else:
                    return False, f"Port {port} still in use after killing stale Chrome (waited 10s)"

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

            logger.info(f"[BrowserService] Starting Chrome: {chrome_path}")
            logger.info(f"[BrowserService] Profile: {user_data_dir}, Port: {port}")
            logger.info(f"[BrowserService] Command: {' '.join(cmd)}")

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

            logger.info(f"[BrowserService] Chrome started, PID: {self._chrome_process.pid}")

            # 等待 Chrome 启动并监听 CDP 端口（最多 20 秒）
            for i in range(20):
                time.sleep(1)

                # 先检查进程是否还活着
                if self._chrome_process.poll() is not None:
                    return False, f"Chrome exited immediately with code {self._chrome_process.returncode}"

                # 尝试 CDP 连接
                if self._try_cdp_connect():
                    logger.info(f"[BrowserService] Chrome CDP connected after {i+1}s")
                    return True, ""

                # 每秒输出进度
                if i % 5 == 4:
                    logger.info(f"[BrowserService] Waiting for CDP... {i+1}s")

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
            logger.info(f"[BrowserService] Terminating Chrome PID {pid}")

            # Windows: 使用 taskkill /T 杀死进程树（包括子进程）
            if sys.platform == "win32":
                try:
                    result = subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(pid)],
                        capture_output=True, text=True, timeout=10
                    )
                    logger.info(f"[BrowserService] taskkill result: {result.returncode}")
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

            logger.info(f"[BrowserService] Chrome PID {pid} killed")
        except Exception as e:
            logger.warning(f"[BrowserService] Error killing Chrome: {e}")
        finally:
            self._chrome_process = None

        # 删除 Chrome 的 singleton 锁定文件
        self._remove_chrome_locks(self._chrome_profile_dir)

        # 等待端口释放
        for i in range(20):
            time.sleep(0.5)
            if not self._is_port_listening(DEFAULT_CDP_PORT):
                logger.info(f"[BrowserService] Port {DEFAULT_CDP_PORT} released after {(i+1)*0.5:.1f}s")
                break
        else:
            logger.warning(f"[BrowserService] Port {DEFAULT_CDP_PORT} still listening after 10s")

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
                f"http://localhost:{DEFAULT_CDP_PORT}"
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
                logger.info(f"[BrowserService] Existing browser connection invalid, reconnecting...")
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
        if self._is_port_listening(DEFAULT_CDP_PORT):
            logger.info(f"[BrowserService] Port {DEFAULT_CDP_PORT} listening but CDP failed, retrying...")
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
        success, error_msg = self._start_chrome(DEFAULT_CDP_PORT)
        if not success:
            chrome_path = self._find_browser()
            return ToolResult(
                f"Error: Failed to start/connect Chrome.\n"
                f"Detail: {error_msg}\n"
                f"Chrome path: {chrome_path}\n"
                f"Profile: {self._chrome_profile_dir}\n"
                f"Port: {DEFAULT_CDP_PORT}\n"
                f"Playwright: {'OK' if self._playwright else 'NOT STARTED'}\n"
                f"Chrome process: {self._chrome_process.pid if self._chrome_process else 'None'}\n"
                f"Port listening: {self._is_port_listening(DEFAULT_CDP_PORT)}",
                error=True,
            )

        # 再次尝试连接
        if not self._connect_browser():
            return ToolResult(
                f"Error: Chrome started but CDP connection failed.\n"
                f"Port: {DEFAULT_CDP_PORT}\n"
                f"Chrome PID: {self._chrome_process.pid if self._chrome_process else 'None'}\n"
                f"Chrome alive: {self._chrome_process.poll() is None if self._chrome_process else False}\n"
                f"Port listening: {self._is_port_listening(DEFAULT_CDP_PORT)}",
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

    def _touch_page(self, tab_index: int, page) -> None:
        """更新 page 的最后活动时间，设为活跃 tab。"""
        self._page_pool[tab_index] = (page, time.time())
        self._active_tab_index = tab_index

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
            logger.info(f"[BrowserService] Closed {len(tabs_to_close)} idle tab(s), "
                        f"{len(self._page_pool)} remaining")

    def _close_page_internal(self, tab_index: int, page) -> None:
        """关闭单个 page 并从池中移除。"""
        self._page_pool.pop(tab_index, None)
        if self._active_tab_index == tab_index:
            self._active_tab_index = None
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
                    f"  Port listening: {self._is_port_listening(DEFAULT_CDP_PORT)}\n\n"
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
                f"--- Page content ---\n{text}",
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
            markdown_link = f"![Screenshot](/api/workspace/files/{filename})"
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


def get_service() -> BrowserService:
    """获取全局浏览器服务实例。

    如果服务未启动，会自动创建并启动（容错设计）。
    """
    global _service
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
