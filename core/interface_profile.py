"""接入端能力档案：当前驱动接口「能渲染什么 / 怎么交付文件」的单一事实源。

为什么需要
----------
提示词原先只有三处写死的 Web 假设（master.json 静态区），QQ/微信回合里模型
拿不到任何渲染面说明，只能按最保守的先验行事（典型：把 SVG 栅格化成 PNG 再
"推送给前端"，而 Web 前端原生渲染 SVG）。本模块把「接口能力」收敛成结构化
数据，``core/prompt_builder`` 的 ``interface_caps`` 块按当前回合的驱动接口
生成 ``## Response Surface`` 段落。

分层归属（设计文档 workspace/cili-interface-capability-plan-2026-10-15.md）
---------------------------------------------------------------------------
- **事实**（字符上限、可内联图片类型、体积上限）从各传输层的既有常量导入，
  在各接入端的 ``profile.py`` 里组装。core 不 import 接入端；档案随接入端
  模块 import 时注册（幂等），注册点：
  - ``web/deps.py`` → ``web/profile.py``
  - ``interfaces/qq/__init__.py`` → ``interfaces/qq/profile.py``
  - ``interfaces/wechat/__init__.py`` → ``interfaces/wechat/profile.py``
- **文案**（guidance）是行为指令，随档案放在接入端；role JSON 只通过
  ``interface_caps`` 块声明「要这一段」，内容由 :func:`render_capability_section`
  生成。

解析规则（:func:`resolve_profile`）
-----------------------------------
- ``_turn_interface`` 有已注册档案 → 用它（接口声明优先）。
- 未声明（None）：interactive **不猜**——对齐 Claude-Code
  ``questionPreviewFormat`` 的 undefined 语义，未声明就不注入，避免用看似
  合理的默认值掩盖「接口层忘了设置」；autonomous（worker/lite 子代理）落到
  ``AGENT_PROFILE``——它的输出是给父代理读的文本，不是给用户的界面。
- 未知接口名：warning 后按未声明处理。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InterfaceProfile:
    """一份接入端的能力声明。

    字段语义：
    - ``renders``：客户端能把哪些语法渲染成视觉结果（正面声明）。
    - ``not_rendered``：明确**不**渲染的东西（显式否定式声明——deepseek-harness
      的先例：告诉模型「没有什么」比只列正面能力更能挡住瞎猜）。
    - ``max_message_chars``：单条消息字符上限；None 表示无上限。
    - ``image_protocol``：终端图片协议（none|sixel|kitty|iterm2），为终端接入端
      预留（pi 的 TerminalCapabilities 只认 kitty/iterm2）。
    - ``file_delivery``：``upload``（上传到聊天）/ ``link``（transcript 内下载链接）
      / ``none``（不支持 send_file）。
    - ``guidance``：行为指令文案（含禁止项），逐行进入能力段。
    - ``extra``：前向扩展口——加新能力字段时不必改所有档案。
    """

    name: str
    label: str
    renders: tuple[str, ...] = ()
    not_rendered: tuple[str, ...] = ()
    inline_image_exts: tuple[str, ...] = ()
    max_message_chars: int | None = None
    streaming: bool = False
    thinking_visible: bool = False
    image_protocol: str = "none"
    can_send_file: bool = False
    file_delivery: str = "none"
    file_size_limit_mb: int | None = None
    guidance: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


# ─── 注册表 ──────────────────────────────────────────────────────────

_PROFILES: dict[str, InterfaceProfile] = {}


def register_profile(profile: InterfaceProfile) -> None:
    """注册（或覆盖）一份接口能力档案。幂等，重复注册以最后一次为准。"""
    _PROFILES[profile.name] = profile


def get_profile(name: str | None) -> InterfaceProfile | None:
    """按名字取档案。未知名字 warning 后返回 None（按未声明处理）。"""
    if not name:
        return None
    profile = _PROFILES.get(name)
    if profile is None:
        logger.warning(f"[interface_profile] 未知接口 {name!r}，按未声明处理")
    return profile


# ─── agent 档案：autonomous 子代理（core 自带，不是客户端） ───────────

AGENT_PROFILE = InterfaceProfile(
    name="agent",
    label="sub-agent (output is consumed by the calling agent)",
    not_rendered=("SVG", "HTML", "images", "mermaid"),
    guidance=(
        "Do NOT produce UI-oriented artifacts: no mermaid-for-display, "
        "no inline SVG, no `send_file`.",
        "Return text, file paths, and structured summaries.",
    ),
)
register_profile(AGENT_PROFILE)


def resolve_profile(runner) -> InterfaceProfile | None:
    """按 runner 的当前回合解析能力档案。

    只读 ``runner._turn_interface`` 与 ``runner._mode`` 两个属性。
    """
    profile = get_profile(getattr(runner, "_turn_interface", None))
    if profile is not None:
        return profile
    # 未声明：交互态不猜（见模块 docstring），autonomous 落到 agent 档案
    if getattr(runner, "_mode", "") == "autonomous":
        return get_profile("agent")
    return None


# ─── 能力段渲染 ──────────────────────────────────────────────────────


def _format_exts(exts: tuple[str, ...]) -> str:
    return "/".join(e.lstrip(".").upper() for e in sorted(exts))


def render_capability_section(profile: InterfaceProfile) -> str:
    """把能力档案渲染成 system prompt 段落（``## Response Surface``）。

    行顺序：身份 → 渲染面（正/反）→ 消息上限 → 文件交付 → 档案自带 guidance。
    只对「面向用户的客户端」（renders 非空）输出消息上限行——agent 档案
    不需要「Message limit: none」这类噪音。
    """
    lines = ["## Response Surface", "", f"You are replying through: **{profile.label}**."]

    if profile.renders:
        lines.append(f"Rendered natively: {', '.join(profile.renders)}.")
    if profile.not_rendered:
        lines.append(f"NOT rendered: {', '.join(profile.not_rendered)}.")

    if profile.renders:
        if profile.max_message_chars:
            lines.append(
                f"Message limit: {profile.max_message_chars} characters per message; "
                "longer replies are split automatically."
            )
        else:
            lines.append("Message limit: none.")

    if profile.can_send_file:
        if profile.file_delivery == "link":
            lines.append(
                "Files: call `send_file` to hand a workspace file to the user; "
                "it arrives as a download link in the transcript."
            )
        else:
            lines.append(
                "Files: call `send_file` to deliver a workspace file; "
                "it is uploaded and sent after the current turn."
            )
        if profile.inline_image_exts:
            lines.append(
                f"Only {_format_exts(profile.inline_image_exts)} arrive as image "
                "messages; other file types arrive as file cards."
            )
        if profile.file_size_limit_mb:
            lines.append(f"Max file size: {profile.file_size_limit_mb}MB.")

    lines.extend(profile.guidance)
    return "\n".join(lines)
