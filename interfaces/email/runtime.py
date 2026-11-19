"""邮件接入端的运行时容器：把共享依赖打成一包。

单独成文件是为了避免 ``poller`` / ``inbound`` / ``sink`` 之间的循环导入——它们
都只依赖这个纯数据容器。每个账号一个 runtime 实例（与 QQ/微信的多账号模型一致）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from interfaces.email.config import EmailConfig
from interfaces.email.state import EmailAccountState

if TYPE_CHECKING:
    from interfaces.email.client import EmailClient
    from interfaces.email.inbound import InboundHandler
    from interfaces.email.poller import Poller


@dataclass
class EmailRuntime:
    """一个账号进程生命周期内共享的邮件运行时依赖。"""

    account_id: str                          # 账号标识（= 邮箱地址）
    cfg: EmailConfig                         # 账号级配置（每账号独立实例）
    state: EmailAccountState                 # 该账号的状态切片
    loop: asyncio.AbstractEventLoop          # 宿主 loop
    client: "EmailClient | None" = None
    poller: "Poller | None" = None
    handler: "InboundHandler | None" = None
    # 供 /api/email/status 展示（由 poller 更新）
    status: dict = field(default_factory=dict)
