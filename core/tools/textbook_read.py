"""TextbookReadTool — 读取教材某一章的提炼全文。"""

from __future__ import annotations

from core.textbooks import pack_store
from core.tools.base import Tool, ToolResult


class TextbookReadTool(Tool):
    name = "textbook_read"
    concurrency_safe = True
    description = (
        "Read one chapter's full refined text from a textbook. Locate the chapter "
        "by its number (from textbook_outline) or by a keyword in its title. "
        "Content is returned verbatim; very long chapters are truncated with a "
        "note (raise max_chars to read more)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "book": {"type": "string",
                     "description": "教材 id（8 位前缀即可）或书名关键词"},
            "chapter": {"type": "string",
                        "description": "章节序号（如 3）或章节标题关键词（如 勾股）"},
            "max_chars": {"type": "integer",
                          "description": "正文最大字符数，默认 20000，最大 60000"},
        },
        "required": ["book", "chapter"],
    }

    def execute(self, book: str = "", chapter: str = "",
                max_chars: int = 20000, **_kwargs) -> ToolResult:
        try:
            store = pack_store.PackStore()
            found, candidates = store.resolve_book(book)
            if found is None:
                if candidates:
                    lines = [f"书名匹配到 {len(candidates)} 本，请指定 id："]
                    lines += [pack_store.format_book_line(b) for b in candidates]
                    return ToolResult("\n".join(lines))
                return ToolResult(f"找不到教材「{book}」。可先用 textbook_find 检索。",
                                  error=True)
            max_chars = max(1000, min(int(max_chars or 20000), 60000))
            title, body = store.read_chapter(found["cid"], chapter, max_chars)
            meta = "·".join(x for x in (found.get("stage"), found.get("subject"),
                                        found.get("edition"), found.get("grade"),
                                        found.get("volume")) if x)
            return ToolResult(
                f"【{found['title']}】({meta}) 第{chapter}章 · {title}\n\n{body}")
        except pack_store.PackUnavailable as e:
            return ToolResult(str(e), error=True)
        except ValueError as e:
            return ToolResult(str(e), error=True)
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"读取章节失败: {e}", error=True)
