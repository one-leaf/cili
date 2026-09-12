"""SessionSearchTool — 跨会话搜索历史消息。

在当前工作区（workspace_uuid）的所有会话目录里按关键词检索 messages.jsonl，
返回命中的会话及其消息片段。只读，不修改任何会话文件。
"""

from __future__ import annotations

from datetime import datetime

from core.config import get_workspace_data_dir
from core.session import MESSAGES_FILE, read_jsonl, read_meta
from core.tools.base import Tool, ToolResult


class SessionSearchTool(Tool):
    name = "session_search"
    description = (
        "Search historical messages across all sessions in the current workspace.\n"
        "Useful to recall what was discussed or done in past sessions: what files were "
        "changed, what decisions were made, what the user previously asked for.\n"
        "Returns matching sessions (most recently updated first) with message snippets, "
        "roles, and session names."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword to search for in message text (case-insensitive substring).",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of sessions to report (default: 5).",
            },
            "per_session": {
                "type": "integer",
                "description": "Maximum matching messages to show per session (default: 3).",
            },
        },
        "required": ["query"],
    }

    MAX_MESSAGES_SCANNED = 200_000  # 防御：单次扫描消息总数上限

    def execute(self, query: str, max_results: int = 5, per_session: int = 3, **_kwargs) -> ToolResult:
        query = (query or "").strip()
        if not query:
            return ToolResult("Error: query is required", error=True)
        if len(query) > 200:
            return ToolResult("Error: query too long (max 200 chars)", error=True)
        if max_results < 1:
            max_results = 1
        if per_session < 1:
            per_session = 1

        sessions_root = get_workspace_data_dir(self.workspace_uuid) / "sessions"
        if not sessions_root.is_dir():
            return ToolResult(f"No sessions directory found at {sessions_root}")

        sessions = []
        for p in sessions_root.iterdir():
            if p.is_dir() and (p / MESSAGES_FILE).is_file():
                sessions.append((p, read_meta(p)))
        sessions.sort(key=lambda item: self._updated_at(item[1]), reverse=True)

        needle = query.lower()
        results = []
        scanned = 0
        for sdir, meta in sessions:
            if len(results) >= max_results:
                break
            matches = []
            for line in read_jsonl(sdir / MESSAGES_FILE):
                if scanned >= self.MAX_MESSAGES_SCANNED:
                    break
                scanned += 1
                text = self._extract_text(line.get("content"))
                if text and needle in text.lower():
                    matches.append({
                        "seq": line.get("seq"),
                        "role": line.get("role", "?"),
                        "text": self._clip(text),
                    })
                    if len(matches) >= per_session:
                        break
            if matches:
                results.append({
                    "session_id": meta.get("session_id") or sdir.name,
                    "name": meta.get("name") or sdir.name,
                    "updated_at": self._updated_at_str(meta),
                    "matches": matches,
                })

        if not results:
            return ToolResult(
                f"No historical messages matched '{query}' "
                f"(scanned {scanned} message(s) across {len(sessions)} session(s))."
            )

        lines = [f"Found matches for '{query}' in {len(results)} session(s):", ""]
        for r in results:
            lines.append(f"## {r['name']} (id: {r['session_id']}, updated: {r['updated_at']})")
            for m in r["matches"]:
                lines.append(f"- [{m['role']} #{m['seq']}] {m['text']}")
            lines.append("")
        lines.append(f"Scanned {scanned} message(s) across {len(sessions)} session(s).")
        return ToolResult("\n".join(lines))

    @staticmethod
    def _extract_text(content: object) -> str:
        """从消息 content（字符串或 block 列表）提取可搜索文本。"""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        parts = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    t = block.get("text")
                    if isinstance(t, str):
                        parts.append(t)
                elif btype == "tool_use":
                    name = block.get("name")
                    if name:
                        parts.append(f"[tool:{name}]")
                elif btype == "tool_result":
                    parts.append(SessionSearchTool._extract_text(block.get("content")))
        return "\n".join(parts)

    @staticmethod
    def _clip(text: str, limit: int = 300) -> str:
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        return text[:limit] + "…"

    @staticmethod
    def _updated_at(meta: dict) -> float:
        try:
            updated = (meta.get("metadata") or {}).get("updated_at", "")
            return datetime.strptime(updated, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return 0.0

    @staticmethod
    def _updated_at_str(meta: dict) -> str:
        return (meta.get("metadata") or {}).get("updated_at", "")
