"""Memory tool (v3) - delegate to MemoryStore/Journal.

Actions: store / find / read / update / delete / list / stat.
Data model: entries/{type}/{name}.md + MEMORY.md index + journal.jsonl/.cursor.
定位键是 name（slug，全局唯一），不再按 title 全目录搜索。
"""

from __future__ import annotations

from typing import Any

from core.config import get_workspace_data_dir
from core.memory_journal import Journal
from core.workspace_layout import memory_session_state_dir
from core.time_utils import now_str
from core.memory_store import (
    MEMORY_TYPES,
    SOURCES,
    MemoryStore,
)
from core.tools.base import UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END, Tool, ToolResult


class MemoryTool(Tool):
    name = "memory"
    prompt_guidelines = (
        "Search memory first (find → read) when the task may depend on prior facts.",
        "Store only durable facts, preferences, techniques, or references; memory older than 30 days may be stale — verify before relying on it.",
        "When the user asks you to remember something, call memory(action='store') instead of only acknowledging.",
    )
    description = (
        "**When the user asks you to remember something, call action='store' — do not only acknowledge.**\n"
        "Long-term memory tool for storing, searching and managing the workspace's "
        "cross-session memory. Four memory types: fact (project facts/decisions), "
        "preference (user preferences/communication style), skill (reusable techniques), "
        "reference (external source leads). Each entry lives at entries/{type}/{name}.md "
        "with a globally-unique kebab-case 'name' (slug) as its locator. "
        "Use 'find' to list matching entries by frontmatter keyword, then 'read' to fetch "
        "the full body of an entry (increments its usage count)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["store", "find", "read", "update", "delete", "list", "stat",
                         "consolidate", "archive", "restore"],
                "description": (
                    "Operation type. store: create/replace an entry; find: list matching "
                    "entries by keyword; read: read an entry's full body; update: replace "
                    "content/metadata; delete: remove an entry; list: enumerate entries; "
                    "stat: memory statistics; consolidate: turn pending journal records "
                    "into entries for the current workspace only; archive/restore: move "
                    "an entry to/from archive."
                ),
            },
            "memory_type": {
                "type": "string",
                "enum": list(MEMORY_TYPES),
                "description": (
                    "Memory type: fact, preference, skill, reference. Required for store; "
                    "optional filter for find/list."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Globally-unique kebab-case slug locator (e.g., 'rest-api-design'). "
                    "Required for read/update/delete; optional for store (derived from title "
                    "if omitted). Must be a meaningful ASCII slug, not a UUID."
                ),
            },
            "query": {
                "type": "string",
                "description": "Keyword for find (case-insensitive substring match on name/title/description/tags/refs and body). Required for find."
            },
            "status": {
                "type": "string",
                "enum": ["active", "stale", "archived"],
                "description": "Status filter for find/list."
            },
            "limit": {
                "type": "integer",
                "description": "Max items for find/list (default 20/100)."
            },
            "offset": {
                "type": "integer",
                "description": "Pagination offset for find/list."
            },
            "title": {
                "type": "string",
                "description": "Human-readable title (required for store unless name is provided)."
            },
            "description": {
                "type": "string",
                "description": "One-sentence description (<=200 chars). The primary retrieval signal used by find and the index."
            },
            "content": {
                "type": "string",
                "description": "Entry body (Markdown). Required for store; optional for update. Must be complete, not truncated."
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags for categorization and retrieval (replaces old 'topic')."
            },
            "source": {
                "type": "string",
                "enum": list(SOURCES),
                "description": "Where this memory came from: session, user, web, file, python, derived. Default user."
            },
            "refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Source references, e.g. ['file:E:/docs/x.md', 'session:abc123', 'web:https://...']."
            },
        },
        "required": ["action"],
    }

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session=None,
                 permission_rules=None):
        super().__init__(
            cwd, workspace_uuid, session,
            permission_rules=permission_rules,
        )
        self.memory_dir = str(get_workspace_data_dir(workspace_uuid) / "memory")
        self.store = MemoryStore(self.memory_dir)
        self.session_id = str(getattr(session, "session_id", "") or "")
        # 手动 store 也进当前会话的 runtime journal；无 session（测试/系统）回退旧目录。
        state_dir = (
            memory_session_state_dir(workspace_uuid, self.session_id)
            if workspace_uuid and self.session_id else self.memory_dir
        )
        self.journal = Journal(str(state_dir))

    # ─── public entry point ────────────────────────────

    def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "store")
        try:
            if action == "store":
                return self._store(kwargs)
            if action == "find":
                return self._find(kwargs)
            if action == "read":
                return self._read(kwargs)
            if action == "update":
                return self._update(kwargs)
            if action == "delete":
                return self._delete(kwargs)
            if action == "list":
                return self._list(kwargs)
            if action == "stat":
                return self._stat(kwargs)
            if action == "consolidate":
                return self._consolidate()
            if action == "archive":
                return self._archive(kwargs)
            if action == "restore":
                return self._restore(kwargs)
            return ToolResult(f"Error: unknown action '{action}'", error=True)
        except Exception as e:
            return ToolResult(f"Error: {e}", error=True)

    # ─── store ─────────────────────────────────────────

    def _store(self, kwargs: dict) -> ToolResult:
        type_ = kwargs.get("memory_type")
        if not type_:
            return ToolResult(
                "Error: memory_type is required for store (fact | preference | skill | reference)",
                error=True,
            )
        if kwargs.get("status") is not None:
            return ToolResult(
                "Error: status is not supported for store; use update to change status",
                error=True,
            )
        result = self.store.store(
            type_=type_,
            name=kwargs.get("name"),
            title=kwargs.get("title", ""),
            description=kwargs.get("description", ""),
            content=kwargs.get("content", ""),
            tags=kwargs.get("tags"),
            source=kwargs.get("source", "user"),
            refs=kwargs.get("refs"),
        )
        # 审计：手动写入也进 journal（integrated=true，整合时跳过但游标越过）
        try:
            self.journal.append(
                key=f"store:{result['name']}",
                session_key=self.session_id or self.workspace_uuid or "",
                type_guess=type_,
                title=str(kwargs.get("title") or ""),
                description=str(kwargs.get("description") or ""),
                content=f"[{type_}] {result['name']}: {kwargs.get('title', '')}",
                tags=kwargs.get("tags"),
                refs=kwargs.get("refs"),
                source=str(kwargs.get("source") or "user"),
                integrated=True,
            )
        except Exception:
            pass
        verb = "Updated" if result["replaced"] else "Stored"
        lines = [f"{verb} {result['type']} '{result['name']}' → {result['path']}"]
        return ToolResult("\n".join(lines))

    # ─── find ──────────────────────────────────────────

    def _find(self, kwargs: dict) -> ToolResult:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return ToolResult("Error: query is required for find", error=True)
        limit = int(kwargs.get("limit") or 20)
        offset = max(0, int(kwargs.get("offset") or 0))
        results = self.store.find(
            query=query,
            type_=kwargs.get("memory_type"),
            status=kwargs.get("status"),
            limit=limit,
            offset=offset,
        )
        if not results:
            return ToolResult(
                f"No memory matches for '{query}'. "
                "Use the 'store' action to create it if worth keeping."
            )
        lines = [f"Found {len(results)} match(es) for '{query}':", ""]
        for entry in results:
            lines.append(f"[{entry['type']}] {entry.get('title', entry['name'])} — {entry.get('description', '')}")
            lines.append(f"  name: {entry['name']}")
            lines.append(f"  updated: {entry.get('updated', '')} | uses: {entry.get('usage_count', 0)}")
            tags = entry.get("tags")
            if tags:
                lines.append(f"  tags: [{', '.join(tags)}]")
            note = self.store.stale_note(entry)
            if note:
                lines.append(f"  {note}")
        if len(results) >= limit:
            lines.append("")
            lines.append(f"(showing first {limit}; use limit/offset to see more)")
        lines.append("")
        lines.append('Hint: use memory(action="read", name="<name>") to read an entry\'s full body.')
        return ToolResult(UNTRUSTED_DATA_BEGIN + "\n".join(lines) + UNTRUSTED_DATA_END)

    # ─── read ──────────────────────────────────────────

    def _read(self, kwargs: dict) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult("Error: name is required for read", error=True)
        fm, body = self.store.read(name)
        from core.memory_store import serialize_frontmatter
        text = "\n".join(serialize_frontmatter(fm)) + "\n\n" + body
        note = self.store.stale_note(fm)
        if note:
            text += f"\n\n{note}"
        return ToolResult(UNTRUSTED_DATA_BEGIN + text.strip("\n") + UNTRUSTED_DATA_END)

    # ─── update ────────────────────────────────────────

    def _update(self, kwargs: dict) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult("Error: name is required for update", error=True)
        if kwargs.get("memory_type") is not None:
            return ToolResult(
                "Error: memory_type cannot be changed by update; create a new entry instead",
                error=True,
            )
        if not any(k in kwargs for k in ("title", "description", "content", "tags", "refs", "source", "status")):
            return ToolResult("Error: no fields to update", error=True)
        result = self.store.update(
            name,
            title=kwargs.get("title"),
            description=kwargs.get("description"),
            content=kwargs.get("content"),
            tags=kwargs.get("tags"),
            refs=kwargs.get("refs"),
            source=kwargs.get("source"),
            status=kwargs.get("status"),
        )
        lines = [f"Updated '{name}' → {result['path']}"]
        return ToolResult("\n".join(lines))

    # ─── delete ────────────────────────────────────────

    def _delete(self, kwargs: dict) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult("Error: name is required for delete", error=True)
        result = self.store.delete(name)
        self._audit("delete", name, result.get("type", ""), {})
        return ToolResult(f"Deleted {result['type']} '{name}'")

    def _audit(self, action: str, name: str, type_: str, kwargs: dict) -> None:
        """update/delete 也写会话 journal，保持写操作可审计。"""
        try:
            self.journal.append(
                key=f"{action}:{name}:{now_str()}",
                session_key=self.session_id or self.workspace_uuid or "",
                type_guess=type_,
                title=str(kwargs.get("title") or ""),
                description=str(kwargs.get("description") or ""),
                content=f"[{action}] {name}",
                tags=kwargs.get("tags"),
                refs=kwargs.get("refs"),
                source="user",
                integrated=True,
            )
        except Exception:
            pass

    # ─── list ──────────────────────────────────────────

    def _list(self, kwargs: dict) -> ToolResult:
        all_results = self.store.list(type_=kwargs.get("memory_type"), status=kwargs.get("status"))
        if not all_results:
            return ToolResult("No memory entries.")
        limit = int(kwargs.get("limit") or 100)
        offset = max(0, int(kwargs.get("offset") or 0))
        results = all_results[offset:offset + max(0, limit)]
        lines = [f"{len(all_results)} memory entr{'y' if len(all_results) == 1 else 'ies'}:"]
        for entry in results:
            status = entry.get("status", "active")
            marker = f"[{entry['type']}/{status}]"
            lines.append(f"{marker} {entry.get('title', entry['name'])} — {entry.get('description', '')}")
            lines.append(f"  name: {entry['name']} | updated: {entry.get('updated', '')} | uses: {entry.get('usage_count', 0)}")
        if len(results) < len(all_results):
            lines.append(f"(showing {len(results)}; use limit/offset for more)")
        return ToolResult(UNTRUSTED_DATA_BEGIN + "\n".join(lines) + UNTRUSTED_DATA_END)

    def _archive(self, kwargs: dict) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult("Error: name is required for archive", error=True)
        self.store.archive(name)
        self._audit("archive", name, "", {})
        return ToolResult(f"Archived '{name}'")

    def _restore(self, kwargs: dict) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult("Error: name is required for restore", error=True)
        self.store.restore(name)
        self._audit("restore", name, "", {})
        return ToolResult(f"Restored '{name}'")

    # ─── stat ──────────────────────────────────────────

    def _stat(self, _kwargs: dict) -> ToolResult:
        stats = self.store.stat()
        pending = self.journal.pending_count()
        if self.workspace_uuid:
            from core.memory_pipeline import _journal_dirs
            pending = sum(Journal(str(d)).pending_count() for d in _journal_dirs(self.workspace_uuid))
        by_type = "  ".join(f"{t}: {stats['by_type'][t]}" for t in MEMORY_TYPES)
        lines = [
            "Memory statistics:",
            f"- entries: {stats['total']} active/stale ({by_type})",
            f"- older than 30d: {stats['stale']} | status=stale: {stats.get('status_stale', 0)} | archived: {stats['archived']}",
            f"- index: {stats['index_lines']} lines / {stats['index_bytes']} bytes",
            f"- index path: {self.store.index_path}",
            f"- journal: {pending} pending, cursor at {self.journal.cursor()}",
        ]
        return ToolResult("\n".join(lines))

    # ─── consolidate（cron / 手动触发整合）────────────────

    def _consolidate(self) -> ToolResult:
        """只整合当前工作区；跨工作区整合由 cron/显式服务触发。"""
        from core.memory_pipeline import run_consolidation

        if not self.workspace_uuid:
            return ToolResult("No workspace context for consolidate.", error=True)
        r = run_consolidation(self.workspace_uuid)
        if not r:
            return ToolResult("No workspace memory to consolidate.")
        if r.get("error"):
            return ToolResult(f"Consolidation failed: {r['error']}")
        applied = r.get("applied", [])
        stores = sum(1 for a in applied if a["op"] == "store")
        updates = sum(1 for a in applied if a["op"] == "update")
        others = sum(1 for a in applied if a["op"] not in ("store", "update"))
        return ToolResult(
            f"Consolidated workspace {self.workspace_uuid}: "
            f"processed {r.get('processed', 0)} record(s), "
            f"{stores} stored, {updates} updated, {others} other, "
            f"{r.get('archived', 0)} archived, pending {r.get('pending_after', 0)}"
        )
