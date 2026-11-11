"""编辑器上下文格式化（接口无关）。

用户发送消息时，若前端提供当前打开文件 / 选区，则把其渲染成一段文本块，随
用户消息一起进入 LLM 上下文；**正文不预附**，由模型按需 ``read``。

块级 ``_meta.editor_context`` 仅供前端渲染时隐藏（模型仍可见文本）。
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

MAX_SELECTION_CHARS = 4000


def format_editor_context(ctx: dict | None) -> dict | None:
    """把 editor_context 渲染成 text content block；无有效内容返回 None。

    ctx 形态：``{"path": "src/a.py", "selection": {"startLine", "endLine", "text"} | null}``
    """
    if not isinstance(ctx, dict):
        return None
    path = str(ctx.get("path") or "").strip()
    if not path:
        return None

    lines: list[str] = [
        "[Editor context]",
        f"The user has file `{path}` open in the preview.",
    ]
    selection = ctx.get("selection")
    if isinstance(selection, dict):
        text = selection.get("text")
        if not isinstance(text, str):
            text = ""
        try:
            start = int(selection.get("startLine"))
            end = int(selection.get("endLine"))
        except (TypeError, ValueError):
            start = end = 0
        if text and start > 0 and end >= start:
            if len(text) > MAX_SELECTION_CHARS:
                text = text[:MAX_SELECTION_CHARS] + "\n...(selection truncated)"
            lines.append(f"The user selected lines {start}-{end} from `{path}`:")
            lines.append("```text")
            lines.append(text)
            lines.append("```")
    lines.append("[/Editor context]")

    return {
        "type": "text",
        "text": "\n".join(lines),
        "_meta": {"editor_context": True},
    }


_MENTION_RE = re.compile(r'@(?:"([^"]+)"|([^\s#]+))(?:#L?(\d+)(?:-(?:L)?(\d+))?)?')

MAX_MENTION_LINES = 2000
MAX_MENTION_LINE_CHARS = 2000
MAX_MENTIONS = 8
MAX_MENTION_TOTAL_CHARS = 50_000
MAX_MENTION_SNIFF_BYTES = 8192


def expand_file_mentions(
    content: str,
    workspace_dir: Path | str | None,
    *,
    max_mentions: int = MAX_MENTIONS,
) -> list[dict]:
    """把用户文本里的 ``@path#Lx-y`` 提及展开为带行号的上下文块。

    只展开**带行范围**的提及；路径必须位于工作区内且为文本文件。无范围或
    解析失败时静默跳过（原文里的 ``@...`` 仍保留）。
    """
    if not content or workspace_dir is None:
        return []
    root = Path(workspace_dir).resolve()
    blocks: list[dict] = []
    budget = MAX_MENTION_TOTAL_CHARS

    for match in _MENTION_RE.finditer(content):
        if len(blocks) >= max_mentions or budget <= 0:
            break
        raw = match.group(1) or match.group(2) or ""
        if not raw.strip():
            continue
        start_s, end_s = match.group(3), match.group(4)
        if not start_s:
            continue  # 仅展开带行范围的提及
        try:
            start = int(start_s)
            end = int(end_s) if end_s else start
        except ValueError:
            continue
        if start <= 0:
            continue
        if end < start:
            start, end = end, start
        end = min(end, start + MAX_MENTION_LINES - 1)

        candidate = Path(raw)
        if candidate.is_absolute():
            continue
        target = (root / candidate).resolve()
        try:
            if not target.is_relative_to(root):
                continue
        except ValueError:
            continue
        if not target.is_file():
            continue

        try:
            with open(target, "rb") as bf:
                head = bf.read(MAX_MENTION_SNIFF_BYTES)
        except OSError:
            continue
        if b"\x00" in head:
            continue

        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                sliced = list(itertools.islice(f, start - 1, end))
        except OSError:
            continue
        if not sliced:
            continue

        numbered: list[str] = []
        for i, line in enumerate(sliced, start=start):
            line = line.rstrip("\r\n")
            if len(line) > MAX_MENTION_LINE_CHARS:
                line = line[:MAX_MENTION_LINE_CHARS] + "...(truncated)"
            numbered.append(f"{i:>6}\t{line}")
        body = "\n".join(numbered)
        if len(body) > budget:
            body = body[:budget] + "\n...(truncated)"
        budget -= len(body)

        rel = target.relative_to(root).as_posix()
        blocks.append({
            "type": "text",
            "text": f"[File mention] `{rel}` lines {start}-{end}:\n```text\n{body}\n```",
            "_meta": {"file_mention": True},
        })
    return blocks
