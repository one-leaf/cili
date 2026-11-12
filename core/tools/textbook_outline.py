"""TextbookOutlineTool — 教材章节目录 / 章内篇目列表。"""

from __future__ import annotations

from core.textbooks import pack_store
from core.tools.base import Tool, ToolResult


class TextbookOutlineTool(Tool):
    name = "textbook_outline"
    concurrency_safe = True
    description = (
        "Get a textbook's table of contents with per-chapter character counts, "
        "so you can decide which chapter to read next with textbook_read. "
        "Locate the book by its 8-char id or by a keyword in the title; "
        "ambiguous title matches return a candidate list.\n"
        "If 'chapter' is given, returns the headings (lesson titles) inside that "
        "chapter instead — use this to answer questions like 'which lessons are "
        "in this unit'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "book": {"type": "string",
                     "description": "教材 id（8 位前缀即可）或书名关键词"},
            "chapter": {"type": "string",
                        "description": "章节序号（如 1）或章节标题关键词；"
                                       "提供时返回该章内部的篇目标题列表"},
        },
        "required": ["book"],
    }

    def execute(self, book: str = "", chapter: str = "", **_kwargs) -> ToolResult:
        try:
            store = pack_store.PackStore()
            found, candidates = store.resolve_book(book)
            if found is None:
                if candidates:
                    lines = [f"书名匹配到 {len(candidates)} 本，请用更精确的关键词或 id："]
                    lines += [pack_store.format_book_line(b) for b in candidates]
                    return ToolResult("\n".join(lines))
                return ToolResult(f"找不到教材「{book}」。可先用 textbook_find 检索。",
                                  error=True)
            if chapter:
                title, headings = store.chapter_headings(found["cid"], chapter)
                if not headings:
                    return ToolResult(
                        f"「{title}」内没有可识别的篇目标题"
                        f"（可能是活动单元或纯文本章节，可直接用 textbook_read 阅读全文）。")
                lines = [f"【{found['title']}】第{chapter}章 · {title}"
                         f"（共 {len(headings)} 个标题）", ""]
                lines += [f"- {h}" for h in headings]
                return ToolResult("\n".join(lines))
            outline = store.outline(found["cid"])
            return ToolResult(
                f"【{found['title']}】({found['cid'][:8]})\n\n{outline}\n\n"
                f"下一步：textbook_read(book=\"{found['cid'][:8]}\", chapter=序号或标题关键词)；"
                f"或 textbook_outline(book=..., chapter=N) 查看该章内的篇目列表")
        except pack_store.PackUnavailable as e:
            return ToolResult(str(e), error=True)
        except ValueError as e:
            return ToolResult(str(e), error=True)
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"获取目录失败: {e}", error=True)
