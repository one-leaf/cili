"""接入端通道注册表：通道 id / 展示名 / 默认会话标题 / 能力档案映射。

为什么集中在一处
----------------
「通道」同时出现在四处：会话落盘的归属标记（``channel`` metadata）、
入站解析（latest / dedicated）、能力档案（``_turn_interface`` 对应的
``core.interface_profile``）以及界面徽标/设置页。名字散落会导致
「标记写 qq、档案叫 QQ、列表显示未知」这类漂移。

本模块不 import 任何其他 core/ 模块，供 core、interfaces、web 共同引用。

通道 id 与能力档案名解耦
------------------------
终端通道的产品名与能力档案名统一为 ``cli``（§4 契约）。

会话归属标记
------------
会话在创建时把三元组写进 ``meta.json`` 的 ``metadata``：

- ``channel``：通道 id（qq / wechat / cli / email / web / cron）
- ``channel_account``：通道内账号（QQ/微信账号 id、邮箱地址；本地通道为空）
- ``channel_chat``：账号内聊天/对方（openid、user_id、发件人；本地通道为空）

独立会话模式按三元组**精确匹配**——每个账号、每个聊天各有一条自己的会话。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Channel:
    """一个接入端通道的静态声明。"""

    id: str
    label: str
    default_session_title: str
    profile: str | None      # 能力档案名（core/interface_profile）
    kind: str                # "local"（web/cli/cron）| "remote"（qq/wechat/email）


CHANNELS: dict[str, Channel] = {
    "web": Channel("web", "网页", "网页会话", profile="web", kind="local"),
    "cli": Channel("cli", "终端", "CLI 会话", profile="cli", kind="local"),
    "qq": Channel("qq", "QQ", "QQ 会话", profile="qq", kind="remote"),
    "wechat": Channel("wechat", "微信", "微信会话", profile="wechat", kind="remote"),
    # 邮件通道预留：实现时在 interfaces/email/profile.py 注册同名能力档案
    "email": Channel("email", "邮件", "邮件会话", profile="email", kind="remote"),
    "cron": Channel("cron", "定时", "Cron", profile=None, kind="local"),
}

# 允许经 Web API 新建会话时声明的通道：远程通道的会话由各自接入端创建，
# cron 会话由调度器创建——都不接受客户端指定。
API_CREATE_CHANNEL_IDS = frozenset({"web", "cli"})

# 会话模式
SESSION_MODE_LATEST = "latest"
SESSION_MODE_DEDICATED = "dedicated"
SESSION_MODES = (SESSION_MODE_LATEST, SESSION_MODE_DEDICATED)
DEFAULT_SESSION_MODE = SESSION_MODE_DEDICATED

# 标记键名（meta.json → metadata）
MARKER_CHANNEL = "channel"
MARKER_ACCOUNT = "channel_account"
MARKER_CHAT = "channel_chat"


def get_channel(channel_id: str | None) -> Channel | None:
    """按 id 取通道声明；未知返回 None（调用方决定兜底）。"""
    if not channel_id:
        return None
    return CHANNELS.get(channel_id)


def marker_metadata(channel: str, account: str = "", chat: str = "") -> dict:
    """构造写进会话 metadata 的归属标记。"""
    return {
        MARKER_CHANNEL: channel,
        MARKER_ACCOUNT: account or "",
        MARKER_CHAT: chat or "",
    }


def session_marker(metadata: dict | None) -> tuple[str, str, str]:
    """从会话 metadata 取三元组；无标记返回 ``("", "", "")``。"""
    if not isinstance(metadata, dict):
        return ("", "", "")
    return (
        str(metadata.get(MARKER_CHANNEL) or ""),
        str(metadata.get(MARKER_ACCOUNT) or ""),
        str(metadata.get(MARKER_CHAT) or ""),
    )


def normalize_session_mode(value: str | None, default: str = DEFAULT_SESSION_MODE) -> str:
    """模式字符串归一化：非法值回落默认。"""
    val = (value or "").strip().lower()
    return val if val in SESSION_MODES else default
