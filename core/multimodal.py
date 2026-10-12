"""把入站媒体组装成 ``runner.run(user_input=...)`` 的入参（接口无关）。

原先内联在 ``interfaces/qq/inbound.py``；微信接入端要用同一套语义，故提到 core。

规则（与 Web 的图片上传路径一致）：
- 图片 → multimodal block；模型不支持多模态时退化为给路径（cili 有 ``read_image``
  工具，模型可以自己去读）
- 其余文件 / 语音转写 → 把路径与转写文本拼进正文

返回值可能是 ``str``（纯文本）或 ``list[dict]``（含图片块）——两者都是
``runner.run`` 接受的入参形态。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any

from core.media_fetch import InboundMedia

logger = logging.getLogger(__name__)


def build_user_input(
    content: str,
    media_items: list[InboundMedia],
    runner,
) -> Any:
    """构造 ``runner.run(user_input=...)`` 的入参。"""
    if not media_items:
        return content

    text_parts = [content] if content else []
    blocks: list[dict] = []
    if content:
        blocks.append({"type": "text", "text": content, "_valid": True})

    multimodal = bool(getattr(getattr(runner, "model", None), "multimodal", False))
    for item in media_items:
        if item.kind == "image" and multimodal:
            data, mime = read_image_base64(item)
            if data:
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                    "_valid": True,
                })
                continue
        if item.kind == "voice" and item.transcript:
            text_parts.append(f"[语音转写] {item.transcript}")
        else:
            text_parts.append(f"[附件] {item.rel_path}")

    body = "\n".join(p for p in text_parts if p).strip()
    if len(blocks) <= 1:
        # 没有可用的图片块 → 退回纯文本（含附件路径）
        return body
    if body:
        blocks[0] = {"type": "text", "text": body, "_valid": True}
    return blocks


def read_image_base64(item: InboundMedia) -> tuple[str, str]:
    """读图片为 base64（失败返回空串）。"""
    try:
        data = Path(item.path).read_bytes()
    except OSError as e:
        logger.warning(f"[multimodal] 读取图片失败 {item.path}: {e}")
        return "", ""
    mime = item.content_type or mimetypes.guess_type(item.filename)[0] or "image/png"
    return base64.b64encode(data).decode("ascii"), mime
