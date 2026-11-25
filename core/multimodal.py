"""把入站媒体组装成 ``runner.run(user_input=...)`` 的入参（接口无关）。

原先内联在 ``interfaces/qq/inbound.py``；微信接入端要用同一套语义，故提到 core。

规则（与 Web 的图片上传路径一致）：
- 位图 → multimodal block；模型不支持多模态时退化为给路径（cili 有 ``read``
  工具，模型可以自己去读）
- SVG → 不内联为图片（接口层不收 image/svg+xml），按附件路径交给模型用 ``read``
  读源码
- 其余文件 / 语音转写 → 把路径与转写文本拼进正文

返回值可能是 ``str``（纯文本）或 ``list[dict]``（含图片块）——两者都是
``runner.run`` 接受的入参形态。
"""

from __future__ import annotations

import logging
from typing import Any

from core.image_pipeline import (
    DEFAULT_MAX_BYTES,
    ensure_user_image_meta,
    read_image_base64,
)
from core.media_fetch import InboundMedia

logger = logging.getLogger(__name__)


def _is_svg(item: InboundMedia) -> bool:
    """SVG 不内联：按文本源码读取，避免把 image/svg+xml 发给只认位图的接口。"""
    name = (item.rel_path or item.filename or item.path or "").lower()
    mime = (item.content_type or "").split(";", 1)[0].strip().lower()
    return name.endswith(".svg") or mime == "image/svg+xml"


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

    images_cfg = getattr(getattr(runner, "config", None), "images", None)
    max_dimension = int(getattr(images_cfg, "max_dimension", 4096) or 4096)
    request_target = int(
        getattr(images_cfg, "request_image_max_bytes", DEFAULT_MAX_BYTES)
        or DEFAULT_MAX_BYTES
    )
    raw_target = max(1, min(request_target, DEFAULT_MAX_BYTES))
    jpeg_quality = int(getattr(images_cfg, "jpeg_quality", 85) or 85)

    model_input = list(getattr(getattr(runner, "model", None), "input", []) or [])
    for item in media_items:
        if item.kind == "image" and "image" in model_input and not _is_svg(item):
            data, mime, normalized = read_image_base64(
                item,
                max_dimension=max_dimension,
                max_bytes=raw_target,
                jpeg_quality=jpeg_quality,
            )
            if data:
                block = {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                    "_valid": True,
                    "_meta": {"path": item.rel_path, "media_type": mime, "normalized": normalized},
                }
                ensure_user_image_meta(
                    block, cwd=getattr(runner, "cwd", None),
                    max_dimension=max_dimension, max_bytes=raw_target,
                    jpeg_quality=jpeg_quality,
                )
                blocks.append(block)
                continue
        if item.kind == "voice" and item.transcript:
            text_parts.append(f"[语音转写] {item.transcript}")
        else:
            text_parts.append(f"[附件] {item.rel_path}")

    body = "\n".join(p for p in text_parts if p).strip()
    has_image = any(b.get("type") == "image" for b in blocks)
    if not has_image:
        # 没有可用的图片块 → 退回纯文本（含附件路径）
        return body
    text_block = next((b for b in blocks if b.get("type") == "text"), None)
    if body:
        if text_block is not None:
            text_block["text"] = body
        else:
            # 纯图片/语音混合消息没有文本块：插到首位，不能覆盖已有图片块
            blocks.insert(0, {"type": "text", "text": body, "_valid": True})
    return blocks
