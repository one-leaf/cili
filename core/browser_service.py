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
import re
import subprocess
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Page

from core.browser_chrome import ChromeProcessRef as _ChromeProcessRef
from core.browser_chrome import (
    build_chrome_command,
    find_chrome_process_by_profile,
    find_pid_by_port,
    is_port_listening,
    kill_process_tree,
    pick_free_port,
    pid_uses_profile,
    probe_cdp,
    remove_chrome_locks,
    spawn_chrome,
)
from core.browser_snapshot import (
    parse_aria_snapshot,
)
from core.browser_url_policy import check_navigate_url
from core.config import PROJECT_ROOT
from core.tools.base import UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END, ToolResult

# 每 tab 的 console/request 缓冲上限
BUFFER_MAXLEN = 200

logger = logging.getLogger(__name__)

# Default Chrome DevTools Protocol port
DEFAULT_CDP_PORT = 9222

# Tab idle timeout: auto-close tabs inactive for this many seconds (10 minutes)
TAB_IDLE_TIMEOUT = 600

# 工作线程任务执行超时（秒）
_WORKER_TASK_TIMEOUT = 60
# 随机 CDP 端口的候选范围与尝试次数
_RANDOM_CDP_PORT_RANGE = (9300, 9900)
_RANDOM_CDP_PORT_TRIES = 50
# 端口释放等待的重试次数与轮询间隔（秒）
_PORT_RELEASE_RETRIES = 20
_POLL_INTERVAL_S = 0.5
# Chrome 启动等待：总上限与轮询粒度。粒度取 0.25s——Chrome 通常 1~3s 起来，
# 1s 粒度会让就绪最多晚 1s 才被发现，期间所有浏览器操作排队。
_CHROME_START_TIMEOUT_S = 20.0
_CHROME_POLL_INTERVAL_S = 0.25
_CHROME_PROGRESS_EVERY_TICKS = int(5 / _CHROME_POLL_INTERVAL_S)  # 每 5 秒打一次进度
# CDP 连接重试次数
_CDP_CONNECT_RETRIES = 3
# 页面交互超时（ms）：导航 / 前进后退刷新 / 单击与输入
_NAV_TIMEOUT_MS = 60000
_HISTORY_TIMEOUT_MS = 30000
_ACTION_TIMEOUT_MS = 10000
# 输出文本截断上限（字符）：工具输出 / snapshot 展示
_TRUNCATE_OUTPUT_CHARS = 10000
_TRUNCATE_SNAPSHOT_CHARS = 20000
# 顺序输入按键间隔（ms）
_TYPE_DELAY_MS = 20
# 下载等待超时（ms）
_DOWNLOAD_TIMEOUT_MS = 60000
# worker 等待时间必须大于操作自身超时，否则外层先抛 TimeoutError、操作仍在跑
_NAV_WORKER_TIMEOUT = _NAV_TIMEOUT_MS / 1000 + 20
_DOWNLOAD_WORKER_TIMEOUT = _DOWNLOAD_TIMEOUT_MS / 1000 + 20
# wait_for 最大等待（ms）：模型传入的 timeout 必须钳在 worker 预算以内，
# 否则 worker 先超时 → 结果被丢弃、工作线程继续被占住（后续操作全部排队超时）
_WAIT_FOR_MAX_TIMEOUT_MS = 45000
# 超时自愈：杀 Chrome 后等待 worker 从卡住的调用里退出的宽限（秒）
_RECOVERY_WORKER_GRACE_S = 8.0
# 两次自动恢复之间的最小间隔（秒），防止多个排队超时的调用反复杀 Chrome
_RECOVERY_MIN_INTERVAL_S = 10.0
# 弹窗自动处理行为：dismiss（记录后关闭）/ accept（记录后确认）
_DIALOG_BEHAVIOR = "dismiss"
# 请求详情 body 读取上限（字符），超过则跳过 body
_BODY_MAX_CHARS = 2000

# Try to import stealth
try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False


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
        # 已关闭 tab 释放出的 index，新 tab 优先复用（否则编号只增不减，
        # 长时间运行后不便排查）。关闭时已清理该 index 的全部附属状态，
        # 见 _close_page_internal。
        self._free_tab_indices: list[int] = []
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

        # ref 定位：(tab_index, frame_index) → {ref: {role, name, index, mode}}；
        # frame_index None/0 均归一为主 frame（None 为默认主 frame 上下文）
        self._snapshot_refs: dict[tuple[int, int | None], dict[str, dict]] = {}
        # console / 网络请求 / dialog 缓冲：tab_index → deque[(type, text)] /
        # deque[dict]（请求详情）/ deque[(type, message)]
        self._console_buffers: dict[int, deque] = {}
        self._request_buffers: dict[int, deque] = {}
        self._dialog_buffers: dict[int, deque] = {}
        # 已挂监听器的 page（按 id(page) 去重）
        self._listener_pages: set[int] = set()

        # 自愈状态：工作线程卡死（被原生弹窗/渲染器阻塞的 sync 调用占住）时，
        # 杀 Chrome 让卡住的调用失败；仍未退出则换代重建 worker
        self._worker_generation = 0
        # RLock：_run_in_worker 持锁提交任务，_start_worker_thread 内部再次加锁
        self._worker_start_lock = threading.RLock()
        self._recovery_lock = threading.Lock()
        self._recovering = False
        self._last_recovery_ts = 0.0
        self._stopping = False

    # ==================== 生命周期方法 ====================

    def _start_worker_thread(self) -> None:
        """启动专用工作线程；每次启动分配新队列与代号（换代）。"""
        with self._worker_start_lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return

            self._worker_generation += 1
            generation = self._worker_generation
            task_queue: queue.Queue = queue.Queue()
            self._task_queue = task_queue
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                args=(task_queue, generation),
                name=f"BrowserService-Worker-{generation}",
                daemon=True,
            )
            self._worker_thread.start()
        logger.debug(
            f"[BrowserService] Worker thread started (generation {generation})"
        )

    def _worker_loop(self, task_queue: queue.Queue, generation: int) -> None:
        """工作线程主循环，处理任务队列中的任务。

        每次执行任务前，自动清理超时的 tab（> TAB_IDLE_TIMEOUT 无活动）。
        队列与代号在启动时绑定：换代后旧线程从卡住调用返回时按 sentinel 退出，
        不会误消费新队列的任务。
        """
        while True:
            try:
                item = task_queue.get()
                if item is None or (isinstance(item, tuple) and item and item[0] is None):
                    break  # 退出信号
                if generation != self._worker_generation:
                    break  # 旧代线程：让位给新一代

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

    def _run_in_worker(self, func, *args, timeout: float | None = None, **kwargs):
        """在专用工作线程中执行函数。

        每次调用分配独立的结果槽位（Event + 结果），多线程并发调用互不串扰。
        注意：此方法不能在工作线程内部调用，否则会死锁！
        """
        # 检查是否在工作线程内部调用
        if threading.current_thread() == self._worker_thread:
            # 直接执行，不通过队列
            return func(*args, **kwargs)

        # 启动线程与提交任务在同一把锁内：换代重建期间不会把任务投进
        # 已被废弃的旧队列（否则新 worker 看不到，调用方只能等超时）
        with self._worker_start_lock:
            self._start_worker_thread()

            # 为本次调用分配独立结果槽位
            task_id = self._next_task_id()
            event = threading.Event()
            with self._task_lock:
                self._task_events[task_id] = event

            # 提交任务到队列
            self._task_queue.put((task_id, func, args, kwargs))

        # 等待结果（按操作类型分级；未指定时用全局默认）
        effective_timeout = timeout if timeout is not None else _WORKER_TASK_TIMEOUT
        if not event.wait(timeout=effective_timeout):
            with self._task_lock:
                self._task_events.pop(task_id, None)
            # 同步 Playwright 调用无法取消：任务仍在 worker 里跑，且 worker 是全局
            # 单线程——不恢复的话后续所有浏览器操作都会排队超时。异步自愈：
            # 杀 Chrome 让卡住的 CDP 调用立即失败；worker 仍不退出则换代重建。
            self._schedule_recovery(
                f"worker task timeout ({effective_timeout:.0f}s)"
            )
            raise TimeoutError(f"Task execution timeout ({effective_timeout:.0f}s)")

        with self._task_lock:
            success, result = self._task_results.pop(task_id)
        if not success:
            raise result
        return result

    # ─── 超时自愈（原生弹窗/渲染器阻塞导致工作线程卡死）────────────────

    def _schedule_recovery(self, reason: str) -> None:
        """异步触发恢复；节流并去重（多个排队超时的调用只恢复一次）。"""
        if self._stopping:
            return
        with self._recovery_lock:
            now = time.time()
            if self._recovering or (now - self._last_recovery_ts) < _RECOVERY_MIN_INTERVAL_S:
                return
            self._recovering = True
            self._last_recovery_ts = now
        threading.Thread(
            target=self._recover,
            args=(reason,),
            name="BrowserService-Recovery",
            daemon=True,
        ).start()

    def _recover(self, reason: str) -> None:
        """杀 Chrome 释放卡住的 CDP 调用；worker 仍未退出则换代重建。

        调用方须已置 ``_recovering``（``_schedule_recovery`` 或 ``reset``）。
        """
        try:
            logger.warning(f"[BrowserService] 自动恢复浏览器（{reason}）")
            try:
                self._kill_chrome_internal()
            except Exception as e:  # noqa: BLE001 —— 恢复流程尽力而为
                logger.warning(f"[BrowserService] 恢复时终止 Chrome 失败: {e}")

            worker = self._worker_thread
            if worker is not None:
                worker.join(timeout=_RECOVERY_WORKER_GRACE_S)

            if worker is not None and worker.is_alive():
                # 杀 Chrome 也没能让卡住的调用退出（如 driver 线程死锁）：
                # 放弃旧线程，换代重建（旧 Playwright 实例随之废弃）
                logger.warning(
                    "[BrowserService] 工作线程未退出，换代重建 worker"
                )
                self._restart_worker()
                return

            # worker 已恢复：作废旧连接与 page 池，下一次操作重连/重启 Chrome
            if self._worker_thread is not None:
                try:
                    self._run_in_worker(self._reset_state_in_worker, timeout=5)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[BrowserService] 复位连接状态失败，换代重建 worker: {e}"
                    )
                    self._restart_worker()
        finally:
            with self._recovery_lock:
                self._recovering = False

    def _restart_worker(self) -> None:
        """丢弃可能卡死的 worker：换代启动新线程，旧实例引用全部作废。

        新 worker 在首次操作时懒启动新的 Playwright 实例；旧线程从卡住调用
        返回后按 sentinel 退出（daemon，不阻塞进程退出）。
        """
        # 与 _run_in_worker 的提交串行化：交换期间不会有任务落进旧队列
        with self._worker_start_lock:
            old_queue = self._task_queue
            self._playwright = None
            self._browser = None
            self._context = None
            self._page_pool.clear()
            self._active_tab_index = None
            self._free_tab_indices.clear()
            self._listener_pages.clear()
            self._snapshot_refs.clear()
            self._console_buffers.clear()
            self._request_buffers.clear()
            self._dialog_buffers.clear()
            self._worker_thread = None
            try:
                old_queue.put(None)
            except Exception:  # noqa: BLE001
                pass
            self._start_worker_thread()

    def _reset_state_in_worker(self) -> None:
        """作废连接与 page 池（必须在 worker 线程内执行）。"""
        self._disconnect_internal()
        # page 已全部关闭：监听器/快照/缓冲区状态一并作废，避免旧 tab_index 残留
        self._listener_pages.clear()
        self._snapshot_refs.clear()
        self._console_buffers.clear()
        self._request_buffers.clear()
        self._dialog_buffers.clear()
        self._browser = None
        self._context = None

    def reset(self, reason: str = "manual") -> ToolResult:
        """强制恢复浏览器：杀 Chrome、作废连接；卡死的工作线程会被换代重建。

        刻意不走工作线程——卡死时工作线程已不可用。供 ``browser(action="reset")``
        在页面被原生弹窗/渲染器阻塞、操作持续超时时手动恢复。
        """
        with self._recovery_lock:
            if self._recovering:
                return ToolResult(
                    "Browser reset already in progress; retry shortly.", error=True
                )
            self._recovering = True
            self._last_recovery_ts = time.time()
        logger.warning(f"[BrowserService] 手动重置浏览器（{reason}）")
        self._recover(reason)
        return ToolResult(
            "Browser reset: Chrome was stopped and the connection invalidated. "
            "The next browser action will restart Chrome automatically."
        )

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
        self._stopping = True
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
                self._run_in_worker(_cleanup, timeout=15)
            except Exception as e:
                logger.warning(f"[BrowserService] Error during cleanup: {e}")

            # 停止工作线程；卡死的 sync 调用无法从外部中断，杀 Chrome 令其失败后
            # 放弃旧线程（daemon），进程退出时由 OS 回收
            worker = self._worker_thread
            if worker is not None:
                try:
                    self._task_queue.put(None)  # 退出信号
                except Exception:  # noqa: BLE001
                    pass
                worker.join(timeout=5)
                if worker.is_alive():
                    logger.warning(
                        "[BrowserService] Worker thread did not exit; abandoning it"
                    )
                    try:
                        self._kill_chrome_internal()
                    except Exception:  # noqa: BLE001
                        pass
                self._worker_thread = None

            # 旧线程可能仍持有 Playwright 实例：引用置空，避免下次使用误判为可用
            self._playwright = None
            self._browser = None
            self._context = None
            self._page_pool.clear()
            self._active_tab_index = None
            self._free_tab_indices.clear()
            self._listener_pages.clear()
            self._running = False

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
        # 池已清空，编号可从 1 重新开始
        self._free_tab_indices.clear()
        self._next_tab_index = 1

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
        return is_port_listening(port)

    def _pick_free_port(self) -> int:
        """在 [9300, 9900] 内找一个当前未监听的端口，作为 CDP 端口替代。"""
        return pick_free_port(self._is_port_listening)

    def _find_pid_by_port(self, port: int) -> int | None:
        """找到监听指定端口（LISTENING）的进程 PID。"""
        return find_pid_by_port(port)

    def _pid_uses_our_profile(self, pid: int) -> bool:
        """判断指定 PID 的进程命令行是否使用本项目 Chrome profile。"""
        return pid_uses_profile(pid, self._chrome_profile_dir)

    def _try_cdp_connect(self) -> bool:
        """尝试通过 CDP 连接到 Chrome。返回 True 表示成功。"""
        return probe_cdp(self._playwright, self._cdp_port)

    def _find_chrome_process_by_profile(self):
        """查找使用指定 profile 的浏览器进程（Chrome 或 Edge）。"""
        return find_chrome_process_by_profile(self._chrome_profile_dir)

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
                    for i in range(_PORT_RELEASE_RETRIES):
                        time.sleep(_POLL_INTERVAL_S)
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
            cmd = build_chrome_command(chrome_path, port, user_data_dir)

            logger.debug(f"[BrowserService] Starting Chrome: {chrome_path}")
            logger.debug(f"[BrowserService] Profile: {user_data_dir}, Port: {port}")
            logger.debug(f"[BrowserService] Command: {' '.join(cmd)}")

            self._chrome_process = spawn_chrome(cmd, self._env)

            logger.debug(f"[BrowserService] Chrome started, PID: {self._chrome_process.pid}")

            # 等待 Chrome 启动并监听 CDP 端口（最多 _CHROME_START_TIMEOUT_S 秒）
            start = time.monotonic()
            ticks = 0
            while time.monotonic() - start < _CHROME_START_TIMEOUT_S:
                time.sleep(_CHROME_POLL_INTERVAL_S)
                ticks += 1

                # 先检查进程是否还活着
                if self._chrome_process.poll() is not None:
                    return False, f"Chrome exited immediately with code {self._chrome_process.returncode}"

                # 尝试 CDP 连接
                if self._try_cdp_connect():
                    logger.debug(
                        f"[BrowserService] Chrome CDP connected after {time.monotonic() - start:.1f}s"
                    )
                    return True, ""

                # 每 5 秒输出一次进度
                if ticks % _CHROME_PROGRESS_EVERY_TICKS == 0:
                    logger.debug(f"[BrowserService] Waiting for CDP... {time.monotonic() - start:.0f}s")

            # 超时 - 检查进程状态
            if self._chrome_process.poll() is not None:
                return False, f"Chrome exited with code {self._chrome_process.returncode} after timeout"
            else:
                return False, (
                    f"Chrome running (PID {self._chrome_process.pid}) but CDP not ready "
                    f"after {_CHROME_START_TIMEOUT_S:.0f}s"
                )

        except Exception as e:
            return False, f"Failed to start Chrome: {e}"

    def _kill_chrome_internal(self) -> None:
        """内部方法：终止本项目 Chrome 进程（不持锁）。

        安全边界：只终结「记录的 PID 仍存活且命令行确认使用本项目 profile」的
        进程。Windows 会复用 PID——服务运行久了，旧 PID 可能已属于用户自己的
        浏览器或其他程序；拿不到命令行（PowerShell 失败）时宁可不杀，也不能
        误伤。用户个人 Chrome 使用独立 profile，因此不会被这里的 tree kill 波及。
        """
        if not self._chrome_process:
            # 没有我们自己的进程，只清理本项目 profile 的锁文件
            self._remove_chrome_locks(self._chrome_profile_dir)
            return

        pid = self._chrome_process.pid

        # 1) 进程已退出：PID 可能已被系统复用，绝不能按旧 PID 继续杀
        try:
            alive = self._chrome_process.poll() is None
        except Exception:
            alive = False
        if not alive:
            logger.debug(
                f"[BrowserService] Chrome PID {pid} already exited; skip kill"
            )
            self._chrome_process = None
            self._remove_chrome_locks(self._chrome_profile_dir)
            return

        # 2) 身份校验：命令行必须包含本项目 profile（防 PID 复用误杀用户浏览器）
        if not self._pid_uses_our_profile(pid):
            logger.warning(
                f"[BrowserService] PID {pid} is not verified as this project's "
                "Chrome (possibly a reused PID); skip kill to avoid harming "
                "other browsers or programs"
            )
            self._chrome_process = None
            return

        try:
            logger.debug(f"[BrowserService] Terminating Chrome PID {pid}")

            kill_process_tree(self._chrome_process)

            logger.debug(f"[BrowserService] Chrome PID {pid} killed")
        except Exception as e:
            logger.warning(f"[BrowserService] Error killing Chrome: {e}")
        finally:
            self._chrome_process = None

        # 删除 Chrome 的 singleton 锁定文件
        self._remove_chrome_locks(self._chrome_profile_dir)

        # 等待端口释放
        for i in range(_PORT_RELEASE_RETRIES):
            time.sleep(_POLL_INTERVAL_S)
            if not self._is_port_listening(self._cdp_port):
                logger.debug(f"[BrowserService] Port {self._cdp_port} released after {(i+1)*0.5:.1f}s")
                break
        else:
            logger.warning(f"[BrowserService] Port {self._cdp_port} still listening after 10s")

    def _remove_chrome_locks(self, profile_dir: str) -> None:
        """删除 Chrome 的 singleton 锁定文件，防止 profile 被锁定。"""
        remove_chrome_locks(profile_dir)

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
            logger.debug("[BrowserService] CDP connected successfully")
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

        def _invalidate_connection():
            # 必须走工作线程：_context/_browser 由工作线程的
            # _do_connect/_disconnect/_open_new_page 读写，在主调线程直接赋值
            # 会与之竞争（无锁）
            self._context = None
            self._browser = None

        try:
            is_valid = self._run_in_worker(_check_connection)
            if is_valid:
                logger.debug("[BrowserService] Existing browser connection is valid")
                return None  # 已有有效连接
            logger.debug("[BrowserService] Existing browser connection invalid, reconnecting...")
            # 连接已失效，清理后重新连接
            self._run_in_worker(_invalidate_connection)
        except Exception as e:
            logger.warning(f"[BrowserService] Error checking connection: {e}")
            try:
                self._run_in_worker(_invalidate_connection)
            except Exception as inner:
                # 工作线程已不可用：此时不存在并发写，直接清空
                logger.debug(f"[BrowserService] 清理连接状态失败: {inner}")
                self._context = None
                self._browser = None

        # 尝试连接到现有的 Chrome
        if self._connect_browser():
            return None  # 已连接

        # 连接失败 - 检查 Chrome 是否已在运行
        if self._is_port_listening(self._cdp_port):
            logger.debug(f"[BrowserService] Port {self._cdp_port} listening but CDP failed, retrying...")
            # Chrome 在运行但连不上 - 重试连接
            for attempt in range(_CDP_CONNECT_RETRIES):
                time.sleep(_POLL_INTERVAL_S)
                logger.debug(f"[BrowserService] CDP retry attempt {attempt + 1}/3")
                if self._connect_browser():
                    return None
            # 全部失败，杀死并重启
            logger.warning("[BrowserService] CDP retry failed, restarting Chrome")
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
        if self._free_tab_indices:
            tab_index = self._free_tab_indices.pop()
        else:
            tab_index = self._next_tab_index
            self._next_tab_index += 1

        self._touch_page(tab_index, new_page)
        return tab_index

    def _ensure_listeners(self, page, tab_index: int) -> None:
        """为 page 挂上 console / response / dialog 监听器（幂等）。

        监听器回调在 Playwright driver 线程运行，deque.append 是原子的，无锁安全。
        """
        if id(page) in self._listener_pages:
            return
        self._listener_pages.add(id(page))
        console_buf: deque = deque(maxlen=BUFFER_MAXLEN)
        request_buf: deque = deque(maxlen=BUFFER_MAXLEN)
        dialog_buf: deque = deque(maxlen=BUFFER_MAXLEN)
        self._console_buffers[tab_index] = console_buf
        self._request_buffers[tab_index] = request_buf
        self._dialog_buffers[tab_index] = dialog_buf
        try:
            page.on("console", lambda msg: console_buf.append((msg.type, msg.text)))
            page.on("response", lambda resp: self._capture_response(request_buf, resp))
            page.on("dialog", lambda dlg: self._handle_dialog(tab_index, dlg))
        except Exception:
            pass

    def _capture_response(self, buf: deque, resp) -> None:
        """捕获响应详情为 dict（driver 线程回调，绝不调 page 方法，全部 try/except 包裹）。

        body 仅对文本类 content-type 且 content-length 在阈值内才读取，避免大响应拖慢页面。
        """
        try:
            entry: dict = {
                "method": resp.request.method,
                "url": resp.url,
                "status": resp.status,
            }
            try:
                entry["req_headers"] = dict(resp.request.headers)
            except Exception:
                pass
            try:
                entry["resp_headers"] = dict(resp.headers)
            except Exception:
                pass
            try:
                ct = (resp.headers.get("content-type") or "").lower()
                cl = resp.headers.get("content-length")
                if ("text/" in ct or "json" in ct or "xml" in ct or "+json" in ct) \
                        and cl is not None and cl.isdigit() and int(cl) <= _BODY_MAX_CHARS * 4:
                    body = resp.body()
                    if len(body) <= _BODY_MAX_CHARS:
                        entry["body_text"] = body.decode("utf-8", errors="replace")
            except Exception:
                pass
            buf.append(entry)
        except Exception:
            pass

    def _handle_dialog(self, tab_index: int, dlg) -> None:
        """记录弹窗并按策略自动关闭（driver 线程回调，只调 dialog 方法不调 page，防死锁）。"""
        try:
            buf = self._dialog_buffers.get(tab_index)
            if buf is not None:
                buf.append((dlg.type, (dlg.message or "")[:500]))
        except Exception:
            pass
        try:
            if _DIALOG_BEHAVIOR == "accept":
                dlg.accept()
            else:
                dlg.dismiss()
        except Exception:
            pass

    def _find_tab_index(self, page) -> int | None:
        """根据 page 对象反查它在 tab 池中的 index。"""
        for idx, (pool_page, _ts) in self._page_pool.items():
            if pool_page is page:
                return idx
        return self._active_tab_index

    def _resolve_frame(self, page, frame: int | None = None):
        """解析 frame 上下文：None/0 → 主 frame；否则 page.frames[i]（越界报错）。

        Page 与 Frame 提供相同定位接口（get_by_role/get_by_text/locator/evaluate），
        返回的 base 可直接用于这些调用。
        """
        if frame is None:
            return page
        frames = page.frames
        if not (0 <= frame < len(frames)):
            raise ValueError(
                f"Frame index {frame} out of range (0-{len(frames)-1}). "
                f"Run 'get_frames' to list frames."
            )
        return frames[frame]

    def _ref_key(self, tab_index: int, frame: int | None) -> tuple[int, int | None]:
        """ref 映射键：主 frame 统一用 0（None 与 0 等价）。"""
        return (tab_index, 0 if frame is None else frame)

    def _resolve_ref(self, tab_index: int, ref: str, frame: int | None = None) -> dict:
        """把 ref 解析为定位条目；未知 ref 报错提示先跑 snapshot。"""
        ref_map = self._snapshot_refs.get(self._ref_key(tab_index, frame)) or {}
        entry = ref_map.get(ref)
        if not entry:
            fdesc = "" if frame is None else f" (frame {frame})"
            raise ValueError(
                f"Unknown ref '{ref}' for tab {tab_index}{fdesc}. "
                f"Run the 'snapshot' action first to get fresh refs."
            )
        return entry

    def _locator_for(self, base, entry: dict):
        """根据 ref 定位条目生成 Playwright locator（base 为 Page 或 Frame）。

        mode=role 用 get_by_role(role, name).nth(index)；
        mode=text（paragraph/generic 等文本节点）用 get_by_text(name).nth(index)。
        """
        if entry["mode"] == "text":
            return base.get_by_text(entry["name"], exact=True).nth(entry["index"])
        if not entry["name"]:
            # 无名元素（role in INTERACTIVE_ROLES）：按 role 定位。
            # 不能传 name=""——那会去匹配「可访问名为空」这一条件，语义不同。
            return base.get_by_role(entry["role"]).nth(entry["index"])
        return base.get_by_role(
            entry["role"], name=entry["name"], exact=True
        ).nth(entry["index"])

    def _build_snapshot(self, page, tab_index: int, frame: int | None = None) -> tuple[list[tuple[str, str]], dict]:
        """生成页面 aria snapshot 并更新该 tab+frame 的 ref 映射。

        Returns:
            (lines, ref_map)：lines 用于文本展示，ref_map 存入 _snapshot_refs[(tab_index, frame)]
        """
        base = self._resolve_frame(page, frame)
        yaml_text = base.locator("body").aria_snapshot()
        lines, ref_map = parse_aria_snapshot(yaml_text)
        self._snapshot_refs[self._ref_key(tab_index, frame)] = ref_map
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
        """关闭单个 page 并从池中移除（index 归还给 _free_tab_indices 复用）。"""
        self._page_pool.pop(tab_index, None)
        if tab_index not in self._free_tab_indices:
            self._free_tab_indices.append(tab_index)
        if self._active_tab_index == tab_index:
            self._active_tab_index = None
        self._listener_pages.discard(id(page))
        self._snapshot_refs = {k: v for k, v in self._snapshot_refs.items() if k[0] != tab_index}
        self._console_buffers.pop(tab_index, None)
        self._request_buffers.pop(tab_index, None)
        self._dialog_buffers.pop(tab_index, None)
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

    def _execute_operation(self, operation_name: str, func, tab_index: int | None = None,
                           timeout: float | None = None) -> ToolResult:
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
                    f"Suggestion: Close unnecessary tabs or restart the browser, then retry.",
                    error=True,
                )

        try:
            return self._run_in_worker(_do_operation, timeout=timeout)
        except TimeoutError as e:
            # _run_in_worker 已在超时处触发自愈（杀 Chrome → 必要时换代重建）
            return ToolResult(
                f"Browser {operation_name} timed out ({e}).\n"
                "The page may be blocked by a native dialog (print preview, "
                "certificate/permission prompt) or a frozen renderer.\n"
                "Recovery was started automatically; if the next call still fails, "
                "close the blocking dialog and retry, or call browser(action=\"reset\").",
                error=True,
            )
        except Exception as e:
            return ToolResult(f"Browser {operation_name} failed with worker error: {e}", error=True)

    def navigate(self, url: str, tab_index: int | None = None,
                 approved_non_public: bool = False) -> ToolResult:
        """导航到 URL 并返回页面文本内容。

        Args:
            url: 目标 URL
            tab_index: 指定 tab 编号，None 表示创建新 tab
            approved_non_public: 用户已批准该非公网导航（由浏览器工具在批准后传入）。
                只跳过私网/环回判定；scheme 白名单与云元数据端点仍硬拒绝。

        Returns:
            ToolResult，data 包含 tab_index 字段
        """
        # A30: SSRF 防护，先于任何浏览器操作快速失败
        block_reason = check_navigate_url(url, allow_non_public=approved_non_public)
        if block_reason:
            return ToolResult(
                f"Error: 导航被拒绝 — {block_reason}", error=True
            )
        def _do_navigate(page, current_tab_index):
            page.goto(url, wait_until="load", timeout=_NAV_TIMEOUT_MS)
            # 等待 JavaScript 渲染和重定向
            time.sleep(1)
            title = page.title()

            # 提取页面文本内容（而非原始 HTML）
            try:
                text = page.inner_text("body")
            except Exception:
                # fallback: 如果 inner_text 失败（如页面结构异常），取 HTML
                text = page.content()
            if len(text) > _TRUNCATE_OUTPUT_CHARS:
                text = text[:_TRUNCATE_OUTPUT_CHARS] + "\n... (truncated)"

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
                return ToolResult("Browser navigate failed: could not open tab", error=True)
            self._apply_stealth()
            return _do_navigate(page, actual_tab_index)

        try:
            return self._run_in_worker(_do_navigate_tab, timeout=_NAV_WORKER_TIMEOUT)
        except TimeoutError as e:
            return ToolResult(
                f"Browser navigate timed out ({e}).\n"
                "The page may be blocked by a native dialog (print preview, "
                "certificate/permission prompt) or a frozen renderer.\n"
                "Recovery was started automatically; if the next call still fails, "
                "close the blocking dialog and retry, or call browser(action=\"reset\").",
                error=True,
            )
        except Exception as e:
            return ToolResult(f"Browser navigate failed with worker error: {e}", error=True)

    def screenshot(self, path: str, tab_index: int | None = None,
                   image_url: str | None = None) -> ToolResult:
        """截取当前页面的截图。

        Args:
            path: 截图保存路径
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
            image_url: 前端可访问的图片 URL；缺省按文件名拼 ``/api/files/``
        """
        def _do_screenshot(page):
            if not os.path.isabs(path):
                abs_path = os.path.join(self._project_root, path)
            else:
                abs_path = path
            page.screenshot(path=abs_path, full_page=True)
            url = image_url or f"/api/files/{os.path.basename(abs_path)}"
            markdown_link = f"![Screenshot]({url})"
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
            if len(result_str) > _TRUNCATE_OUTPUT_CHARS:
                result_str = result_str[:_TRUNCATE_OUTPUT_CHARS] + "\n... (truncated)"
            return ToolResult(f"JavaScript result:\n{result_str}")

        return self._execute_operation("execute_script", _do_execute, tab_index=tab_index)

    def wait_for(self, selector: str, timeout: int = _ACTION_TIMEOUT_MS, tab_index: int | None = None) -> ToolResult:
        """等待 CSS 选择器出现。

        Args:
            selector: CSS 选择器
            timeout: 超时时间（毫秒）。会钳制在 ``_WAIT_FOR_MAX_TIMEOUT_MS`` 以内——
                等待时间超过 worker 预算时，调用方会先超时丢结果，而工作线程
                仍被占住（后续操作全部排队超时）。
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        effective_ms = max(1000, min(int(timeout), _WAIT_FOR_MAX_TIMEOUT_MS))

        def _do_wait(page):
            page.wait_for_selector(selector, timeout=effective_ms)
            return ToolResult(f"Element '{selector}' found")

        return self._execute_operation(
            f"wait_for({selector})", _do_wait, tab_index=tab_index,
            timeout=effective_ms / 1000 + 15,
        )

    def get_text(self, tab_index: int | None = None) -> ToolResult:
        """获取页面的所有文本内容。

        Args:
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        def _do_get_text(page):
            text = page.inner_text("body")
            if len(text) > _TRUNCATE_SNAPSHOT_CHARS:
                text = text[:_TRUNCATE_SNAPSHOT_CHARS] + "\n... (truncated)"
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
            lines = [f"- [{it['text'][:50]}]({it['href']})" for it in links]
            return ToolResult(f"Found {len(links)} links:\n\n" + "\n".join(lines))

        return self._execute_operation("get_links", _do_get_links, tab_index=tab_index)

    # ==================== ref 定位交互（snapshot/find/click/fill/type/press） ====================

    def snapshot(self, tab_index: int | None = None, frame: int | None = None) -> ToolResult:
        """获取页面 accessibility snapshot，为可交互元素分配 ref。

        返回的 YAML 树中带 [ref=rN] 的元素可用 click/fill/type/press 定位；
        ref 仅在本次返回的 snapshot 有效，页面变化后请重新 snapshot。
        frame: 指定 frame 索引（见 get_frames），None 表示主 frame。
        """
        def _do_snapshot(page):
            actual_tab = self._find_tab_index(page) or 0
            lines, ref_map = self._build_snapshot(page, actual_tab, frame)
            if not lines:
                return ToolResult("Snapshot empty: no accessible elements found", error=True)
            body = "\n".join(line for _indent, line in lines)
            if len(body) > _TRUNCATE_SNAPSHOT_CHARS:
                body = body[:_TRUNCATE_SNAPSHOT_CHARS] + "\n... (truncated)"
            fdesc = "" if frame is None else f", frame {frame}"
            return ToolResult(
                f"Accessibility snapshot (tab {actual_tab}{fdesc}, {len(ref_map)} refs):\n"
                f"---\n{body}\n---\n"
                f"Use ref to interact: click(ref=\"r1\"), fill(ref=\"r2\", text=\"...\"), "
                f"type(ref=\"r2\", text=\"...\"), press(ref=\"r3\", key=\"Enter\")",
                meta={"refs": ref_map},
            )

        return self._execute_operation("snapshot", _do_snapshot, tab_index=tab_index)

    def find(self, pattern: str, tab_index: int | None = None, frame: int | None = None) -> ToolResult:
        """在页面 accessibility snapshot 中搜索匹配 role 或 name 的元素。

        Args:
            pattern: 正则表达式或子串（大小写不敏感），匹配 role 或 name
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
            frame: 指定 frame 索引（见 get_frames），None 表示主 frame
        """
        def _do_find(page):
            actual_tab = self._find_tab_index(page) or 0
            lines, ref_map = self._build_snapshot(page, actual_tab, frame)
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

    def click(self, ref: str, button: str = "left", tab_index: int | None = None,
              frame: int | None = None) -> ToolResult:
        """点击 snapshot 中指定 ref 的元素（支持右键/中键）。"""
        def _do_click(page):
            actual_tab = self._find_tab_index(page) or 0
            entry = self._resolve_ref(actual_tab, ref, frame)
            base = self._resolve_frame(page, frame)
            loc = self._locator_for(base, entry)
            loc.click(button=button, timeout=_ACTION_TIMEOUT_MS)
            return ToolResult(
                f"Clicked [{ref}] ({entry['role']} \"{entry['name']}\") "
                f"[{button}]",
                meta={"tab_index": actual_tab},
            )

        return self._execute_operation("click", _do_click, tab_index=tab_index)

    def fill(self, ref: str, text: str, tab_index: int | None = None,
             frame: int | None = None) -> ToolResult:
        """向 snapshot 中指定 ref 的输入框填入文本（触发一次 input 事件）。"""
        def _do_fill(page):
            actual_tab = self._find_tab_index(page) or 0
            entry = self._resolve_ref(actual_tab, ref, frame)
            base = self._resolve_frame(page, frame)
            loc = self._locator_for(base, entry)
            loc.fill(text, timeout=_ACTION_TIMEOUT_MS)
            return ToolResult(
                f"Filled [{ref}] ({entry['role']} \"{entry['name']}\") with {len(text)} chars",
                meta={"tab_index": actual_tab},
            )

        return self._execute_operation("fill", _do_fill, tab_index=tab_index)

    def type(self, ref: str, text: str, tab_index: int | None = None,
             frame: int | None = None) -> ToolResult:
        """向 snapshot 中指定 ref 的元素逐键输入文本（触发按键级事件）。"""
        def _do_type(page):
            actual_tab = self._find_tab_index(page) or 0
            entry = self._resolve_ref(actual_tab, ref, frame)
            base = self._resolve_frame(page, frame)
            loc = self._locator_for(base, entry)
            loc.press_sequentially(text, delay=_TYPE_DELAY_MS)
            return ToolResult(
                f"Typed {len(text)} chars into [{ref}] ({entry['role']} \"{entry['name']}\")",
                meta={"tab_index": actual_tab},
            )

        return self._execute_operation("type", _do_type, tab_index=tab_index)

    def press(self, ref: str | None, key: str, tab_index: int | None = None,
              frame: int | None = None) -> ToolResult:
        """按指定键。

        Args:
            ref: snapshot 中的元素 ref；None 表示在页面全局按（page.keyboard.press）
            key: 按键名（Enter/Tab/Escape/ArrowDown/Backspace 等）
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
            frame: 指定 frame 索引（见 get_frames），None 表示主 frame
        """
        def _do_press(page):
            actual_tab = self._find_tab_index(page) or 0
            if ref:
                entry = self._resolve_ref(actual_tab, ref, frame)
                base = self._resolve_frame(page, frame)
                loc = self._locator_for(base, entry)
                loc.press(key)
                return ToolResult(
                    f"Pressed '{key}' on [{ref}] ({entry['role']} \"{entry['name']}\")",
                    meta={"tab_index": actual_tab},
                )
            # Frame 无独立 keyboard API：page 级按键统一走 page.keyboard（发给当前聚焦元素）
            page.keyboard.press(key)
            return ToolResult(f"Pressed '{key}' (page level)", meta={"tab_index": actual_tab})

        return self._execute_operation("press", _do_press, tab_index=tab_index)

    # ==================== 增强交互（scroll/hover/drag/upload/fill_form） ====================

    def scroll(self, direction: str = "down", amount: int = 800, ref: str | None = None,
               frame: int | None = None, tab_index: int | None = None) -> ToolResult:
        """滚动页面或滚动到指定元素。

        Args:
            direction: down/up（按 amount 像素滚动）/ top/bottom（到顶/到底）/ to_element（滚动到 ref）
            amount: down/up 的滚动像素量（默认 800）
            ref: direction='to_element' 时目标元素 ref
            frame: 指定 frame 索引（见 get_frames），None 表示主 frame
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        def _do_scroll(page):
            actual_tab = self._find_tab_index(page) or 0
            if direction == "to_element":
                if not ref:
                    return ToolResult("Error: 'ref' is required for scroll direction='to_element'", error=True)
                entry = self._resolve_ref(actual_tab, ref, frame)
                base = self._resolve_frame(page, frame)
                loc = self._locator_for(base, entry)
                loc.scroll_into_view_if_needed(timeout=_ACTION_TIMEOUT_MS)
                return ToolResult(
                    f"Scrolled [{ref}] ({entry['role']} \"{entry['name']}\") into view",
                    meta={"tab_index": actual_tab},
                )
            delta = {"up": -amount, "down": amount}.get(direction)
            if delta is not None:
                base = self._resolve_frame(page, frame)
                base.evaluate(f"window.scrollBy(0, {delta})")
                return ToolResult(f"Scrolled {direction} by {amount}px", meta={"tab_index": actual_tab})
            if direction == "top":
                base = self._resolve_frame(page, frame)
                base.evaluate("window.scrollTo(0, 0)")
                return ToolResult("Scrolled to top", meta={"tab_index": actual_tab})
            if direction == "bottom":
                base = self._resolve_frame(page, frame)
                base.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                return ToolResult("Scrolled to bottom", meta={"tab_index": actual_tab})
            return ToolResult(
                f"Error: unknown direction '{direction}' (use down/up/top/bottom/to_element)", error=True
            )

        return self._execute_operation("scroll", _do_scroll, tab_index=tab_index)

    def hover(self, ref: str, frame: int | None = None, tab_index: int | None = None) -> ToolResult:
        """悬停到 snapshot 中指定 ref 的元素。"""
        def _do_hover(page):
            actual_tab = self._find_tab_index(page) or 0
            entry = self._resolve_ref(actual_tab, ref, frame)
            base = self._resolve_frame(page, frame)
            loc = self._locator_for(base, entry)
            loc.hover(timeout=_ACTION_TIMEOUT_MS)
            return ToolResult(
                f"Hovered [{ref}] ({entry['role']} \"{entry['name']}\")",
                meta={"tab_index": actual_tab},
            )

        return self._execute_operation("hover", _do_hover, tab_index=tab_index)

    def drag(self, ref: str, target_ref: str, frame: int | None = None,
             tab_index: int | None = None) -> ToolResult:
        """把 snapshot 中 ref 元素拖拽到 target_ref 元素上。"""
        def _do_drag(page):
            actual_tab = self._find_tab_index(page) or 0
            base = self._resolve_frame(page, frame)
            src = self._resolve_ref(actual_tab, ref, frame)
            dst = self._resolve_ref(actual_tab, target_ref, frame)
            src_loc = self._locator_for(base, src)
            dst_loc = self._locator_for(base, dst)
            src_loc.drag_to(dst_loc, timeout=_ACTION_TIMEOUT_MS)
            return ToolResult(
                f"Dragged [{ref}] ({src['role']} \"{src['name']}\") onto [{target_ref}]",
                meta={"tab_index": actual_tab},
            )

        return self._execute_operation("drag", _do_drag, tab_index=tab_index)

    def upload(self, ref: str, path: str, frame: int | None = None,
               tab_index: int | None = None) -> ToolResult:
        """向 snapshot 中指定 ref 的文件输入框上传本地文件。"""
        def _do_upload(page):
            actual_tab = self._find_tab_index(page) or 0
            entry = self._resolve_ref(actual_tab, ref, frame)
            base = self._resolve_frame(page, frame)
            loc = self._locator_for(base, entry)
            loc.set_input_files(path, timeout=_ACTION_TIMEOUT_MS)
            return ToolResult(
                f"Uploaded file to [{ref}] ({entry['role']} \"{entry['name']}\"): {path}",
                meta={"tab_index": actual_tab},
            )

        return self._execute_operation("upload", _do_upload, tab_index=tab_index)

    def fill_form(self, fields: list[dict], frame: int | None = None,
                  tab_index: int | None = None) -> ToolResult:
        """批量填充表单：fields=[{ref, value}, ...]，单个 worker 任务内顺序填充。"""
        def _do_fill_form(page):
            actual_tab = self._find_tab_index(page) or 0
            base = self._resolve_frame(page, frame)
            results = []
            for field in fields or []:
                ref = field.get("ref")
                value = field.get("value")
                if not ref or value is None:
                    results.append(f"  [{ref}]: skipped (missing ref/value)")
                    continue
                entry = self._resolve_ref(actual_tab, ref, frame)
                loc = self._locator_for(base, entry)
                loc.fill(str(value), timeout=_ACTION_TIMEOUT_MS)
                results.append(
                    f"  Filled [{ref}] ({entry['role']} \"{entry['name']}\") with {len(str(value))} chars"
                )
            return ToolResult(
                f"Fill form ({len(fields or [])} fields):\n" + "\n".join(results),
                meta={"tab_index": actual_tab},
            )

        return self._execute_operation("fill_form", _do_fill_form, tab_index=tab_index)

    # ==================== 结构化提取（extract/extract_table） ====================

    def extract(self, selector: str, attribute: str = "text", limit: int = 50,
                frame: int | None = None, tab_index: int | None = None) -> ToolResult:
        """按 CSS 选择器批量提取元素文本或属性值。"""
        def _do_extract(page):
            base = self._resolve_frame(page, frame)
            js = (
                "({sel, attr, limit}) => Array.from(document.querySelectorAll(sel))"
                ".slice(0, limit)"
                ".map(el => attr === 'text' ? (el.textContent || '').trim() "
                ": (el.getAttribute(attr) ?? el[attr] ?? ''))"
            )
            try:
                # evaluate 只接受 1 个 arg，多个参数必须打包成一个对象
                data = base.evaluate(js, {"sel": selector, "attr": attribute, "limit": limit})
            except Exception as e:
                return ToolResult(f"Error: extract failed — {e}", error=True)
            if not data:
                return ToolResult(f"No elements match selector '{selector}'")
            lines = [f"{i + 1}. {str(v)}" for i, v in enumerate(data)]
            return ToolResult(
                f"Extracted {len(data)} element(s) via '{selector}' (attr={attribute}):\n"
                + "\n".join(lines)
            )

        return self._execute_operation("extract", _do_extract, tab_index=tab_index)

    def extract_table(self, index: int = 0, frame: int | None = None,
                      tab_index: int | None = None) -> ToolResult:
        """把页面第 index 个 <table> 转为 markdown 表格。"""
        def _do_extract_table(page):
            base = self._resolve_frame(page, frame)
            try:
                rows = base.evaluate(
                    "(i) => Array.from(document.querySelectorAll('table')[i].rows)"
                    ".map(r => Array.from(r.cells).map(c => (c.textContent || '').trim()))",
                    index,
                )
            except Exception as e:
                return ToolResult(f"Error: extract_table failed — {e}", error=True)
            if not rows:
                return ToolResult(f"No <table> elements found (or table {index} is empty)")
            if not rows[0]:
                return ToolResult(f"Table {index} has no cells")

            def _fmt_row(cells):
                return "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"

            header = _fmt_row(rows[0])
            sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
            body_lines = [_fmt_row(r) for r in rows[1:]]
            out = "\n".join([header, sep] + body_lines)
            if len(out) > _TRUNCATE_OUTPUT_CHARS:
                out = out[:_TRUNCATE_OUTPUT_CHARS] + "\n... (truncated)"
            return ToolResult(f"Table {index} ({len(rows) - 1} data rows):\n{out}")

        return self._execute_operation("extract_table", _do_extract_table, tab_index=tab_index)

    # ==================== 下载（download） ====================

    def download(self, ref: str | None = None, url: str | None = None, save_path: str | None = None,
                 tab_index: int | None = None) -> ToolResult:
        """下载文件：点击 ref 触发，或直接导航到下载 URL。

        Args:
            ref: 触发下载的元素 ref（点击后等待下载）
            url: 直接导航到的下载 URL（与 ref 二选一）
            save_path: 保存路径（绝对路径，由工具层解析）；缺省用 suggested_filename
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
        def _do_download(page):
            actual_tab = self._find_tab_index(page) or 0
            with page.expect_download(timeout=_DOWNLOAD_TIMEOUT_MS) as dl_info:
                if url:
                    page.goto(url, wait_until="load", timeout=_NAV_TIMEOUT_MS)
                elif ref:
                    entry = self._resolve_ref(actual_tab, ref)
                    loc = self._locator_for(page, entry)
                    loc.click(timeout=_ACTION_TIMEOUT_MS)
                else:
                    return ToolResult("Error: download requires 'ref' (trigger) or 'url'", error=True)
            download = dl_info.value
            suggested = download.suggested_filename
            if save_path:
                abs_path = save_path if os.path.isabs(save_path) else os.path.join(self._project_root, save_path)
            else:
                abs_path = os.path.join(self._project_root, suggested)
            download.save_as(abs_path)
            return ToolResult(
                f"Downloaded '{suggested}' -> {abs_path} (tab {actual_tab})",
                meta={"tab_index": actual_tab, "file": abs_path, "filename": suggested},
            )

        return self._execute_operation("download", _do_download, tab_index=tab_index,
                                        timeout=_DOWNLOAD_WORKER_TIMEOUT)

    # ==================== 弹窗（dialogs） ====================

    def dialogs(self, clear: bool = False, tab_index: int | None = None) -> ToolResult:
        """返回该 tab 捕获的 JS 弹窗（alert/confirm/prompt）。弹窗已被自动关闭。"""
        def _do_dialogs(page):
            actual_tab = self._find_tab_index(page) or 0
            buf = self._dialog_buffers.get(actual_tab)
            if not buf:
                return ToolResult("No dialogs captured yet.")
            msgs = list(buf)
            if clear:
                buf.clear()
            if not msgs:
                return ToolResult("No dialogs captured.")
            lines = [f"[{t}] {m}" for t, m in msgs]
            return ToolResult(
                f"Dialogs (tab {actual_tab}, {len(lines)}):\n" + "\n".join(lines)
            )

        return self._execute_operation("dialogs", _do_dialogs, tab_index=tab_index)

    # ==================== iframe（get_frames） ====================

    def get_frames(self, tab_index: int | None = None) -> ToolResult:
        """列出当前页面的所有 frame（index 0 为主 frame，其余为 iframe）。"""
        def _do_get_frames(page):
            frames = page.frames
            if not frames:
                return ToolResult("No frames found")
            lines = []
            for i, f in enumerate(frames):
                name = f.name or "(unnamed)"
                url = f.url or "(no url)"
                tag = " [MAIN]" if i == 0 else ""
                lines.append(f"  frame {i}: {name}{tag}\n    URL: {url}")
            return ToolResult(f"Frames ({len(lines)}):\n\n" + "\n".join(lines))

        return self._execute_operation("get_frames", _do_get_frames, tab_index=tab_index)

    # ==================== cookies（get/clear/set） ====================

    def get_cookies(self, url: str | None = None, tab_index: int | None = None) -> ToolResult:
        """返回当前浏览器 context 的 cookies（可选按 url 过滤）。"""
        def _do_get_cookies(page):
            ctx = page.context
            cookies = ctx.cookies(url=url) if url else ctx.cookies()
            if not cookies:
                return ToolResult("No cookies found")
            lines = [
                f"  {c['name']} = {c['value']} "
                f"(domain={c.get('domain')}, path={c.get('path')}, "
                f"secure={c.get('secure')}, httpOnly={c.get('httpOnly')}, sameSite={c.get('sameSite')})"
                for c in cookies
            ]
            return ToolResult(f"Cookies ({len(cookies)}):\n" + "\n".join(lines))

        return self._execute_operation("get_cookies", _do_get_cookies, tab_index=tab_index)

    def clear_cookies(self, tab_index: int | None = None) -> ToolResult:
        """清除当前浏览器 context 的全部 cookies。"""
        def _do_clear_cookies(page):
            ctx = page.context
            ctx.clear_cookies()
            return ToolResult("All cookies cleared")

        return self._execute_operation("clear_cookies", _do_clear_cookies, tab_index=tab_index)

    def set_cookie(self, cookie: dict, tab_index: int | None = None) -> ToolResult:
        """设置一个 cookie：{name, value, domain, path, ...}（Playwright Cookie 字段）。"""
        def _do_set_cookie(page):
            ctx = page.context
            ctx.add_cookies([cookie])
            return ToolResult(f"Cookie set: {cookie.get('name')} (domain={cookie.get('domain')})")

        return self._execute_operation("set_cookie", _do_set_cookie, tab_index=tab_index)

    # ==================== 导航（go_back/go_forward/reload） ====================

    def go_back(self, tab_index: int | None = None) -> ToolResult:
        """返回上一页。"""
        def _do_go_back(page):
            resp = page.go_back(wait_until="load", timeout=_HISTORY_TIMEOUT_MS)
            return ToolResult(f"Navigated back to {resp.url}" if resp else "No history to go back to")

        return self._execute_operation("go_back", _do_go_back, tab_index=tab_index)

    def go_forward(self, tab_index: int | None = None) -> ToolResult:
        """前进到下一页。"""
        def _do_go_forward(page):
            resp = page.go_forward(wait_until="load", timeout=_HISTORY_TIMEOUT_MS)
            return ToolResult(f"Navigated forward to {resp.url}" if resp else "No forward history")

        return self._execute_operation("go_forward", _do_go_forward, tab_index=tab_index)

    def reload(self, tab_index: int | None = None) -> ToolResult:
        """重新加载当前页面。"""
        def _do_reload(page):
            page.reload(wait_until="load", timeout=_HISTORY_TIMEOUT_MS)
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

    def requests(self, clear: bool = False, details: bool = False, body: bool = False,
                 tab_index: int | None = None) -> ToolResult:
        """返回该 tab 捕获的网络请求。

        Args:
            clear: 读取后清空缓冲
            details: True 时展开请求/响应头（默认仅 方法 URL 状态码）
            body: True 时展开捕获到的响应 body（受 content-type/length 阈值限制）
            tab_index: 指定 tab 编号，None 表示使用活跃 tab
        """
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
            if details:
                lines = []
                for r in reqs:
                    head = f"{r['method']} {r['url']} {r['status']}"
                    if body and r.get("body_text"):
                        head += "\n  body: " + r["body_text"][:500]
                    if r.get("req_headers"):
                        head += "\n  req headers: " + json.dumps(r["req_headers"], ensure_ascii=False)[:800]
                    if r.get("resp_headers"):
                        head += "\n  resp headers: " + json.dumps(r["resp_headers"], ensure_ascii=False)[:800]
                    lines.append(head)
            else:
                lines = [f"{r['method']} {r['url']} {r['status']}" for r in reqs]
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


def stop_browser_service() -> None:
    """停止全局浏览器服务。"""
    global _service
    if _service:
        _service.stop()
        _service = None
