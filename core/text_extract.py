"""从消息 content（字符串或 block 列表）提取纯文本的唯一实现。"""

from __future__ import annotations

from typing import Any


def message_text(
    content: Any,
    *,
    first: bool = False,
    image_placeholder: str | None = None,
    strip: bool = True,
) -> str:
    """拼接文本块（可含裸字符串块）。

    - ``first``：只取第一个非空文本块（会话预览等场景）
    - ``image_placeholder``：图片块替换为该占位文本（时间线等展示场景）
    - ``strip``：是否去除首尾空白（调用方需要保留原格式时传 False）
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                value = str(block.get("text") or "")
                if value:
                    parts.append(value)
                    if first:
                        break
            elif btype == "image" and image_placeholder:
                parts.append(image_placeholder)
        text = "\n".join(parts)
    else:
        text = str(content or "")
    return text.strip() if strip else text
