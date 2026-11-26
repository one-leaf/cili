"""全局收件箱：持久化的用户通知。

与 ``agent_mailbox`` / ``outbox`` 的分工：

- ``agent_mailbox``：agent ↔ agent 的进程内消息，会注入 LLM 上下文；
- ``outbox``：工具 → 接入端的文件交付临时队列；
- 本模块：AI / 系统 → 用户的持久通知，用户可见、可回看、有已读状态，
  **不进入 LLM 上下文**，只由 Web 读写。

存储位置 ``{DATA_DIR}/inbox/inbox.json``，单进程 + RLock，整文件原子写。
规模由 ``MAX_ITEMS`` 与 ``DELETED_RETENTION_DAYS`` 约束。
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.fs_utils import atomic_write_json, load_json_or_backup
from core.time_utils import now_str, parse_ts

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_ITEMS = 500
DELETED_RETENTION_DAYS = 30
MAX_TITLE_CHARS = 120
MAX_TOOL_BODY_CHARS = 4000
MAX_SYSTEM_BODY_CHARS = 2000
MAX_PER_SESSION_PER_MINUTE = 10

LEVELS = frozenset({"info", "success", "warning", "error"})
SOURCE_TYPES = frozenset({"tool", "cron", "workflow", "system"})
STATUSES = frozenset({"active", "unread", "read", "deleted", "all"})

ITEM_ID_RE = re.compile(r"^inb_[A-Za-z0-9_]+$")


def _new_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"inb_{stamp}_{uuid.uuid4().hex[:8]}"


def _truncate(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value[:limit] if len(value) > limit else value


def _excerpt(body: str, limit: int = 200) -> str:
    value = re.sub(r"\s+", " ", str(body or "")).strip()
    return value[:limit] + ("…" if len(value) > limit else "")


def _sort_key(item: "InboxItem") -> tuple[datetime, str]:
    return (parse_ts(item.created_at) or datetime.min, item.id)


@dataclass(frozen=True)
class InboxItem:
    """一条收件箱通知。"""

    id: str
    created_at: str
    level: str
    title: str
    body: str
    source: dict[str, str]
    workspace_uuid: str = ""
    workspace_name: str = ""
    session_id: str = ""
    session_title: str = ""
    dedupe_key: str = ""
    payload: dict[str, Any] | None = None
    read_at: str = ""
    deleted_at: str = ""

    @property
    def is_unread(self) -> bool:
        return not self.read_at and not self.deleted_at

    @property
    def is_deleted(self) -> bool:
        return bool(self.deleted_at)

    def to_dict(self, *, include_body: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "created_at": self.created_at,
            "level": self.level,
            "title": self.title,
            "source": dict(self.source),
            "workspace_uuid": self.workspace_uuid,
            "workspace_name": self.workspace_name,
            "session_id": self.session_id,
            "session_title": self.session_title,
            "read_at": self.read_at,
            "deleted_at": self.deleted_at,
        }
        if include_body:
            data["body"] = self.body
        else:
            data["excerpt"] = _excerpt(self.body)
        return data

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "level": self.level,
            "title": self.title,
            "body": self.body,
            "source": dict(self.source),
            "workspace_uuid": self.workspace_uuid,
            "workspace_name": self.workspace_name,
            "session_id": self.session_id,
            "session_title": self.session_title,
            "dedupe_key": self.dedupe_key,
            "payload": dict(self.payload or {}),
            "read_at": self.read_at,
            "deleted_at": self.deleted_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "InboxItem | None":
        if not isinstance(data, dict):
            return None
        item_id = str(data.get("id") or "")
        if not ITEM_ID_RE.match(item_id):
            return None
        level = str(data.get("level") or "info")
        if level not in LEVELS:
            level = "info"
        source = data.get("source")
        if not isinstance(source, dict):
            source = {}
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        return cls(
            id=item_id,
            created_at=str(data.get("created_at") or ""),
            level=level,
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            source={str(k): str(v) for k, v in source.items()},
            workspace_uuid=str(data.get("workspace_uuid") or ""),
            workspace_name=str(data.get("workspace_name") or ""),
            session_id=str(data.get("session_id") or ""),
            session_title=str(data.get("session_title") or ""),
            dedupe_key=str(data.get("dedupe_key") or ""),
            payload=payload,
            read_at=str(data.get("read_at") or ""),
            deleted_at=str(data.get("deleted_at") or ""),
        )


class InboxStore:
    """收件箱存储。所有公开方法线程安全，写盘失败向上抛异常。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._items: list[InboxItem] = []
        self._loaded = False

    # ── 读写 ──────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        raw = load_json_or_backup(
            self.path, {"schema_version": SCHEMA_VERSION, "items": []}
        )
        items: list[InboxItem] = []
        if isinstance(raw, dict):
            for entry in raw.get("items") or []:
                item = InboxItem.from_dict(entry)
                if item is not None:
                    items.append(item)
        pruned = _prune_items(items)
        self._items = pruned
        self._loaded = True
        if len(pruned) != len(items):
            try:
                self._save_items(pruned)
            except Exception:  # noqa: BLE001 —— 启动清理失败不阻塞使用
                logger.exception("[inbox] 启动清理后写盘失败")

    def _save_items(self, items: list[InboxItem]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "items": [item.to_storage_dict() for item in items],
            },
        )

    def _commit(self, items: list[InboxItem], action: str) -> None:
        ordered = sorted(items, key=_sort_key, reverse=True)
        self._save_items(ordered)
        self._items = ordered
        self._publish(action)

    def _publish(self, action: str) -> None:
        try:
            from core.event_bus import get_event_bus

            # 纯信号：不带标题/正文/工作区/条目 ID，避免青少年模式下
            # 通过广播泄露受限条目的存在性；数据由 /api/inbox 过滤后重拉。
            get_event_bus().publish({
                "type": "inbox_changed",
                "broadcast": True,
                "action": action,
            })
        except Exception:  # noqa: BLE001 —— 事件失败不影响收件箱写入
            logger.debug("[inbox] 发布变更事件失败: %s", action, exc_info=True)

    # ── 写入 ──────────────────────────────────────────────

    def add(
        self,
        title: str,
        body: str = "",
        *,
        level: str = "info",
        source_type: str = "system",
        agent: str = "",
        interface: str = "",
        workspace_uuid: str = "",
        workspace_name: str = "",
        session_id: str = "",
        session_title: str = "",
        dedupe_key: str = "",
        payload: dict[str, Any] | None = None,
        max_body_chars: int = MAX_SYSTEM_BODY_CHARS,
    ) -> InboxItem | None:
        """新增一条通知；``dedupe_key`` 命中时返回已有条目（不修改内容）。"""
        safe_title = _truncate(title, MAX_TITLE_CHARS)
        if not safe_title:
            raise ValueError("通知标题不能为空")

        with self._lock:
            self._ensure_loaded()
            if dedupe_key:
                for existing in self._items:
                    if existing.dedupe_key == dedupe_key:
                        return existing

            item = InboxItem(
                id=_new_id(),
                created_at=now_str(),
                level=level if level in LEVELS else "info",
                title=safe_title,
                body=_truncate(body, max_body_chars),
                source={
                    "type": source_type if source_type in SOURCE_TYPES else "system",
                    "agent": agent,
                    "interface": interface,
                },
                workspace_uuid=workspace_uuid,
                workspace_name=workspace_name,
                session_id=session_id,
                session_title=session_title,
                dedupe_key=dedupe_key,
                payload=dict(payload or {}),
            )
            items = _prune_items([item] + self._items)
            self._save_items(items)
            self._items = items
            self._publish("created")
            return item

    def mark_read(self, item_ids: list[str]) -> int:
        return self._set_read(item_ids, read=True)

    def mark_unread(self, item_ids: list[str]) -> int:
        return self._set_read(item_ids, read=False)

    def _set_read(self, item_ids: list[str], *, read: bool) -> int:
        targets = set(item_ids)
        if not targets:
            return 0
        with self._lock:
            self._ensure_loaded()
            stamp = now_str()
            changed = 0
            items: list[InboxItem] = []
            for item in self._items:
                if item.id in targets and not item.deleted_at:
                    if read and not item.read_at:
                        items.append(replace(item, read_at=stamp))
                        changed += 1
                        continue
                    if not read and item.read_at:
                        items.append(replace(item, read_at=""))
                        changed += 1
                        continue
                items.append(item)
            if changed:
                self._commit(items, "read" if read else "unread")
            return changed

    def mark_all_read(
        self,
        *,
        workspace_uuid: str = "",
        allowed_workspace_uuids: frozenset[str] | None = None,
    ) -> int:
        with self._lock:
            self._ensure_loaded()
            stamp = now_str()
            changed = 0
            items: list[InboxItem] = []
            for item in self._items:
                if (
                    item.is_unread
                    and _matches_scope(item, workspace_uuid, allowed_workspace_uuids)
                ):
                    items.append(replace(item, read_at=stamp))
                    changed += 1
                else:
                    items.append(item)
            if changed:
                self._commit(items, "read_all")
            return changed

    def delete(self, item_ids: list[str]) -> int:
        targets = set(item_ids)
        if not targets:
            return 0
        with self._lock:
            self._ensure_loaded()
            stamp = now_str()
            changed = 0
            items: list[InboxItem] = []
            for item in self._items:
                if item.id in targets and not item.deleted_at:
                    items.append(replace(item, deleted_at=stamp))
                    changed += 1
                else:
                    items.append(item)
            if changed:
                self._commit(items, "deleted")
            return changed

    def restore(self, item_ids: list[str]) -> int:
        targets = set(item_ids)
        if not targets:
            return 0
        with self._lock:
            self._ensure_loaded()
            changed = 0
            items: list[InboxItem] = []
            for item in self._items:
                if item.id in targets and item.deleted_at:
                    # 设计状态机：已删除 ──恢复──▶ 已读；删除时未读的条目
                    # 恢复后不应重新计入未读。
                    items.append(replace(item, deleted_at="", read_at=item.read_at or now_str()))
                    changed += 1
                else:
                    items.append(item)
            if changed:
                self._commit(items, "restored")
            return changed

    def delete_permanent(self, item_ids: list[str]) -> int:
        """物理删除指定条目（回收站里的「彻底删除」）。"""
        targets = set(item_ids)
        if not targets:
            return 0
        with self._lock:
            self._ensure_loaded()
            items = [item for item in self._items if item.id not in targets]
            removed = len(self._items) - len(items)
            if removed:
                self._commit(items, "purged")
            return removed

    def purge_deleted(self, *, now: datetime | None = None) -> int:
        with self._lock:
            self._ensure_loaded()
            current = now or datetime.now()
            cutoff = current - timedelta(days=DELETED_RETENTION_DAYS)
            items: list[InboxItem] = []
            for item in self._items:
                if item.deleted_at:
                    deleted_at = parse_ts(item.deleted_at)
                    if deleted_at is not None and deleted_at < cutoff:
                        continue
                items.append(item)
            removed = len(self._items) - len(items)
            if removed:
                self._commit(items, "purged")
            return removed

    # ── 查询 ──────────────────────────────────────────────

    def get(self, item_id: str) -> InboxItem | None:
        with self._lock:
            self._ensure_loaded()
            for item in self._items:
                if item.id == item_id:
                    return item
            return None

    def list(
        self,
        *,
        status: str = "active",
        workspace_uuid: str = "",
        allowed_workspace_uuids: frozenset[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[InboxItem], int]:
        if status not in STATUSES:
            status = "active"
        with self._lock:
            self._ensure_loaded()
            if status in ("deleted", "all"):
                # 打开回收站/全量视图时顺带清理过期项；active 读路径不写。
                self.purge_deleted()
            filtered = [
                item
                for item in self._items
                if _matches_status(item, status)
                and _matches_scope(item, workspace_uuid, allowed_workspace_uuids)
            ]
            total = len(filtered)
            start = max(0, int(offset or 0))
            size = max(0, int(limit or 0))
            return filtered[start:start + size] if size else filtered[start:], total

    def unread_count(
        self,
        *,
        workspace_uuid: str = "",
        allowed_workspace_uuids: frozenset[str] | None = None,
    ) -> int:
        with self._lock:
            self._ensure_loaded()
            return sum(
                1
                for item in self._items
                if item.is_unread
                and _matches_scope(item, workspace_uuid, allowed_workspace_uuids)
            )

    def count_recent(
        self,
        session_id: str,
        *,
        seconds: int = 60,
        source_type: str = "tool",
    ) -> int:
        """统计同一会话最近 N 秒内新增的工具通知数（限流用）。"""
        if not session_id:
            return 0
        threshold = datetime.now() - timedelta(seconds=max(0, seconds))
        with self._lock:
            self._ensure_loaded()
            count = 0
            for item in self._items:
                if item.session_id != session_id:
                    continue
                if source_type and item.source.get("type") != source_type:
                    continue
                created = parse_ts(item.created_at)
                if created is not None and created >= threshold:
                    count += 1
            return count


def _matches_status(item: InboxItem, status: str) -> bool:
    if status == "all":
        return True
    if status == "deleted":
        return item.is_deleted
    if item.is_deleted:
        return False
    if status == "unread":
        return item.is_unread
    if status == "read":
        return bool(item.read_at)
    return True  # active


def _matches_scope(
    item: InboxItem,
    workspace_uuid: str,
    allowed_workspace_uuids: frozenset[str] | None,
) -> bool:
    if workspace_uuid and item.workspace_uuid != workspace_uuid:
        return False
    if allowed_workspace_uuids is not None:
        if item.workspace_uuid:
            return item.workspace_uuid in allowed_workspace_uuids
        return "" in allowed_workspace_uuids
    return True


def _prune_items(items: list[InboxItem], now: datetime | None = None) -> list[InboxItem]:
    """清理过期已删除条目；超过上限时按「已读 → 已删除 → 未读」淘汰最旧。"""
    current = now or datetime.now()
    cutoff = current - timedelta(days=DELETED_RETENTION_DAYS)
    kept: list[InboxItem] = []
    for item in items:
        if item.deleted_at:
            deleted_at = parse_ts(item.deleted_at)
            if deleted_at is not None and deleted_at < cutoff:
                continue
        kept.append(item)

    if len(kept) <= MAX_ITEMS:
        return sorted(kept, key=_sort_key, reverse=True)

    for predicate, label in (
        (lambda i: bool(i.read_at) and not i.deleted_at, "已读"),
        (lambda i: bool(i.deleted_at), "已删除"),
        (lambda i: True, "未读"),
    ):
        excess = len(kept) - MAX_ITEMS
        if excess <= 0:
            break
        candidates = sorted(
            (item for item in kept if predicate(item)),
            key=_sort_key,
        )
        if not candidates:
            continue
        drop = candidates[:excess]
        drop_ids = {item.id for item in drop}
        logger.warning(
            "[inbox] 超过上限 %s，清理 %s 条%s通知", MAX_ITEMS, len(drop), label
        )
        kept = [item for item in kept if item.id not in drop_ids]

    return sorted(kept, key=_sort_key, reverse=True)


# ── 单例 ──────────────────────────────────────────────────

_store: InboxStore | None = None
_store_lock = threading.Lock()


def inbox_file_path() -> Path:
    """收件箱文件路径；每次调用读取当前 DATA_DIR，便于测试重定向。"""
    from core.config import DATA_DIR

    return Path(DATA_DIR) / "inbox" / "inbox.json"


def get_inbox_store() -> InboxStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = InboxStore(inbox_file_path())
        return _store


def reset_inbox_store() -> None:
    """清空单例（测试切换临时 DATA_DIR 时使用）。"""
    global _store
    with _store_lock:
        _store = None
