"""TextbookOriginalTool — 读取清洗版教材原文（分页）。"""

from __future__ import annotations

from core.textbooks import pack_store
from core.tools.base import Tool, ToolResult


class TextbookOriginalTool(Tool):
    name = "textbook_original"
    concurrency_safe = True
    description = (
        "Read the cleaned original text of a textbook (before refinement). Use it "
        "to verify refined content, quote exact textbook wording, or recover "
        "content missing from the refined version. The original has no reliable "
        "chapter boundaries — read it in pages with offset/limit. This requires a "
        "local originals.zip (not shipped in the distributed pack)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "book": {"type": "string",
                     "description": "教材 id（8 位前缀即可）或书名关键词"},
            "offset": {"type": "integer", "description": "起始字符偏移，默认 0"},
            "limit": {"type": "integer", "description": "本次返回字符数，默认 8000，最大 50000"},
        },
        "required": ["book"],
    }

    def execute(self, book: str = "", offset: int = 0,
                limit: int = 8000, **_kwargs) -> ToolResult:
        try:
            store = pack_store.PackStore()
            found, candidates = store.resolve_book(book)
            if found is None:
                if candidates:
                    return ToolResult(pack_store.format_book_candidates(candidates))
                return ToolResult(f"找不到教材「{book}」。可先用 textbook_find 检索。",
                                  error=True)
            text = store.original(found["cid"], offset=offset, limit=limit)
            return ToolResult(f"【{found['title']}】({found['cid'][:8]})\n\n{text}")
        except pack_store.PackUnavailable as e:
            return ToolResult(str(e), error=True)
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"读取原文失败: {e}", error=True)
