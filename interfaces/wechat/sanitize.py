"""微信出站文本：markdown 清洗。

**为什么需要**：微信 iLink 客户端把正文里的裸 ``<`` 当标记，可能把后面的内容整段
吞掉；``~~``、图片语法、5-6 级标题在它那里渲染也不正常。**代码区（围栏与行内代码）
必须逐字节保留**——清洗用户贴的代码是破坏性的。

算法照抄官方插件 ``sanitizeWeixinMarkdown``。

**框架脚手架剥离不在本模块**——那是所有接入端共用的
``core/text_sanitize.strip_scaffolding``，由 ``sanitize_wechat_text`` 串起来。
"""

from __future__ import annotations

import re

from core.text_sanitize import strip_scaffolding

# 围栏代码块或行内代码：split 后偶数下标是「代码之外」的部分
_CODE_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]*`)")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_DEEP_HEADING_RE = re.compile(r"(?m)^#{5,6}\s+")


def clean_markdown_for_wechat(content: str) -> str:
    """做微信特有的 markdown 清洗（代码区原样保留）。"""
    if not content:
        return content
    parts = _CODE_RE.split(content)
    for index in range(0, len(parts), 2):
        text = parts[index]
        text = _IMAGE_RE.sub("", text)
        text = text.replace("<", "＜").replace(">", "＞")
        text = text.replace("~~", "")
        text = _DEEP_HEADING_RE.sub("", text)
        parts[index] = text
    return "".join(parts)


def sanitize_wechat_text(text: str) -> str:
    """出站文本总入口：先剥框架脚手架，再做微信 markdown 清洗。

    **在整段文本上调用，不要逐块调用**——成对标签可能被切在两次流式回调之间，
    逐块清洗会漏掉（见 ``sink.py``）。
    """
    return clean_markdown_for_wechat(strip_scaffolding(text))
