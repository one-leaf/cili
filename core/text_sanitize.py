"""出站文本消毒：剥离框架脚手架标签与模型思考内容（接口无关）。

纯函数，无状态，可直接测。

为什么要自己做：各平台的 Python SDK **都没有文本消毒**——它们只管传输。官方 TS
插件才有 ``sanitizeQQBotText``，本模块的正则集照抄自
``openclaw-qqbot/src/outbound/sanitize.ts``。

保留 Markdown/HTML 原样——各接入端的渲染层自己决定怎么处理（微信还要额外把
``<`` ``>`` 转全角，那是渲染层的事，不在本模块）。
"""

from __future__ import annotations

import re

# 顺序有讲究：先剥「成对标签包住整块」，再剥残留的单标签
_INTERNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 框架脚手架
    re.compile(r"<system-reminder\b[^>]*>.*?</system-reminder>", re.S | re.I),
    re.compile(r"<previous_response\b[^>]*>.*?</previous_response>", re.S | re.I),
    re.compile(r"<\s*/?\s*(?:system-reminder|previous_response)\b[^>]*/?\s*>", re.I),
    # deepseek 用反引号替代尖括号作为标签定界符：`think` … `/think`
    re.compile(r"`think`.*?`/think`", re.S | re.I),
    # claude / 通用 think 标签（成对）
    re.compile(r"<think\b[^>]*>.*?</think>", re.S | re.I),
    # claude thinking 标签（成对）
    re.compile(r"<thinking\b[^>]*>.*?</thinking>", re.S | re.I),
    # 残留单个标签
    re.compile(r"<\s*/?\s*(?:think|thinking)\b[^>]*/?\s*>", re.I),
)


def strip_scaffolding(text: str) -> str:
    """剥离内部脚手架块与模型推理内容，返回去首尾空白的文本。"""
    if not text:
        return ""
    result = text
    for pattern in _INTERNAL_PATTERNS:
        result = pattern.sub("", result)
    return result.strip()
