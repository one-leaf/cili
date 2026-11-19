"""邮件接入端的能力档案。

事实来源：出站体积/条数上限来自 ``interfaces.email.config`` 的默认值，
入站附件与正文格式由 ``outbound.py`` / ``media.py`` 决定。

与 QQ/微信的关键差别（进 guidance 而非字段）：回信是**纯文本邮件**——表格、
内联图片、mermaid 都不会渲染；一次回合只回一封信。

注册时机：``interfaces/email/__init__.py`` import 本模块。
"""

from core.interface_profile import InterfaceProfile, register_profile
from interfaces.email.config import (
    DEFAULT_MAX_OUTBOUND_BYTES,
    DEFAULT_MAX_OUTBOUND_FILES,
)

PROFILE = InterfaceProfile(
    name="email",
    label="Email (IMAP/SMTP)",
    renders=("plain text",),
    not_rendered=("markdown tables", "inline images", "mermaid", "SVG", "LaTeX", "raw HTML"),
    inline_image_exts=(),
    streaming=False,
    can_send_file=True,
    file_delivery="attachment",
    file_size_limit_mb=DEFAULT_MAX_OUTBOUND_BYTES // (1024 * 1024),
    max_files_per_turn=DEFAULT_MAX_OUTBOUND_FILES,
    guidance=(
        "Replies are plain-text email; avoid tables and inline images. "
        "Use short paragraphs and simple lists.",
    ),
)

register_profile(PROFILE)
