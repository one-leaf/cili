"""出站文件队列：``send_user_file`` 工具入队 → 接入端回合末出队。

放在 core 而不是某个接入端里，是因为**工具在 runner 线程跑、接入端在另一个
线程取**，需要一个接口无关的交接点：工具不知道（也不该知道）当前是 Web 还是
QQ 在驱动会话。

生命周期：回合结束后由接口层取走（``drain_files``）并交给当前接入端——Web 推
file 帧 + 落 file_card 消息（``web/sse.drain_outbox_events``），QQ/微信走富媒体
上传。

容量与条数
----------
「每回合能发几个」是**接口事实**，由 ``InterfaceProfile.max_files_per_turn``
声明、工具在登记时校验（见 ``core/tools/send_user_file.py``）。本模块的容量只是
**兜底**（``DEFAULT_MAX_PENDING_FILES``），防模型绕开校验狂发；队列里的条目在同
回合末被取走，所以它同时就是「本回合待发集合」。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 兜底容量：档案未声明 max_files_per_turn（如 Web）时生效。队列只增不减会吃内存
# （若接入端始终不来取），且交付层的配额有限，排太多也发不完。
DEFAULT_MAX_PENDING_FILES = 32


@dataclass(frozen=True)
class OutboxFile:
    """一个待发给用户的文件。"""

    path: str        # 绝对路径
    note: str = ""   # 可选的附带说明（不保证被接入端使用）
    enqueued_at: float = 0.0


@dataclass
class EnqueueResult:
    """一次批量登记的结果。"""

    accepted: list[str] = field(default_factory=list)    # 实际入队的路径
    duplicates: list[str] = field(default_factory=list)  # 已在队列中、跳过的路径
    overflow: bool = False                               # 容量不足（整批未入队）


_lock = threading.Lock()
_pending: dict[str, list[OutboxFile]] = {}


def enqueue_files(
    session_key: str,
    entries: list[tuple[str, str]],
    *,
    limit: int | None = None,
) -> EnqueueResult:
    """批量登记待发文件（原子）。

    - **去重**：同一会话内已在队列里的路径跳过（模型重复登记同一产物只交付一次，
      保留首次的 note）。
    - **容量**：``limit``（当前接口的每回合条数上限）或兜底值。超出时**整批拒绝**
      （``overflow=True``）而不是截断——截断会让工具回执与事实不符。
    """
    if not session_key or not entries:
        return EnqueueResult()

    cap = limit if limit and limit > 0 else DEFAULT_MAX_PENDING_FILES
    accepted: list[str] = []
    duplicates: list[str] = []
    with _lock:
        bucket = _pending.setdefault(session_key, [])
        known = {os.path.normcase(f.path) for f in bucket}
        fresh: list[tuple[str, str]] = []
        for path, note in entries:
            if not path:
                continue
            key = os.path.normcase(path)
            if key in known:
                duplicates.append(path)
                continue
            known.add(key)
            fresh.append((path, note))

        if len(bucket) + len(fresh) > cap:
            logger.warning(
                f"[outbox] 会话 {session_key} 待发文件将超出容量 {cap}"
                f"（已有 {len(bucket)}，本次 {len(fresh)}），整批拒绝"
            )
            return EnqueueResult(duplicates=duplicates, overflow=True)

        now = time.time()
        for path, note in fresh:
            bucket.append(OutboxFile(path=path, note=note, enqueued_at=now))
            accepted.append(path)

    return EnqueueResult(accepted=accepted, duplicates=duplicates)


def enqueue_file(
    session_key: str,
    path: str,
    note: str = "",
    *,
    limit: int | None = None,
) -> bool:
    """单件登记（``enqueue_files`` 的薄封装）。返回是否真的入队。"""
    if not session_key or not path:
        return False
    return bool(enqueue_files(session_key, [(path, note)], limit=limit).accepted)


def drain_files(session_key: str) -> list[OutboxFile]:
    """取走并清空该会话的全部待发文件（原子）。"""
    with _lock:
        return _pending.pop(session_key, [])


def pending_count(session_key: str) -> int:
    with _lock:
        return len(_pending.get(session_key, ()))


def build_delivery_notice(
    delivered: list[str], skipped: list[str], reason: str = ""
) -> tuple[str, dict]:
    """构造交付回执（消息内容 + ``_meta``）。

    交付层受配额约束时可能发不完（QQ 被动回复、微信 token 预算），回执把
    「实际发了哪些 / 哪些没发」落进会话——用户在 Web 里回看这条会话时也看得见。
    与 ``error_notice`` / ``file_card`` 同机制：UI 可见、不进 LLM 上下文。
    """
    def _names(paths: list[str]) -> str:
        return ", ".join(os.path.basename(p) for p in paths)

    parts: list[str] = []
    if delivered:
        parts.append(f"已交付 {len(delivered)} 个文件：{_names(delivered)}")
    if skipped:
        tail = f"（{reason}）" if reason else ""
        parts.append(f"未发出 {len(skipped)} 个：{_names(skipped)}{tail}")
    content = f"📎 {'；'.join(parts)}" if parts else ""
    meta = {
        "delivery_notice": {
            "delivered": list(delivered),
            "skipped": list(skipped),
            "reason": reason,
        }
    }
    return content, meta


def record_delivery_notice(
    session, delivered: list[str], skipped: list[str], reason: str = ""
) -> None:
    """把交付回执落进会话（UI 可见、不进 LLM 上下文）。失败只记日志，不打断交付。"""
    content, meta = build_delivery_notice(delivered, skipped, reason)
    if not content:
        return
    try:
        session.add_message("assistant", content, meta=meta, flush=True)
    except Exception:  # noqa: BLE001 —— 回执落盘失败绝不能影响回合收尾
        logger.exception("[outbox] 交付回执落盘失败")


def clear(session_key: str | None = None) -> None:
    """清空队列（供测试与接入端停机使用）。"""
    with _lock:
        if session_key is None:
            _pending.clear()
        else:
            _pending.pop(session_key, None)
