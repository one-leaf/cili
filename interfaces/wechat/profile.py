"""微信（iLink）接入端的能力档案。

事实来源（不重述，改协议常量后提示词自动跟随）：
- ``max_message_chars`` ← ``interfaces.wechat.config.DEFAULT_TEXT_CHUNK_LIMIT``
  （iLink 单条硬限制 1800 字符）
- ``inline_image_exts`` ← ``interfaces.wechat.media._IMAGE_EXTS``（出站图片项的
  类型集；其余走文件项）

与 QQ 的关键差别（进 guidance 而非字段）：iLink 客户端对 markdown 的渲染很弱，
``clean_markdown_for_wechat`` 会剥掉图片语法、``~~``、裸 ``<``/``>`` 与 5-6 级
标题——所以渲染面按「plain markdown」声明，并把清洗行为显式告诉模型。

注册时机：``interfaces/wechat/__init__.py`` import 本模块。
"""

from core.interface_profile import InterfaceProfile, register_profile
from interfaces.wechat.config import DEFAULT_TEXT_CHUNK_LIMIT
from interfaces.wechat.media import _IMAGE_EXTS

PROFILE = InterfaceProfile(
    name="wechat",
    label="WeChat (iLink) private chat",
    renders=("plain markdown",),
    not_rendered=("inline images", "mermaid", "SVG", "LaTeX", "raw HTML"),
    max_message_chars=DEFAULT_TEXT_CHUNK_LIMIT,
    inline_image_exts=tuple(sorted(_IMAGE_EXTS)),
    streaming=False,
    can_send_file=True,
    file_delivery="upload",
    # iLink CDN 无公开上限，取保守值：media.upload_media 整读内存（read_bytes），
    # 过大文件会在上传前先撑爆内存；真超限由上传层报错兜底
    file_size_limit_mb=200,
    # 与正文/进度共享每个 context_token 的约 8 条预算，留 1 条给正文
    max_files_per_turn=3,
    guidance=(
        "Images, `~~`, `<`/`>` and H5/H6 headings are stripped from your markdown.",
    ),
)

register_profile(PROFILE)
