"""微信接入端的运行时容器：把共享依赖打成一包。

单独成文件是为了避免 ``sink`` / ``inbound`` / ``poller`` 之间的循环导入——它们都只
依赖这个纯数据容器。

``client`` / ``typing`` / ``outbound`` 用 ``TYPE_CHECKING`` 引入：容器本身不需要在
导入期拉起 HTTP 层与发送层（也顺带避免和 ``outbound`` 之间形成环）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from interfaces.wechat.account import ContextTokenStore, WechatAccount
from interfaces.wechat.config import WechatConfig
from interfaces.wechat.state import WechatState

if TYPE_CHECKING:
    from interfaces.wechat.client import WechatClient
    from interfaces.wechat.outbound import OutboundSender
    from interfaces.wechat.typing import TypingManager


@dataclass
class WechatRuntime:
    """一次进程生命周期内共享的微信运行时依赖。"""

    cfg: WechatConfig
    state: WechatState
    account: WechatAccount
    tokens: ContextTokenStore
    loop: asyncio.AbstractEventLoop          # 宿主 loop（入站回调与发送都派到它上面）
    client: "WechatClient | None" = None
    typing: "TypingManager | None" = None
    outbound: "OutboundSender | None" = None

    def ready(self) -> bool:
        """运行时依赖是否齐备（缺就说明启动没走完）。"""
        return self.client is not None and self.outbound is not None
