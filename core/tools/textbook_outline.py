"""TextbookOutlineTool — 教材章节目录 / 章内篇目列表。"""

from __future__ import annotations

import re

from core.textbooks import pack_store
from core.tools.base import Tool, ToolResult

_INDEX_LINE = re.compile(r"^- \[chapters/(\d+)\.md\].*? (.*)（(\d+) 字）")


class TextbookOutlineTool(Tool):
    name = "textbook_outline"
    concurrency_safe = True
    description = (
        "Get a textbook's table of contents with per-chapter character counts, "
        "so you can decide which chapter to read next with textbook_read. "
        "Locate the book by its 8-char id or by a keyword in the title; "
        "ambiguous title matches return a candidate list.\n"
        "With 'chapter', returns the lesson/section titles inside that chapter "
        "(activity headings like 思考/练习 are filtered by default). "
        "With chapter='all', returns the filtered section list for every chapter "
        "in one call — prefer this over many single-chapter calls."
    )
    parameters = {
        "type": "object",
        "properties": {
            "book": {"type": "string",
                     "description": "教材 id（8 位前缀即可）或书名关键词"},
            "chapter": {"type": "string",
                        "description": "章节序号（如 1）、章节标题关键词，或 'all'"
                                       "（一次返回全部章的小节列表）"},
            "compact": {"type": "boolean",
                        "description": "章内标题是否过滤活动类小标题（思考/练习/习题等），"
                                       "默认 true；false 返回全部 markdown 标题"},
        },
        "required": ["book"],
    }

    def _resolve(self, book: str) -> tuple[dict | None, ToolResult | None]:
        store = pack_store.PackStore()
        found, candidates = store.resolve_book(book)
        if found is not None:
            return found, None
        if candidates:
            return None, ToolResult(
                pack_store.format_book_candidates(candidates, "请用更精确的关键词或 id：")
            )
        return None, ToolResult(f"找不到教材「{book}」。可先用 textbook_find 检索。",
                                error=True)

    def execute(self, book: str = "", chapter: str = "", compact: bool = True,
                **_kwargs) -> ToolResult:
        try:
            store = pack_store.PackStore()
            found, error = self._resolve(book)
            if error:
                return error
            if chapter and chapter.lower() in ("all", "全部", "每章"):
                outline = store.outline(found["cid"])
                lines = [f"【{found['title']}】全部章节目录（小节已过滤活动标题）", ""]
                for line in outline.splitlines():
                    m = _INDEX_LINE.match(line)
                    if not m:
                        continue
                    idx, title, _chars = m.group(1), m.group(2), m.group(3)
                    try:
                        ch_title, headings = store.chapter_headings(
                            found["cid"], idx, compact=compact)
                    except Exception as e:  # noqa: BLE001 —— 单章失败不阻断
                        lines.append(f"## 第{int(idx)}章 {title}（读取失败: {e}）")
                        continue
                    lines.append(f"## 第{int(idx)}章 {ch_title}")
                    body = [h for h in headings if h != ch_title]
                    lines += [f"- {h}" for h in body] or ["- （本章无可列举的小节标题）"]
                    lines.append("")
                return ToolResult("\n".join(lines))
            if chapter:
                title, headings = store.chapter_headings(found["cid"], chapter,
                                                         compact=compact)
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
                f"或 textbook_outline(book=..., chapter=all) 一次查看所有章节的小节")
        except pack_store.PackUnavailable as e:
            return ToolResult(str(e), error=True)
        except ValueError as e:
            return ToolResult(str(e), error=True)
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"获取目录失败: {e}", error=True)
