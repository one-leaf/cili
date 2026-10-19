"""终端接入端的能力档案（样例）。

接入实现落地时，在接入模块 import 本文件即完成注册（与 web/qq/wechat 同一
机制）。字段按终端的现实约束填写：

- markdown：等宽字体 + ANSI 渲染，表格/粗体可用，无 HTML；
- 图片：``image_protocol`` 按探测结果填（kitty/iterm2 可内嵌，其余纯文本），
  对齐 pi 的 ``TerminalCapabilities``（只有 kitty/iterm2，无 sixel）；
- 无 send_user_file：文件交付=告诉用户工作区路径；
- 流式：终端逐行刷新即可。
"""

from core.interface_profile import InterfaceProfile, register_profile

PROFILE = InterfaceProfile(
    name="tui",
    label="terminal (CLI/TUI client)",
    renders=("markdown", "tables"),
    not_rendered=("HTML", "inline SVG", "mermaid", "LaTeX", "inline images"),
    max_message_chars=None,
    streaming=True,
    thinking_visible=True,
    image_protocol="none",  # 接入实现探测后可覆盖（kitty|iterm2）
    can_send_file=False,
    file_delivery="none",
    guidance=(
        "Files: tell the user the workspace-relative path; the terminal cannot "
        "open files or render images inline.",
        "Avoid wide tables and long unbroken lines — the terminal wraps hard.",
    ),
)

register_profile(PROFILE)
