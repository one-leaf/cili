"""接口无关的会话输入队列（TurnQueue）。

路由只入队（``enqueue``），runner 在**安全边界**消费（``drain_batch``）：
迭代顶部（stop 检查与后台通知排空之后）与「模型本来要结束回合」的出口。
队列按 ``workspace_uuid:session_id`` 分键、FIFO、默认批量合并投递——同一
边界取当前全部可投递项合成一条 user 消息，减少空转。

状态放服务端而非浏览器：刷新、切会话、多标签、换接入端都不丢队列。
P1 仅内存存储；P4 可持久化到 ``{session_dir}/queue.json``。

设计文档：``workspace/default/cili-input-queue-plan-2026-10-29.md``。
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.event_bus import get_event_bus

logger = logging.getLogger(__name__)

# 容量约束（超限入队被拒，返回中文文案）
MAX_ITEMS = 20
MAX_TEXT = 8000
MAX_MERGED_TEXT = 32000
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_PREVIEW_MAX = 200


class QueueError(Exception):
    """入队被拒（超上限 / 超长 / 超字节）；message 可直接展示给用户。"""


@dataclass
class QueuedTurn:
    """一条排队项 = 一次用户回合输入（未投递前不写入 SessionStore）。"""

    item_id: str
    content: str                          # 纯文本预览
    user_input: Any                       # str 或 content blocks（含图片）
    interface: str = "web"
    client_id: str | None = None
    created_at: str = ""
    has_images: bool = False

    def preview(self) -> dict:
        """UI/HTTP 快照：不含 base64 大对象，避免广播放大。"""
        text = self.content or ""
        return {
            "item_id": self.item_id,
            "content": text[:_PREVIEW_MAX],
            "truncated": len(text) > _PREVIEW_MAX,
            "has_images": self.has_images,
            "interface": self.interface,
            "created_at": self.created_at,
        }


@dataclass
class _SessionQueue:
    items: list[QueuedTurn] = field(default_factory=list)
    paused: bool = False
    reason: str = ""


def _image_bytes(user_input: Any) -> int:
    """估算 content blocks 中图片 base64 的字节数（P1 只按字符数计）。"""
    if not isinstance(user_input, list):
        return 0
    total = 0
    for block in user_input:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        total += len(str((block.get("source") or {}).get("data", "")))
    return total


def _split_key(key: str) -> tuple[str, str]:
    ws, _, sid = key.partition(":")
    return ws, sid


def new_item_id() -> str:
    """队列项 ID（服务端生成，队列内唯一）。"""
    return f"q_{uuid.uuid4().hex[:8]}"


def merge_queued_turns(items: list[QueuedTurn]) -> tuple[Any, dict]:
    """把一批排队项合并成一条 user 消息的入参 + 审计 meta。

    - 纯文本：各条 ``content`` 按 FIFO 以空行连接成一个字符串。
    - 含图片：按条目前后顺序拼 content blocks（文本块 + 图片块）；
      纯图片项不产生空文本块。
    """
    texts = [it.content for it in items if it.content]
    has_images = any(it.has_images for it in items)
    if has_images:
        blocks: list[dict] = []
        for it in items:
            if it.content:
                blocks.append({"type": "text", "text": it.content, "_valid": True})
            if isinstance(it.user_input, list):
                blocks.extend(
                    b for b in it.user_input
                    if isinstance(b, dict) and b.get("type") == "image"
                )
        user_input: Any = blocks if blocks else "\n\n".join(texts)
    else:
        user_input = "\n\n".join(texts)
    meta = {
        "queued_ids": [it.item_id for it in items],
        "queued_interfaces": [it.interface for it in items],
        "merged_count": len(items),
    }
    return user_input, meta


class TurnQueue:
    """按会话分键的输入队列；线程安全（``threading.RLock``）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._queues: dict[str, _SessionQueue] = {}

    # ── 写入 ─────────────────────────────────────────────────────

    def enqueue(self, key: str, item: QueuedTurn) -> QueuedTurn:
        """入队；同 ``client_id`` 重复提交返回原项（网络重试幂等）。

        暂停状态下入队视为「恢复」（设计 §3.4：直接发新消息 = 恢复）。
        超限抛 ``QueueError``，不入队。
        """
        with self._lock:
            q = self._queues.setdefault(key, _SessionQueue())
            if item.client_id:
                for existing in q.items:
                    if existing.client_id == item.client_id:
                        return existing
            if len(q.items) >= MAX_ITEMS:
                raise QueueError(f"排队消息已达上限（{MAX_ITEMS} 条），请等待执行或先清空队列。")
            if len(item.content or "") > MAX_TEXT:
                raise QueueError(f"单条排队消息过长（上限 {MAX_TEXT} 字符）。")
            if _image_bytes(item.user_input) > MAX_IMAGE_BYTES:
                raise QueueError("排队图片总大小超过 8MB，请减少图片后重试。")
            if not item.item_id:
                item.item_id = new_item_id()
            if not item.created_at:
                item.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            q.items.append(item)
            if q.paused:
                q.paused = False
                q.reason = ""
            self._publish(key, q)
            return item

    def remove(self, key: str, item_id: str) -> bool:
        with self._lock:
            q = self._queues.get(key)
            if not q:
                return False
            for idx, it in enumerate(q.items):
                if it.item_id == item_id:
                    q.items.pop(idx)
                    self._publish(key, q)
                    return True
            return False

    def clear(self, key: str) -> int:
        with self._lock:
            q = self._queues.get(key)
            if not q:
                return 0
            count = len(q.items)
            q.items = []
            q.paused = False
            q.reason = ""
            self._publish(key, q)
            return count

    def pause(self, key: str, reason: str = "stopped") -> None:
        with self._lock:
            q = self._queues.setdefault(key, _SessionQueue())
            q.paused = True
            q.reason = reason
            self._publish(key, q)

    def resume(self, key: str) -> None:
        with self._lock:
            q = self._queues.get(key)
            if not q or (not q.paused):
                return
            q.paused = False
            q.reason = ""
            self._publish(key, q)

    def remove_session(self, key: str) -> None:
        """删除会话时清理（不广播：会话已不存在，订阅者无需刷新）。"""
        with self._lock:
            self._queues.pop(key, None)

    # ── 读取 ─────────────────────────────────────────────────────

    def snapshot(self, key: str) -> dict:
        with self._lock:
            q = self._queues.get(key)
            if not q:
                return {"items": [], "paused": False, "reason": ""}
            return {
                "items": [it.preview() for it in q.items],
                "paused": q.paused,
                "reason": q.reason,
            }

    def has_items(self, key: str) -> bool:
        with self._lock:
            q = self._queues.get(key)
            return bool(q and q.items)

    def is_paused(self, key: str) -> bool:
        with self._lock:
            q = self._queues.get(key)
            return bool(q and q.paused)

    def drain_batch(self, key: str, max_text: int = MAX_MERGED_TEXT) -> list[QueuedTurn]:
        """取走当前队列的一批可投递项（FIFO + 合并预算）。

        **只允许 runner 线程（或持有会话执行权的后台泵）调用**：与 ``enqueue``
        同一把锁，保证取走期间不会插队；暂停时返回空列表（停止语义：不自动
        开跑下一批）。
        """
        with self._lock:
            q = self._queues.get(key)
            if not q or q.paused or not q.items:
                return []
            taken: list[QueuedTurn] = []
            total = 0
            for it in q.items:
                n = len(it.content or "")
                if taken and total + n > max_text:
                    break
                taken.append(it)
                total += n
            q.items = q.items[len(taken):]
            # 先广播空快照让队列条清空，再丢弃空且未暂停的键
            self._publish(key, q)
            if not q.items and not q.paused:
                self._queues.pop(key, None)
            return taken

    # ── 广播 ─────────────────────────────────────────────────────

    def _publish(self, key: str, q: _SessionQueue) -> None:
        """变更后广播 ``queue_update``（多标签/多接入端同步的单一来源）。"""
        ws, sid = _split_key(key)
        try:
            get_event_bus().publish({
                "type": "queue_update",
                "workspace_uuid": ws,
                "session_id": sid,
                "items": [it.preview() for it in q.items],
                "paused": q.paused,
                "reason": q.reason,
            })
        except Exception:  # noqa: BLE001 —— 广播失败不影响队列本身
            logger.debug("[turn_queue] queue_update 广播失败", exc_info=True)


# 模块级单例（各接入端共享同一份队列，与 SessionRegistry 同一生命周期策略）
queue = TurnQueue()
