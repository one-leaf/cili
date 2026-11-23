"""记忆摄入日志 Journal：journal.jsonl + .cursor（恰好一次消费）。"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from core.fs_utils import atomic_write_text
from core.time_utils import now_str

logger = logging.getLogger(__name__)

_STORE_LOCKS: dict[str, threading.Lock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _dir_lock(memory_dir: str) -> threading.Lock:
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(memory_dir)
        if lock is None:
            lock = threading.Lock()
            _STORE_LOCKS[memory_dir] = lock
        return lock


class Journal:
    """journal.jsonl（append-only）+ .cursor（已整合游标）——恰好一次消费。

    追加端去重：同 key（消息 id）只写一次；消费端去重：只读 cursor 之后的
    未整合记录，处理成功后单调推进 cursor。崩溃不推 cursor 即可重跑。
    """

    def __init__(self, memory_dir: str | Path):
        self.memory_dir = Path(memory_dir).resolve()
        self.journal_path = self.memory_dir / "journal.jsonl"
        self.cursor_path = self.memory_dir / ".cursor"
        self._lock = _dir_lock(str(self.memory_dir))
        # append 去重用的 key → cursor 索引（惰性构建；compact 重写文件后失效）。
        # 没有它时每次 append 都要全量扫描 journal，记录变多后退化为线性。
        self._key_index: dict[str, int] | None = None
        self._last_cursor: int = 0

    # ── 游标 ──

    def cursor(self) -> int:
        try:
            return int(self.cursor_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return 0

    def _write_cursor(self, value: int) -> None:
        atomic_write_text(self.cursor_path, str(value))

    def advance(self, to_cursor: int) -> int:
        """单调推进游标（不后退）。返回新游标。"""
        with self._lock:
            current = self.cursor()
            target = max(current, int(to_cursor))
            if target != current:
                self._write_cursor(target)
            return target

    # ── 追加 ──

    def append(self, *, key: str, session_key: str = "", type_guess: str = "",
               title: str = "", description: str = "", content: str = "",
               tags: list[str] | None = None, refs: list[str] | None = None,
               source: str = "session", integrated: bool = False,
               raw: bool = False, ts: str | None = None) -> int:
        """追加一条摄入记录，返回其 cursor id。同 key 已存在则返回既有 cursor（去重）。"""
        if not key:
            raise ValueError("key is required for journal append")
        with self._lock:
            if self._key_index is None:
                records = self._read_records()
                self._key_index = {
                    str(r["key"]): int(r.get("cursor", 0)) for r in records if r.get("key")
                }
                self._last_cursor = int(records[-1].get("cursor", 0)) if records else 0
            existing = self._key_index.get(key)
            if existing is not None:
                return existing
            cursor = self._last_cursor + 1
            self._last_cursor = cursor
            record = {
                "cursor": cursor,
                "key": key,
                "ts": ts or now_str(),
                "session_key": session_key,
                "type_guess": type_guess,
                "title": title,
                "description": description,
                "content": content,
                "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
                "refs": [str(r).strip() for r in (refs or []) if str(r).strip()],
                "source": source,
                "integrated": integrated,
                "raw": raw,
            }
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                # 掉电安全（审查 P1-8）：journal 是恰好一次语义的载体，追加后强制落盘
                f.flush()
                os.fsync(f.fileno())
            self._key_index[key] = cursor
            return cursor

    # ── 读取 / 消费 ──

    def _read_records(self) -> list[dict]:
        if not self.journal_path.is_file():
            return []
        records: list[dict] = []
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(f"[memory] 损坏 journal 行（跳过单行）: {self.journal_path}")
                        continue
        except OSError:
            return []
        return records

    def read_pending(self, limit: int = 20) -> list[dict]:
        """游标之后、未整合（integrated!=true）的记录，按 cursor 升序。"""
        with self._lock:
            cur = self.cursor()
            records = self._read_records()
        return [r for r in records if int(r.get("cursor", 0)) > cur and not r.get("integrated")][:limit]

    def pending_count(self) -> int:
        with self._lock:  # 与 read_pending 一致（审查 P1-7：cursor 与读取需同锁）
            cur = self.cursor()
            records = self._read_records()
        return sum(1 for r in records
                   if int(r.get("cursor", 0)) > cur and not r.get("integrated"))

    def compact(self, keep: int = 500) -> int:
        """删除已整合且超预算的旧记录，journal 保底不膨胀（仿 nanobot compact_history）。"""
        with self._lock:
            cur = self.cursor()
            records = self._read_records()
            pending = [r for r in records if int(r.get("cursor", 0)) > cur]
            processed = [r for r in records if int(r.get("cursor", 0)) <= cur]
            keep_records = sorted(pending + processed[-keep:], key=lambda r: int(r.get("cursor", 0)))
            if len(keep_records) == len(records):
                return 0
            atomic_write_text(
                self.journal_path,
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep_records),
            )
            # 文件已重写，缓存的 key 索引失效
            self._key_index = None
            return len(records) - len(keep_records)
