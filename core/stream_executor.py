"""流式/长任务专用线程池。

为什么要独立于默认 executor：SSE 的队列轮询（阻塞 ``queue.get``）与整回合执行
都是**长生命周期、且不可取消**的工作（见 ``web/sse.py`` 的注释），却和请求期短
任务（如会话列表的 meta 读取）共用事件循环的默认 executor。默认 executor 只有
``min(32, cpu+4)`` 个 worker，被长任务占满后短任务的提交会**永久排队**——症状是
主页加载卡死（``/api/workspaces/{uuid}/sessions`` 拿不到线程）。

分工：长任务走本池，短任务继续用默认 executor，两者互不挤占。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

# 每条 SSE 流占 1 个 worker，每个进行中的回合占 1 个。浏览器同源并发上限是 6，
# 多标签/多客户端累加；32 给足余量，同时把线程数封顶避免无界增长。
MAX_STREAM_WORKERS = 32

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def get_stream_executor() -> ThreadPoolExecutor:
    """取（或惰性创建）流式/长任务专用线程池（进程级单例）。"""
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=MAX_STREAM_WORKERS, thread_name_prefix="cili-stream"
            )
        return _executor


def shutdown_stream_executor() -> None:
    """关闭专用池（不等待阻塞中的 worker——它们最多阻塞到 ``queue.get`` 超时）。"""
    global _executor
    with _lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
