"""专用工作线程执行器：单线程串行执行，每次调用独立结果槽位。

Playwright 的同步 API 必须在同一线程调用；执行器负责懒启动、换代重建、
超时回调与在 worker 内部直接执行（避免死锁）。业务状态清理仍由调用方完成。
"""

from __future__ import annotations

import logging
import queue
import threading

logger = logging.getLogger(__name__)


class WorkerExecutor:
    def __init__(self, *, name: str, task_timeout: float, on_timeout,
                 before_task=None):
        self._name = name
        self._task_timeout = task_timeout
        self._on_timeout = on_timeout
        self._before_task = before_task

        self._thread: threading.Thread | None = None
        self._queue: queue.Queue = queue.Queue()
        self._generation = 0
        # 每次提交分配独立结果槽位（task_id → Event / 结果）
        self._task_lock = threading.Lock()
        self._events: dict[int, threading.Event] = {}
        self._results: dict[int, tuple[bool, object]] = {}
        self._counter = 0
        # RLock：submit 持锁提交，ensure_started 内部再次加锁
        self.start_lock = threading.RLock()

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    @property
    def generation(self) -> int:
        return self._generation

    def ensure_started(self) -> None:
        """启动工作线程；每次启动分配新队列与代号（换代）。"""
        with self.start_lock:
            self._ensure_started_locked()

    def _ensure_started_locked(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._generation += 1
        generation = self._generation
        task_queue: queue.Queue = queue.Queue()
        self._queue = task_queue
        self._thread = threading.Thread(
            target=self._loop,
            args=(task_queue, generation),
            name=f"{self._name}-Worker-{generation}",
            daemon=True,
        )
        self._thread.start()
        logger.debug("[BrowserWorker] Worker thread started (generation %s)", generation)

    def _loop(self, task_queue: queue.Queue, generation: int) -> None:
        """工作线程主循环；换代后旧线程按 sentinel/代号退出。"""
        while True:
            try:
                item = task_queue.get()
                if item is None or (isinstance(item, tuple) and item and item[0] is None):
                    break
                if generation != self._generation:
                    break

                task_id, task_func, task_args, task_kwargs = item
                if self._before_task is not None:
                    try:
                        self._before_task()
                    except Exception as e:  # noqa: BLE001
                        logger.debug("[BrowserWorker] before_task error (non-fatal): %s", e)

                try:
                    task_result = (True, task_func(*task_args, **task_kwargs))
                except Exception as e:  # noqa: BLE001
                    logger.warning("[BrowserWorker] Worker task error: %s", e)
                    task_result = (False, e)

                # 调用方已超时放弃则不存（避免泄漏）
                with self._task_lock:
                    event = self._events.pop(task_id, None)
                    if event is not None:
                        self._results[task_id] = task_result
                if event is not None:
                    event.set()
            except Exception as e:  # noqa: BLE001
                logger.error("[BrowserWorker] Worker loop error: %s", e, exc_info=True)

    def _next_task_id(self) -> int:
        with self._task_lock:
            self._counter += 1
            return self._counter

    def submit(self, func, *args, timeout: float | None = None, **kwargs):
        """在工作线程执行函数；worker 内部调用时直接执行。"""
        if threading.current_thread() is self._thread:
            return func(*args, **kwargs)

        # 启动线程与提交任务在同一把锁内：换代期间不会投进废弃队列
        with self.start_lock:
            self._ensure_started_locked()
            task_id = self._next_task_id()
            event = threading.Event()
            with self._task_lock:
                self._events[task_id] = event
            self._queue.put((task_id, func, args, kwargs))

        effective_timeout = timeout if timeout is not None else self._task_timeout
        if not event.wait(timeout=effective_timeout):
            with self._task_lock:
                self._events.pop(task_id, None)
            if self._on_timeout is not None:
                self._on_timeout(
                    f"worker task timeout ({effective_timeout:.0f}s)"
                )
            raise TimeoutError(f"Task execution timeout ({effective_timeout:.0f}s)")

        with self._task_lock:
            success, result = self._results.pop(task_id)
        if not success:
            raise result
        return result

    def replace_locked(self) -> None:
        """调用方持 ``start_lock`` 时换代：丢弃旧队列并启动新线程。"""
        old_queue = self._queue
        self._thread = None
        try:
            old_queue.put(None)
        except Exception:  # noqa: BLE001
            pass
        self._ensure_started_locked()

    def stop(self, join_timeout: float = 5) -> bool:
        """发送退出信号并等待；返回线程是否仍存活。"""
        thread = self._thread
        if thread is None:
            return False
        try:
            self._queue.put(None)
        except Exception:  # noqa: BLE001
            pass
        thread.join(timeout=join_timeout)
        alive = thread.is_alive()
        # 无论是否退出都摘除引用：卡死线程被放弃后，下一次 submit 应换代新建，
        # 而不是把任务排到旧队列里等满超时。
        self._thread = None
        return alive
