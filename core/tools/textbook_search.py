"""TextbookSearchTool — 跨教材全文检索（FTS trigram）。"""

from __future__ import annotations

from core.textbooks import pack_store
from core.tools.base import Tool, ToolResult


class TextbookSearchTool(Tool):
    name = "textbook_search"
    concurrency_safe = True
    description = (
        "Full-text search across all refined textbook chapters (about 8,800 "
        "chapters), for knowledge-point lookup across books and editions, e.g. "
        "'which textbooks explain 勾股定理'. Supports metadata filters to narrow "
        "the scope. Returns ranked hits with book metadata, chapter number/title "
        "and an excerpt around the match. Queries should be at least 3 characters "
        "(2-character queries fall back to a slower scan)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词，建议 ≥3 个汉字"},
            "stage": {"type": "string", "description": "限定学段（可选）"},
            "subject": {"type": "string", "description": "限定学科（可选）"},
            "grade": {"type": "string", "description": "限定年级（可选）"},
            "edition": {"type": "string", "description": "限定版本（可选）"},
            "book": {"type": "string", "description": "限定教材（id 前缀或书名关键词，可选）"},
            "limit": {"type": "integer", "description": "返回条数，默认 20，最大 50"},
        },
        "required": ["query"],
    }

    def execute(self, query: str = "", stage: str = "", subject: str = "",
                grade: str = "", edition: str = "", book: str = "",
                limit: int = 20, **_kwargs) -> ToolResult:
        try:
            limit = max(1, min(int(limit or 20), 50))
            hits = pack_store.PackStore().search(
                query, stage=stage, subject=subject, grade=grade,
                edition=edition, book=book, limit=limit)
            if not hits:
                return ToolResult(
                    f"没有包含「{query}」的章节。可换更短的关键词或减少筛选条件。")
            lines = [f"「{query}」命中 {len(hits)} 条：", ""]
            for h in hits:
                meta = "·".join(x for x in (h.get("stage"), h.get("subject"),
                                            h.get("edition"), h.get("grade"),
                                            h.get("volume")) if x)
                lines.append(
                    f"- {h['title']} ({meta}) 第{h['idx']}章 {h.get('ch_title', '')}")
                lines.append(f"  {h.get('snip', '')}")
                lines.append(f"  → textbook_read(book=\"{h['cid'][:8]}\", chapter={h['idx']})")
            return ToolResult("\n".join(lines))
        except pack_store.PackUnavailable as e:
            return ToolResult(str(e), error=True)
        except ValueError as e:
            return ToolResult(str(e), error=True)
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"检索失败: {e}", error=True)
