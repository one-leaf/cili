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

from core.image_pipeline import ensure_user_image_meta, normalize_image_bytes
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

    model_input = list(getattr(getattr(runner, "model", None), "input", []) or [])
    for item in media_items:
        if item.kind == "image" and "image" in model_input:
            data, mime = read_image_base64(item)
            if data:
                block = {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                    "_valid": True,
                    "_meta": {"path": item.rel_path, "media_type": mime},
                }
                ensure_user_image_meta(block, cwd=getattr(runner, "cwd", None))
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


def read_image_base64(item: InboundMedia) -> tuple[str, str]:
    """读图片为 base64；优先应用 EXIF 方向、缩放和重编码。"""
    try:
        raw = Path(item.path).read_bytes()
    except OSError as e:
        logger.warning(f"[multimodal] 读取图片失败 {item.path}: {e}")
        return "", ""
    mime = item.content_type or mimetypes.guess_type(item.filename)[0] or "image/png"
    normalized = normalize_image_bytes(raw, mime)
    if normalized is not None:
        payload, mime, _, _ = normalized
        return base64.b64encode(payload).decode("ascii"), mime
    return base64.b64encode(raw).decode("ascii"), mime
