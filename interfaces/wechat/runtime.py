"""微信接入端的运行时容器：把共享依赖打成一包。

单独成文件是为了避免 ``sink`` / ``inbound`` / ``poller`` 之间的循环导入——它们都只
依赖这个纯数据容器。

``client`` / ``typing`` / ``outbound`` 用 ``TYPE_CHECKING`` 引入：容器本身不需要在
导入期拉起 HTTP 层与发送层（也顺带避免和 ``outbound`` 之间形成环）。

多账号模型下（设计 §3.4）每个账号一个 runtime 实例：``account_id`` 用于
日志、凭证与状态的定位；``state`` 是该账号的状态切片。``ContextTokenStore``
等按 user_id 分键的对象都挂在实例上，多账号天然隔离。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from interfaces.wechat.account import ContextTokenStore, WechatAccount
from interfaces.wechat.config import WechatConfig
from interfaces.wechat.state import WechatAccountState

if TYPE_CHECKING:
    from interfaces.wechat.client import WechatClient
    from interfaces.wechat.outbound import OutboundSender
    from interfaces.wechat.typing import TypingManager


@dataclass
class WechatRuntime:
    """一个账号进程生命周期内共享的微信运行时依赖。"""

    account_id: str                          # 账号标识（凭证/状态/日志定位）
    cfg: WechatConfig                        # 账号级配置（每账号独立实例）
    state: WechatAccountState                # 该账号的状态切片
    account: WechatAccount                   # 该账号的凭证（token + 游标）
    tokens: ContextTokenStore
    loop: asyncio.AbstractEventLoop          # 宿主 loop（入站回调与发送都派到它上面）
    client: "WechatClient | None" = None
    typing: "TypingManager | None" = None
    outbound: "OutboundSender | None" = None

    def ready(self) -> bool:
        """运行时依赖是否齐备（缺就说明启动没走完）。"""
        return self.client is not None and self.outbound is not None
