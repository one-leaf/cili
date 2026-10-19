"""QQ 接入端的能力档案。

事实来源（不重述，改协议常量后提示词自动跟随）：
- ``max_message_chars`` ← ``core.text_chunk.MAX_CHUNK_CHARS``（SDK 按 4000 静默
  截断，见 core/text_chunk.py 的 docstring）
- ``inline_image_exts`` ← ``interfaces.qq.media._IMAGE_EXTS``（只有 png/jpg 保证
  内嵌成图片，其余降级为文件卡片）
- ``file_size_limit_mb`` ← ``interfaces.qq.media.HARD_LIMIT_BYTES``（官方硬限制
  200MB）

注册时机：``interfaces/qq/__init__.py`` import 本模块。
"""

from core.interface_profile import InterfaceProfile, register_profile
from core.text_chunk import MAX_CHUNK_CHARS
from interfaces.qq.media import _IMAGE_EXTS, HARD_LIMIT_BYTES

PROFILE = InterfaceProfile(
    name="qq",
    label="QQ private chat",
    renders=("markdown",),
    not_rendered=("inline images", "mermaid", "SVG", "LaTeX", "raw HTML"),
    max_message_chars=MAX_CHUNK_CHARS,
    inline_image_exts=tuple(sorted(_IMAGE_EXTS)),
    streaming=True,
    can_send_file=True,
    file_delivery="upload",
    file_size_limit_mb=HARD_LIMIT_BYTES // (1024 * 1024),
    # 流式消息占 1 次被动回复，单条用户消息共 4 次 → 出站文件 1+N ≤ 4
    max_files_per_turn=3,
)

register_profile(PROFILE)
