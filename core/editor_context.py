"""编辑器上下文格式化（接口无关）。

用户发送消息时，若前端提供当前打开文件 / 选区，则把其渲染成一段文本块，随
用户消息一起进入 LLM 上下文；**正文不预附**，由模型按需 ``read``。

块级 ``_meta.attachment`` 仅供前端渲染时隐藏（模型仍可见文本）。
"""

from __future__ import annotations

import itertools
import re
import secrets
from pathlib import Path

MAX_SELECTION_CHARS = 4000
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"}


def _is_image_path(path: str) -> bool:
    return Path(path).suffix.lower() in _IMAGE_EXTS


def format_attachment(att: dict | None) -> dict | None:
    """统一格式化附件条目（预览选区 / Ctrl+V 上传文件同一套逻辑）。

    att: ``{"path", "selection": {"startLine","endLine","text"} | None, "media_type"}``
    - 有选区：输出 [Editor context]（行范围 + 选中文本）
    - 无选区：输出 [Attachment] 路径提示；图片附 read_image 提示
    """
    if not isinstance(att, dict):
        return None
    path = str(att.get("path") or "").strip()
    if not path:
        return None
    media_type = str(att.get("media_type") or "")
    is_image = media_type.startswith("image/") or _is_image_path(path)

    selection = att.get("selection")
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
            lines = [
                "[Editor context]",
                f"The user selected lines {start}-{end} from `{path}`:",
                "```text",
                text,
                "```",
                "[/Editor context]",
            ]
            return {
                "type": "text",
                "id": f"att_{secrets.token_hex(4)}",
                "text": "\n".join(lines),
                "_meta": {
                    "attachment": {
                        "path": path,
                        "selection": {"startLine": start, "endLine": end},
                    }
                },
            }

    label = media_type or ("image" if is_image else "file")
    lines = [f"[Attachment] `{path}` ({label})"]
    if is_image:
        lines.append(f'Use read_image("{path}") to view this image.')
    return {
        "type": "text",
        "id": f"att_{secrets.token_hex(4)}",
        "text": "\n".join(lines),
        "_meta": {
            "attachment": {
                "path": path,
                "media_type": media_type or ("image" if is_image else "file"),
                "selection": None,
            }
        },
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
