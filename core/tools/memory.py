"""Memory tool (v3) - delegate to MemoryStore/Journal.

Actions: store / find / read / update / delete / list / stat.
Data model: entries/{type}/{name}.md + MEMORY.md index + journal.jsonl/.cursor.
定位键是 name（slug，全局唯一），不再按 title 全目录搜索。
"""

from __future__ import annotations

from typing import Any

from core.config import get_workspace_data_dir
from core.memory_store import (
    MEMORY_TYPES,
    SOURCES,
    Journal,
    MemoryStore,
    best_effort_commit,
    git_log_summary,
    slugify,
)
from core.tools.base import Tool, ToolResult, UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END

_STALE_NOTE = "⚠ 此为历史观察（>30 天未更新），使用前请对照当前代码/事实验证"


class MemoryTool(Tool):
    name = "memory"
    description = (
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
                "enum": ["store", "find", "read", "update", "delete", "list", "stat", "consolidate"],
                "description": (
                    "Operation type. store: create/replace an entry; find: list matching "
                    "entries by keyword; read: read an entry's full body; update: replace "
                    "content/metadata; delete: remove an entry; list: enumerate entries; "
                    "stat: memory statistics; consolidate: turn pending journal records "
                    "into entries across all workspaces (cron/manual trigger)."
                ),
            },
            "type": {
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
                "description": "Keyword for find (case-insensitive match on name/title/description/tags/refs). Required for find."
            },
            "status": {
                "type": "string",
                "enum": ["active", "stale", "archived"],
                "description": "Status filter for find/list."
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

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session_manager=None):
        super().__init__(cwd, workspace_uuid, session_manager)
        self.memory_dir = str(get_workspace_data_dir(workspace_uuid) / "memory")
        self.store = MemoryStore(self.memory_dir)
        self.journal = Journal(self.memory_dir)

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
            return ToolResult(f"Error: unknown action '{action}'", error=True)
        except Exception as e:
            return ToolResult(f"Error: {e}", error=True)

    # ─── store ─────────────────────────────────────────

    def _store(self, kwargs: dict) -> ToolResult:
        type_ = kwargs.get("type")
        if not type_:
            return ToolResult(
                "Error: type is required for store (fact | preference | skill | reference)",
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
                session_key=self.workspace_uuid or "",
                type_guess=type_,
                content=f"[{type_}] {result['name']}: {kwargs.get('title', '')}",
                refs=kwargs.get("refs"),
                source="user",
                integrated=True,
            )
        except Exception:
            pass
        verb = "Updated" if result["replaced"] else "Stored"
        lines = [f"{verb} {result['type']} '{result['name']}' → {result['path']}"]
        ok, commit_note = best_effort_commit(self.memory_dir, f"{verb.lower()} {result['type']}:{result['name']}")
        if ok:
            lines.append(f"git: {commit_note}")
        return ToolResult("\n".join(lines))

    # ─── find ──────────────────────────────────────────

    def _find(self, kwargs: dict) -> ToolResult:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return ToolResult("Error: query is required for find", error=True)
        results = self.store.find(
            query=query,
            type_=kwargs.get("type"),
            status=kwargs.get("status"),
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
        lines.append("")
        lines.append('Hint: use memory(action="read", name="<name>") to read an entry\'s full body.')
        return ToolResult(UNTRUSTED_DATA_BEGIN + "\n".join(lines) + UNTRUSTED_DATA_END)

    # ─── read ──────────────────────────────────────────

    def _read(self, kwargs: dict) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult("Error: name is required for read", error=True)
        fm, body = self.store.read(name)
        from core.memory_store import _serialize_frontmatter
        text = "\n".join(_serialize_frontmatter(fm)) + "\n\n" + body
        note = self.store.stale_note(fm)
        if note:
            text += f"\n\n{note}"
        return ToolResult(UNTRUSTED_DATA_BEGIN + text.strip("\n") + UNTRUSTED_DATA_END)

    # ─── update ────────────────────────────────────────

    def _update(self, kwargs: dict) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult("Error: name is required for update", error=True)
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
        ok, commit_note = best_effort_commit(self.memory_dir, f"update:{name}")
        if ok:
            lines.append(f"git: {commit_note}")
        return ToolResult("\n".join(lines))

    # ─── delete ────────────────────────────────────────

    def _delete(self, kwargs: dict) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult("Error: name is required for delete", error=True)
        result = self.store.delete(name)
        best_effort_commit(self.memory_dir, f"delete:{name}")
        return ToolResult(f"Deleted {result['type']} '{name}' (git history can restore it)")

    # ─── list ──────────────────────────────────────────

    def _list(self, kwargs: dict) -> ToolResult:
        results = self.store.list(type_=kwargs.get("type"), status=kwargs.get("status"))
        if not results:
            return ToolResult("No memory entries.")
        lines = [f"{len(results)} memory entr{'y' if len(results) == 1 else 'ies'}:"]
        for entry in results:
            status = entry.get("status", "active")
            marker = f"[{entry['type']}/{status}]"
            lines.append(f"{marker} {entry.get('title', entry['name'])} — {entry.get('description', '')}")
            lines.append(f"  name: {entry['name']} | updated: {entry.get('updated', '')} | uses: {entry.get('usage_count', 0)}")
        return ToolResult("\n".join(lines))

    # ─── stat ──────────────────────────────────────────

    def _stat(self, _kwargs: dict) -> ToolResult:
        stats = self.store.stat()
        pending = self.journal.pending_count()
        by_type = "  ".join(f"{t}: {stats['by_type'][t]}" for t in MEMORY_TYPES)
        lines = [
            "Memory statistics:",
            f"- entries: {stats['total']} active/stale ({by_type})",
            f"- stale (>30d): {stats['stale']} | archived: {stats['archived']}",
            f"- index: {stats['index_lines']} lines / {stats['index_bytes']} bytes",
            f"- journal: {pending} pending, cursor at {self.journal.cursor()}",
        ]
        commits = git_log_summary(self.memory_dir, max_commits=5)
        if commits:
            lines.append("- recent commits:")
            for c in commits:
                lines.append(f"  {c['hash']} {c['date']} {c['subject']}")
        return ToolResult("\n".join(lines))

    # ─── consolidate（cron / 手动触发整合）────────────────

    def _consolidate(self) -> ToolResult:
        from core.memory_pipeline import consolidate_all

        results = consolidate_all()
        if not results:
            return ToolResult("No workspace memory to consolidate.")
        lines = [f"Consolidated {len(results)} workspace(s):"]
        for r in results:
            if r.get("error"):
                lines.append(f"- {r['workspace_uuid']}: error {r['error']}")
                continue
            applied = r.get("applied", [])
            stores = sum(1 for a in applied if a["op"] == "store")
            updates = sum(1 for a in applied if a["op"] == "update")
            others = sum(1 for a in applied if a["op"] not in ("store", "update"))
            committed = "committed" if r.get("committed") else "no git"
            lines.append(
                f"- {r['workspace_uuid']}: processed {r.get('processed', 0)} record(s), "
                f"{stores} stored, {updates} updated, {others} other, "
                f"{r.get('archived', 0)} archived, pending {r.get('pending_after', 0)}, {committed}"
            )
        return ToolResult("\n".join(lines))
