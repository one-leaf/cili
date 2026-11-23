"""教材工具的公共基类：数据包可用性、书名解析与候选提示。"""

from __future__ import annotations

from core.textbooks import pack_store
from core.tools.base import Tool, ToolResult


class TextbookToolBase(Tool):
    concurrency_safe = True

    @staticmethod
    def _store() -> "pack_store.PackStore":
        return pack_store.PackStore()

    def _resolve_book(self, book: str, hint: str = "") -> tuple[dict | None, ToolResult | None]:
        """返回 (书目条目, 错误结果)；找不到时错误结果里带候选列表。"""
        found, candidates = self._store().resolve_book(book)
        if found is not None:
            return found, None
        if candidates:
            return None, ToolResult(pack_store.format_book_candidates(candidates, hint))
        return None, ToolResult(f"找不到教材「{book}」。可先用 textbook_find 检索。", error=True)
