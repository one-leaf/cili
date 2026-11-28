"""邮件附件落盘与图片识别（入站）。

正文提取与文件名清洗已下沉到 ``core.mail.content``（``mail`` 工具也用同一套），
本模块只保留接入端特有的行为：附件落盘到 ``{workspace}/uploads/``、图片标记为
image 走 multimodal。

文件名一律只取 basename 再做去重（``a.pdf`` 重复时变 ``a-1.pdf``），路径分隔符与
``..`` 天然被挡掉——邮件附件的 ``filename`` 是不可信输入。
"""

from __future__ import annotations

import logging
import mimetypes
from email.message import Message
from pathlib import Path

from core.mail.content import (  # noqa: F401 — extract_text/truncate_body 由本模块 re-export
    extract_text,
    truncate_body,
)
from core.mail.content import (
    safe_filename as _safe_filename,
)
from core.mail.content import (
    unique_path as _unique_path,
)
from core.media_fetch import InboundMedia

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".ico", ".svg"}

__all__ = ["extract_text", "save_attachments", "truncate_body"]


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