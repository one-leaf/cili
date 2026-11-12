"""TextbookFindTool — 教材书目检索（按学段/学科/版本/年级/册次/书名关键词）。"""

from __future__ import annotations

from core.textbooks import pack_store
from core.tools.base import Tool, ToolResult


class TextbookFindTool(Tool):
    name = "textbook_find"
    concurrency_safe = True
    description = (
        "Search the built-in Chinese K-12 textbook dataset (refined chapter texts). "
        "Filter by stage/subject/grade/edition/volume or match keywords in the book "
        "title. Returns a compact list of books with an 8-char id, metadata, chapter "
        "count and character count. Use this first to locate a book, then call "
        "textbook_outline / textbook_read. With no filters, returns the corpus "
        "overview (stage x subject counts)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "stage": {"type": "string",
                      "description": "学段：小学 / 小学（五•四学制）/ 初中 / 初中（五•四学制）/ 高中 / 特殊教育"},
            "subject": {"type": "string",
                        "description": "学科，如 数学 / 语文 / 英语 / 物理"},
            "grade": {"type": "string",
                      "description": "年级：一年级…九年级 / 高一 / 高二 / 高三"},
            "edition": {"type": "string",
                        "description": "版本，如 人教版 / 北师大版 / 统编版"},
            "volume": {"type": "string", "description": "册次：上册 / 下册 / 全一册"},
            "keyword": {"type": "string", "description": "书名关键词"},
            "limit": {"type": "integer", "description": "返回条数，默认 10，最大 50"},
        },
        "required": [],
    }

    def execute(self, stage: str = "", subject: str = "", grade: str = "",
                edition: str = "", volume: str = "", keyword: str = "",
                limit: int = 10, **_kwargs) -> ToolResult:
        try:
            limit = max(1, min(int(limit or 10), 50))
            if not any((stage, subject, grade, edition, volume, keyword)):
                lines = ["教材库概览（数据包索引）：", ""]
                for st, n, wan in pack_store.stage_matrix():
                    lines.append(f"- {st or '未标注'}：{n} 本，约 {wan} 万字")
                lines.append("")
                lines.append("用 stage/subject/grade 等条件进一步筛选。")
                return ToolResult("\n".join(lines))
            books = pack_store.find_books(
                stage=stage, subject=subject, grade=grade, edition=edition,
                volume=volume, keyword=keyword, limit=limit)
            if not books:
                return ToolResult("没有匹配的教材。可减少筛选条件或换用关键词。")
            lines = [f"匹配 {len(books)} 本（显示前 {len(books)} 条）：", ""]
            lines += [pack_store.format_book_line(b) for b in books]
            lines.append("")
            lines.append("下一步：textbook_outline(book=id 或书名关键词) 查看章节目录。")
            return ToolResult("\n".join(lines))
        except pack_store.PackUnavailable as e:
            return ToolResult(str(e), error=True)
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"教材检索失败: {e}", error=True)
