"""出站文件队列：``send_file`` 工具入队 → 接入端回合末出队。

放在 core 而不是某个接入端里，是因为**工具在 runner 线程跑、接入端在另一个
线程取**，需要一个接口无关的交接点：工具不知道（也不该知道）当前是 Web 还是
QQ 在驱动会话。

生命周期：回合结束后由 ``core.turn_dispatch`` 调 ``drain_files`` 取走并交给当前
接入端。Web 目前取走后丢弃并记日志（渲染下载链接留作后续）；QQ 会真发出去。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 单会话待发队列上限。队列只增不减会吃内存（若接入端始终不来取），
# 且 QQ 侧受被动回复配额限制（一条用户消息只能发 4 条），排太多也发不完。
MAX_PENDING_FILES = 8


@dataclass(frozen=True)
class OutboxFile:
    """一个待发给用户的文件。"""

    path: str        # 绝对路径
    note: str = ""   # 可选的附带说明（不保证被接入端使用）
    enqueued_at: float = 0.0


_lock = threading.Lock()
_pending: dict[str, list[OutboxFile]] = {}


def enqueue_file(session_key: str, path: str, note: str = "") -> bool:
    """把文件登记为「发给用户」。返回 False 表示队列已满（调用方应告知模型）。"""
    if not session_key or not path:
        return False
    with _lock:
        bucket = _pending.setdefault(session_key, [])
        if len(bucket) >= MAX_PENDING_FILES:
            logger.warning(
                f"[outbox] 会话 {session_key} 待发文件已达上限 {MAX_PENDING_FILES}，拒绝 {path}"
            )
            return False
        bucket.append(OutboxFile(path=path, note=note, enqueued_at=time.time()))
    return True


def drain_files(session_key: str) -> list[OutboxFile]:
    """取走并清空该会话的全部待发文件（原子）。"""
    with _lock:
        return _pending.pop(session_key, [])


def pending_count(session_key: str) -> int:
    with _lock:
        return len(_pending.get(session_key, ()))


def clear(session_key: str | None = None) -> None:
    """清空队列（供测试与接入端停机使用）。"""
    with _lock:
        if session_key is None:
            _pending.clear()
        else:
            _pending.pop(session_key, None)
