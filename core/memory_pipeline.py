"""Memory ingestion pipeline (v3 §4.2 / §4.3).

Extraction (post-turn, background thread) appends structured or [RAW] records to
the journal; consolidation (cron every 2h or manual) turns pending journal
records into memory entries, refreshes summary.md, advances .cursor and
git-commits the real diff.

Exactly-once is guaranteed by two layers:
- append side: Journal dedups by key (`extract:{session}:{first_msg_id}:{i}`)
- consume side: monotonic .cursor (advanced only after a successful run)

Both stages call the LLM with NO tools (no bash/python/web) — a stronger sandbox
than the design's read-only agent, cheaper, and injectable for tests. The
extractor/consolidator callbacks may be replaced with full agent implementations
later without changing the storage contract.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Callable

from core.config import AGENTS_DIR, get_workspace_data_dir, load_config, load_workspace_config
from core.llm import Message, create_llm_client
from core.memory_store import (
    MEMORY_TYPES,
    Journal,
    MemoryStore,
    best_effort_commit,
    now_str,
)

logger = logging.getLogger(__name__)


# ─── JSON schemas（结构化输出）──────────────────────────────────────

_MEMORY_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(MEMORY_TYPES)},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "content": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["type", "title", "description", "content"],
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {"type": "array", "items": _MEMORY_ITEM_SCHEMA},
    },
    "required": ["memories"],
}

CONSOLIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["store", "update", "archive", "delete", "skip"]},
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": list(MEMORY_TYPES)},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "refs": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["op", "name", "reason"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["ops", "summary"],
}


# ─── 系统提示词（英文，发给 LLM）────────────────────────────────────

_WHAT_NOT_TO_SAVE = (
    "Do NOT save anything that:\n"
    "- can be derived from the code itself (structure, paths, obvious patterns) — reading the code is authoritative;\n"
    "- is git history or a changelog (`git log` / `git blame` are authoritative);\n"
    "- is a debugging fix recipe that now lives in the code or a commit;\n"
    "- is a temporary in-flight task detail.\n"
    "When the user says 'remember this PR list' or similar, do not copy it verbatim; "
    "instead note what was surprising or non-obvious about it."
)

EXTRACTION_SYSTEM_PROMPT = (
    "You extract durable, cross-session memory candidates from conversation turns. "
    "Return a JSON object: {\"memories\": [ {type, title, description, content, tags, refs} ]}.\n"
    "Four types, mutually exclusive — pick exactly one per memory:\n"
    "- fact: a durable project fact, decision, or configuration.\n"
    "- preference: a user preference, working style, or communication preference.\n"
    "- skill: a reusable technique or workflow.\n"
    "- reference: a lead to an external source (doc, URL, article).\n"
    "Rules:\n"
    "- Extract only NEW durable information present in these turns. If nothing worth keeping, return {\"memories\": []}.\n"
    "- title: short human-readable phrase. description: one sentence (<=200 chars) — the primary retrieval signal.\n"
    "- content: complete Markdown body, not truncated.\n"
    "- refs: source references such as file:/web:/session:.\n"
    f"{_WHAT_NOT_TO_SAVE}\n"
    "Do not invent or extrapolate beyond what the turns actually say."
)

CONSOLIDATION_SYSTEM_PROMPT = (
    "You consolidate pending memory records into the workspace memory store. "
    "Return a JSON object: {\"ops\": [ {op, name, type, title, description, content, tags, refs, reason} ], \"summary\": \"...\"}.\n"
    "For each pending record (labelled [cursor N]) decide exactly one op against the existing entries:\n"
    "- store: genuinely new → provide full title/description/content/tags/refs (respect the record's type_guess when a type is missing).\n"
    "- update: duplicates or refines an existing entry (match by name) → merge, accumulate refs, provide the new content.\n"
    "- archive: outdated or low-value but worth keeping for history.\n"
    "- delete: wrong, superseded, or trivially derivable.\n"
    "- skip: keep pending for later (e.g. needs more context).\n"
    "Rules:\n"
    "- 'name' must be an existing entry name for update/archive/delete; for store it should be empty or a new kebab-case slug.\n"
    "- When a record contradicts an existing entry, the new fact wins: use update to replace the old content in place (git keeps history).\n"
    f"{_WHAT_NOT_TO_SAVE}\n"
    "- summary: a refreshed global summary of the workspace memory, <= 2KB, written in the language of the memory content."
)


# ─── 秘密脱敏（§4.2 / §8.2）────────────────────────────────────────

# (pattern, keep_label)：keep_label=True 时保留 group(1) 标签（如 api_key=），
# 仅掩蔽取值；False 时整段为密钥，直接掩蔽。
_SECRET_PATTERNS = [
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|secret|passwd|password)\s*[=:]\s*[\"']?)[A-Za-z0-9_\-\.]{8,}[\"']?"), True),
    (re.compile(r"(?i)((?:sk-(?:ant-)?|ghp_|gho_|xox[baprs]-|AKIA|AIza)[A-Za-z0-9_\-]{12,})"), False),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_\.]{12,}"), True),
]


def redact_secrets(text: str) -> str:
    """入库前掩蔽疑似密钥（API key / token / password）。"""
    if not text:
        return text
    for pattern, keep_label in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda m: (m.group(1) + "***REDACTED***") if keep_label else "***REDACTED***",
            text,
        )
    return text


# ─── 会话级提取游标（.extract/{session_id}.json）────────────────────

class _ExtractPointer:
    """每次提取后记录最后处理的消息 id；崩溃后重读 messages 仍能接续。"""

    def __init__(self, memory_dir: str, session_id: str):
        self.path = os.path.join(memory_dir, ".extract", f"{session_id}.json")

    def load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "last_msg_id": str(data.get("last_msg_id", "")),
                "last_extract_ts": str(data.get("last_extract_ts", "")),
            }
        except (OSError, ValueError):
            return {"last_msg_id": "", "last_extract_ts": ""}

    def save(self, last_msg_id: str, ts: str) -> None:
        dirname = os.path.dirname(self.path)
        os.makedirs(dirname, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"last_msg_id": last_msg_id, "last_extract_ts": ts}, f, ensure_ascii=False)
        os.replace(tmp, self.path)


_pointer_locks: dict[str, threading.Lock] = {}
_pointer_locks_guard = threading.Lock()


def _pointer_lock(memory_dir: str, session_id: str) -> threading.Lock:
    key = f"{memory_dir}|{session_id}"
    with _pointer_locks_guard:
        lock = _pointer_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _pointer_locks[key] = lock
        return lock


# ─── 消息格式化 ─────────────────────────────────────────────────────

def _msg_id(msg: dict) -> str:
    meta = msg.get("_meta") or {}
    return str(meta.get("id") or msg.get("id") or "")


def _message_text(msg: dict, limit: int = 4000) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        text = str(content)
    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + f"\n…(truncated, {len(text)} chars total)"
    return text


def _new_messages(messages: list[dict], last_id: str) -> list[dict]:
    """返回 last_id 之后的新增消息（含 last_id 之后全部，含无 id 消息）。"""
    if not messages:
        return []
    if not last_id:
        return list(messages)
    seen_last = False
    new_msgs = []
    for msg in messages:
        msg_id = _msg_id(msg)
        if seen_last:
            new_msgs.append(msg)
            continue
        if msg_id and msg_id == last_id:
            seen_last = True
    return new_msgs


def _read_index_text(memory_dir: str) -> str:
    index_path = os.path.join(memory_dir, "MEMORY.md")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# ─── 提取（§4.2）───────────────────────────────────────────────────

def _build_extraction_input(messages: list[dict], index_text: str) -> list[Message]:
    parts = [
        "Below are new conversation turns from a workspace session, followed by the "
        "workspace's current memory index. Extract durable memory candidates from the turns."
    ]
    if index_text:
        parts.append("\n## Current memory index\n" + index_text)
    parts.append("\n## New conversation turns")
    shown = 0
    for msg in messages:
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(msg)
        if not text:
            continue
        parts.append(f"\n### {role}\n{text}")
        shown += 1
    if not shown:
        return []
    return [Message(role="user", content="\n".join(parts))]


def run_extraction(
    workspace_uuid: str,
    session_id: str,
    messages: list[dict],
    extractor: Callable | None = None,
) -> dict:
    """从会话新增消息中提取记忆候选，写入 journal。

    恰好一次：会话级指针（last_msg_id）只挑选新增消息；journal 按 key 去重，
    重试不会产生重复记录。提取失败退化为 [RAW] 原文（绝不丢内容）。
    """
    if not session_id:
        session_id = "session"
    memory_dir = str(get_workspace_data_dir(workspace_uuid) / "memory")
    journal = Journal(memory_dir)
    pointer = _ExtractPointer(memory_dir, session_id)
    state = pointer.load()

    with _pointer_lock(memory_dir, session_id):
        new_msgs = _new_messages(messages, state.get("last_msg_id", ""))
        if not new_msgs:
            return {"appended": 0, "raw": 0, "extracted": 0, "skipped": True}

        key_base = _msg_id(new_msgs[0]) or _msg_id(new_msgs[-1]) or f"b{len(new_msgs)}"
        last_id = _msg_id(new_msgs[-1]) or ""

        prompt = _build_extraction_input(new_msgs, _read_index_text(memory_dir))
        if not prompt:
            pointer.save(last_id or key_base, now_str())
            return {"appended": 0, "raw": 0, "extracted": 0, "skipped": True}

        raw_records: list[dict] = []
        degraded = False
        extractor_fn = extractor or default_extractor
        try:
            result = extractor_fn(prompt, EXTRACTION_SYSTEM_PROMPT, EXTRACTION_SCHEMA)
            raw_records = (result or {}).get("memories") or []
            if not isinstance(raw_records, list):
                raw_records = []
        except Exception as e:
            logger.warning("memory extraction failed for session %s: %s", session_id, e)
            degraded = True

        if degraded:
            raw_text = redact_secrets("\n".join(_message_text(m) for m in new_msgs))
            journal.append(
                key=f"raw:{session_id}:{key_base}",
                session_key=session_id,
                content=raw_text,
                source="session",
                integrated=False,
                raw=True,
            )
            pointer.save(last_id or key_base, now_str())
            return {"appended": 1, "raw": 1, "extracted": 0}

        appended = 0
        for i, m in enumerate(raw_records):
            if not isinstance(m, dict):
                continue
            type_guess = str(m.get("type", "")).strip()
            if type_guess not in MEMORY_TYPES:
                type_guess = ""
            content = redact_secrets(str(m.get("content", "")).strip())
            if not content:
                continue
            title = redact_secrets(str(m.get("title", "")).strip())
            description = redact_secrets(str(m.get("description", "")).strip())
            tags = [redact_secrets(str(t).strip()) for t in (m.get("tags") or []) if str(t).strip()]
            refs = [str(r).strip() for r in (m.get("refs") or []) if str(r).strip()]
            journal.append(
                key=f"extract:{session_id}:{key_base}:{i}",
                session_key=session_id,
                type_guess=type_guess,
                title=title[:200],
                description=description[:300],
                content=content,
                tags=tags,
                refs=refs,
                source="session",
                integrated=False,
                raw=False,
            )
            appended += 1

        pointer.save(last_id or key_base, now_str())
        return {"appended": appended, "raw": 0, "extracted": len(raw_records)}


def schedule_extraction(workspace_uuid: str, session_id: str, messages: list[dict]) -> threading.Thread:
    """在后台守护线程中触发提取（web 回合结束后钩子）。不阻塞 SSE 流。"""
    def _run() -> None:
        try:
            run_extraction(workspace_uuid, session_id, messages)
        except Exception:
            logger.exception("memory extraction failed for session %s", session_id)

    thread = threading.Thread(target=_run, name="memory-extract", daemon=True)
    thread.start()
    return thread


# ─── 整合（§4.3）───────────────────────────────────────────────────

_RECORD_MAX_CHARS = 1000
_MAX_CONSOLIDATION_SPLIT = 3  # 整合分批最大递归深度：整批失败/截断时拆半重试


def _entries_context(store: MemoryStore, pending: list[dict], cap: int = 10) -> str:
    """按待整合记录的关键词匹配已有条目，作为冲突消解上下文（只读 frontmatter，不增 usage）。"""
    seen: dict[str, dict] = {}
    for rec in pending:
        haystack = f"{rec.get('title') or ''} {(rec.get('content') or '')[:200]}"
        keywords = [w for w in re.split(r"[^A-Za-z0-9一-鿿]+", haystack) if len(w) >= 2][:4]
        for kw in keywords:
            if not kw:
                continue
            for entry in store.find(query=kw, limit=3):
                if entry["name"] not in seen:
                    seen[entry["name"]] = entry
                if len(seen) >= cap:
                    break
            if len(seen) >= cap:
                break
        if len(seen) >= cap:
            break
    if not seen:
        return ""
    lines: list[str] = []
    for entry in seen.values():
        lines.append(f"- [{entry.get('type', '')}] {entry.get('title', entry['name'])}")
        lines.append(
            f"  name: {entry['name']} | updated: {entry.get('updated', '')} "
            f"| uses: {entry.get('usage_count', 0)} | status: {entry.get('status', 'active')}"
        )
        if entry.get("description"):
            lines.append(f"  description: {entry['description']}")
        if entry.get("tags"):
            lines.append(f"  tags: [{', '.join(entry['tags'])}]")
    return "\n".join(lines)


def _build_consolidation_input(pending: list[dict], index_text: str, entries_snapshot: str) -> list[Message]:
    parts = [
        "Below are the pending memory records from the workspace journal, together with the "
        "current memory index and matched existing entries. Decide an op for each record and "
        "produce a refreshed global summary."
    ]
    if index_text:
        parts.append("\n## Current memory index\n" + index_text)
    if entries_snapshot:
        parts.append("\n## Matched existing entries\n" + entries_snapshot)
    parts.append(f"\n## Pending records ({len(pending)})")
    for rec in pending:
        content = str(rec.get("content") or "")
        if len(content) > _RECORD_MAX_CHARS:
            content = content[:_RECORD_MAX_CHARS] + "\n…(truncated)"
        header = (
            f"[cursor {rec.get('cursor')}] type_guess={rec.get('type_guess') or ''} "
            f"source={rec.get('source') or ''} raw={bool(rec.get('raw'))} "
            f"session={rec.get('session_key') or ''}"
        )
        parts.append("\n" + header)
        if rec.get("title"):
            parts.append(f"title: {rec['title']}")
        if rec.get("tags"):
            parts.append(f"tags: [{', '.join(rec['tags'])}]")
        parts.append(content)
    return [Message(role="user", content="\n".join(parts))]


def apply_ops(store: MemoryStore, ops: list[dict]) -> dict:
    """确定性地应用整合 op，返回 {"applied": [...], "failed": [...]}。

    任何单条失败不阻断其余：失败项进 failed，由调用方决定是否推进 journal
    游标（失败不消费记录，避免"已整合但未写入"的静默丢失）。description 截断
    到 store 的 200 字上限（journal 截断 300，直接透传会误报）。
    """
    applied: list[dict] = []
    failed: list[dict] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        action = op.get("op")
        name = str(op.get("name") or "").strip()
        reason = op.get("reason", "")
        try:
            if action == "store":
                title = str(op.get("title") or "")
                content = str(op.get("content") or "")
                result = store.store(
                    type_=op.get("type") if op.get("type") in MEMORY_TYPES else "fact",
                    name=name or None,
                    title=title,
                    description=str(op.get("description") or "")[:200],
                    content=content,
                    tags=op.get("tags"),
                    source="derived",
                    refs=op.get("refs"),
                )
                applied.append({"op": "store", "name": result["name"], "reason": reason})
            elif action == "update":
                if not name:
                    continue
                store.update(
                    name,
                    title=op.get("title"),
                    description=str(op.get("description") or "")[:200],
                    content=op.get("content"),
                    tags=op.get("tags"),
                    refs=op.get("refs"),
                    source="derived",
                )
                applied.append({"op": "update", "name": name, "reason": reason})
            elif action == "archive":
                if not name:
                    continue
                store.archive(name)
                applied.append({"op": "archive", "name": name, "reason": reason})
            elif action == "delete":
                if not name:
                    continue
                store.delete(name)
                applied.append({"op": "delete", "name": name, "reason": reason})
            elif action == "skip":
                applied.append({"op": "skip", "name": name, "reason": reason})
        except Exception as e:
            logger.warning("apply_ops %s(%s) failed: %s", action, name, e)
            failed.append({"op": action, "name": name, "reason": reason, "error": str(e)})
    return {"applied": applied, "failed": failed}


_SUMMARY_MAX_BYTES = 2 * 1024


def _write_summary(memory_dir: str, summary: str) -> None:
    """写全局摘要 summary.md（≤2KB，超限按整行截断）。"""
    os.makedirs(memory_dir, exist_ok=True)
    text = summary.strip()
    if len(text.encode("utf-8")) > _SUMMARY_MAX_BYTES:
        kept: list[str] = []
        size = 0
        for line in text.splitlines():
            line_size = len((line + "\n").encode("utf-8"))
            if size + line_size > _SUMMARY_MAX_BYTES:
                break
            kept.append(line)
            size += line_size
        text = "\n".join(kept)
    path = os.path.join(memory_dir, "summary.md")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _consolidate_pending(
    store: MemoryStore,
    records: list[dict],
    consolidator: Callable,
    depth: int = 0,
) -> tuple[list[dict], str]:
    """整合一批记录，返回 (ops, summary)。

    整批失败（异常）或输出明显不完整（op 数 < 记录数，常见于 max_tokens 截断
    丢尾部）时，拆半递归重试——小批次输出更短，截断概率骤降。深度受
    _MAX_CONSOLIDATION_SPLIT 限制；仍不完整则原样返回已解析的 ops，由调用方按
    计数决定是否推进游标（不推则记录保持 pending，绝不丢内容）。
    """
    prompt = _build_consolidation_input(
        records, _read_index_text(str(store.memory_dir)), _entries_context(store, records)
    )
    try:
        result = consolidator(prompt, CONSOLIDATION_SYSTEM_PROMPT, CONSOLIDATION_SCHEMA)
    except Exception:
        if depth >= _MAX_CONSOLIDATION_SPLIT or len(records) <= 1:
            raise
        return _split_consolidation(store, records, consolidator, depth)

    ops = (result or {}).get("ops") or []
    if not isinstance(ops, list):
        ops = []
    summary = str((result or {}).get("summary") or "").strip()
    if len(ops) >= len(records) or depth >= _MAX_CONSOLIDATION_SPLIT or len(records) <= 1:
        return ops, summary
    return _split_consolidation(store, records, consolidator, depth)


def _split_consolidation(
    store: MemoryStore,
    records: list[dict],
    consolidator: Callable,
    depth: int,
) -> tuple[list[dict], str]:
    """把记录拆成两半分别整合，合并 ops；summary 取后半（更新鲜）。"""
    mid = max(1, len(records) // 2)
    left_ops, left_summary = _consolidate_pending(store, records[:mid], consolidator, depth + 1)
    right_ops, right_summary = _consolidate_pending(store, records[mid:], consolidator, depth + 1)
    return left_ops + right_ops, right_summary or left_summary


def run_consolidation(
    workspace_uuid: str,
    consolidator: Callable | None = None,
    limit: int = 20,
) -> dict:
    """整合一个工作区 journal 中的待处理记录。

    仅当整合正常完成（op 覆盖全部待处理记录且无应用失败）才推进 .cursor
    （崩溃可安全重跑）；git 提交信息来自真实 diff。op 数不足即视为截断/模型
    少输出，不推进游标，记录保持 pending 供重跑。
    """
    memory_dir = str(get_workspace_data_dir(workspace_uuid) / "memory")
    store = MemoryStore(memory_dir)
    journal = Journal(memory_dir)
    pending = journal.read_pending(limit=limit)
    if not pending:
        return {"processed": 0, "ops": [], "applied": [], "archived": [], "committed": False,
                "pending_after": 0, "summary_len": 0}

    consolidator_fn = consolidator or default_consolidator
    try:
        ops, summary = _consolidate_pending(store, pending, consolidator_fn)
    except Exception as e:
        logger.warning("memory consolidation failed for workspace %s: %s", workspace_uuid, e)
        return {"processed": 0, "ops": [], "applied": [], "archived": [], "committed": False,
                "pending_after": journal.pending_count(), "error": str(e)}

    if not isinstance(ops, list):
        ops = []
    result = apply_ops(store, ops)
    applied = result["applied"]
    failed = result["failed"]

    incomplete = len(ops) < len(pending)
    if summary and not (failed or incomplete):
        _write_summary(memory_dir, summary)

    if failed or incomplete:
        # 有 op 应用失败，或 op 未覆盖全部待整合记录（截断/模型少输出）：
        # 不推游标，记录保持 pending 供下次整合重跑。store 幂等（同名原地替换），
        # 重放已成功的 op 无副作用。
        reason = (f"{len(failed)} op(s) failed to apply" if failed
                  else f"consolidator returned {len(ops)} ops for {len(pending)} pending records")
        return {
            "processed": len(pending),
            "ops": ops,
            "applied": applied,
            "failed": failed,
            "archived": [],
            "committed": False,
            "pending_after": journal.pending_count(),
            "summary_len": len(summary.encode("utf-8")) if summary else 0,
            "error": f"{reason}; journal cursor not advanced",
        }

    max_cursor = max((int(r.get("cursor", 0)) for r in pending), default=0)
    journal.advance(max_cursor)
    journal.compact(keep=500)
    archived = store.archive_stale()

    ok, note = best_effort_commit(memory_dir, f"consolidate: {len(applied)} ops, {len(archived)} archived")
    return {
        "processed": len(pending),
        "ops": ops,
        "applied": applied,
        "failed": failed,
        "archived": archived,
        "committed": ok,
        "commit_note": note if ok else "",
        "pending_after": journal.pending_count(),
        "summary_len": len(summary.encode("utf-8")) if summary else 0,
    }


def _iter_workspace_uuids() -> list[str]:
    """扫描 data/agents/ 下带 memory/ 的工作区 uuid（排除 system）。"""
    uuids: list[str] = []
    try:
        if AGENTS_DIR.is_dir():
            for child in sorted(AGENTS_DIR.iterdir()):
                if child.is_dir() and child.name != "system" and (child / "memory").is_dir():
                    uuids.append(child.name)
    except OSError:
        pass
    return uuids


def memory_enabled(workspace_uuid: str) -> bool:
    """记忆功能开关：data/agents/{uuid}/setting.json 的 memory_enabled 字段。

    缺省为 False——纯工作区隔离，除非用户在记忆管理页显式开启。
    """
    cfg = load_workspace_config(workspace_uuid)
    return bool(cfg.get("memory_enabled", False))


def consolidate_all(consolidator: Callable | None = None, limit: int = 20) -> list[dict]:
    """整合所有开启记忆的工作区（cron / 手动触发）。返回每工作区结果。"""
    results: list[dict] = []
    for uuid in _iter_workspace_uuids():
        if not memory_enabled(uuid):
            continue
        try:
            results.append({"workspace_uuid": uuid, **run_consolidation(uuid, consolidator=consolidator, limit=limit)})
        except Exception as e:
            logger.warning("consolidate workspace %s failed: %s", uuid, e)
            results.append({"workspace_uuid": uuid, "error": str(e)})
    return results


# ─── 默认 LLM 调用（lite model 控成本；测试注入回调替换）───────────

_DEFAULT_EXTRACT_MAX_TOKENS = 8000
_DEFAULT_CONSOLIDATE_MAX_TOKENS = 16000


def _lite_client():
    config = load_config()
    model = config.lite_model or config.model
    return create_llm_client(model)


def _output_limit(client, requested: int) -> int:
    """max_tokens 取请求值与模型配置上限的较小者，避免小模型被超上限拒绝。"""
    return min(requested, client.config.max_tokens)


def default_extractor(messages: list[Message], system: str, schema: dict,
                      max_tokens: int = _DEFAULT_EXTRACT_MAX_TOKENS) -> dict:
    client = _lite_client()
    try:
        return client.chat_structured(messages, system=system, output_schema=schema,
                                      max_tokens=_output_limit(client, max_tokens))
    finally:
        client.close()


def default_consolidator(messages: list[Message], system: str, schema: dict,
                         max_tokens: int = _DEFAULT_CONSOLIDATE_MAX_TOKENS) -> dict:
    client = _lite_client()
    try:
        return client.chat_structured(messages, system=system, output_schema=schema,
                                      max_tokens=_output_limit(client, max_tokens))
    finally:
        client.close()
