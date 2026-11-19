"""邮件正文与附件的编解码。

- **入站**：优先 ``text/plain``；只有 HTML 时用标准库剥标签转纯文本；剥掉 ``>``
  开头的引文行；附件落盘到 ``{workspace}/uploads/``，图片转 multimodal block
  （与 Web/QQ/微信一致）。
- **出站**（附件打包）在 ``outbound.py``。

文件名一律只取 basename 再做去重（``a.pdf`` 重复时变 ``a-1.pdf``），路径分隔符与
``..`` 天然被挡掉——邮件附件的 ``filename`` 是不可信输入。
"""

from __future__ import annotations

import logging
import mimetypes
import re
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path

from core.media_fetch import InboundMedia

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".ico", ".svg"}

# 引文行：邮件客户端习惯用 ">" 前缀引用历史内容（整行剥掉）
_QUOTE_PREFIX_RE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)


class _HtmlToText(HTMLParser):
    """最小 HTML → 纯文本：去标签、保留段落换行、反转义实体。"""

    _BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [line.rstrip() for line in raw.splitlines()]
        # 连续空行压成一个
        out: list[str] = []
        blank = False
        for line in lines:
            if not line:
                if not blank and out:
                    out.append("")
                blank = True
                continue
            blank = False
            out.append(line)
        return "\n".join(out).strip()


def _part_text(part: Message) -> str:
    """取一个 MIME part 的文本内容（按声明的 charset 解码）。"""
    try:
        content = part.get_content()
        return content if isinstance(content, str) else ""
    except Exception:  # noqa: BLE001 — 编码异常时退化为宽松解码
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, "replace")


def extract_text(msg: Message) -> str:
    """提取邮件正文：优先 text/plain，回退 text/html（剥标签）。"""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if not msg.is_multipart():
        content_type = msg.get_content_type()
        if content_type == "text/html":
            html_parts.append(_part_text(msg))
        elif content_type == "text/plain" or content_type.startswith("text/"):
            plain_parts.append(_part_text(msg))
    else:
        for part in msg.walk():
            if part.is_multipart():
                continue
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain":
                plain_parts.append(_part_text(part))
            elif content_type == "text/html":
                html_parts.append(_part_text(part))

    if plain_parts:
        text = "\n".join(t for t in plain_parts if t.strip())
    else:
        converter = _HtmlToText()
        for html in html_parts:
            converter.feed(html)
        text = converter.text()

    # 剥掉引文行（回信里的历史内容不进上下文）
    text = _QUOTE_PREFIX_RE.sub("", text)
    return text.strip()


def truncate_body(text: str, limit: int) -> str:
    """超长正文截断并显式注记（不静默丢内容）。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "\n\n…（正文过长，已截断）"


def _safe_filename(name: str) -> str:
    base = Path(str(name or "")).name.replace("\x00", "").strip()
    base = base.replace("..", "_")
    return base or "attachment"


def _unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for i in range(1, 1000):
        candidate = directory / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}-{len(filename)}{suffix}"


def save_attachments(
    msg: Message, *, uploads_dir: Path, workspace_dir: Path,
) -> list[InboundMedia]:
    """把附件落盘到 ``uploads/``；图片标记为 image（走 multimodal）。"""
    uploads_dir.mkdir(parents=True, exist_ok=True)
    items: list[InboundMedia] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        path = _unique_path(uploads_dir, _safe_filename(filename))
        try:
            path.write_bytes(payload)
        except OSError as e:
            logger.warning(f"[email/media] 附件落盘失败 {filename}: {e}")
            continue
        content_type = part.get_content_type() or \
            mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        is_image = path.suffix.lower() in _IMAGE_EXTS or content_type.startswith("image/")
        try:
            rel = str(path.relative_to(workspace_dir)).replace("\\", "/")
        except ValueError:
            rel = str(path)
        items.append(InboundMedia(
            kind="image" if is_image else "file",
            path=str(path),
            rel_path=rel,
            filename=path.name,
            content_type=content_type,
        ))
    return items
