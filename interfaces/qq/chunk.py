"""QQ 侧的长文本切分入口（实现已提取到 ``core/text_chunk.py``）。

保留本模块是为了让 QQ 的调用点与测试的 import 路径不变。QQ 与微信共用同一份
切分逻辑，QQ 用默认上限 4000——``qqbot-agent-sdk`` 的 ``build_text_body`` 会按
``MAX_MESSAGE_LENGTH = 4000`` **静默截断**，切大了会丢内容（详见 core 模块）。
"""

from core.text_chunk import MAX_CHUNK_CHARS, chunk_markdown

__all__ = ["MAX_CHUNK_CHARS", "chunk_markdown"]
