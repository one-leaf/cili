"""Web 接入端的能力档案。

渲染面事实来自 ``web/static/utils.js`` 的 ``renderMarkdown``：marked + DOMPurify
（放行 MathJax 标签与 iframe）、mermaid 代码块渲染为 SVG、相对路径图片改写为
``/api/workspaces/{uuid}/files/...``、PDF 转 iframe 预览。

注册时机：``web/deps.py`` import 本模块（所有 web 路由的公共依赖）。
"""

from core.interface_profile import InterfaceProfile, register_profile

PROFILE = InterfaceProfile(
    name="web",
    label="Web UI (browser client)",
    renders=(
        "markdown",
        "tables",
        "LaTeX",
        "mermaid",
        "inline SVG",
        "images by workspace-relative path",
        "PDF preview",
    ),
    max_message_chars=None,
    streaming=True,
    thinking_visible=True,
    # send_file 在 Web 落地为下载卡片：回合末 drain outbox → SSE file 帧 +
    # 落一条 file_card 消息（见 web/sse.drain_outbox_events）
    can_send_file=True,
    file_delivery="link",
    # 与 core/tools/send_file.MAX_FILE_BYTES 对齐（工具自身的兜底校验）
    file_size_limit_mb=200,
    guidance=(
        "Files: reference them by workspace-relative path, e.g. `![plot](plot.svg)`. "
        "Never use `file://` URLs or absolute paths.",
        "Do NOT rasterize a diagram to PNG for display — SVG and mermaid render "
        "natively.",
        "Write large artifacts (HTML/SVG/reports) to the workspace and reference "
        "them by path; never inline more than ~4KB of markup in a reply.",
    ),
)

register_profile(PROFILE)
