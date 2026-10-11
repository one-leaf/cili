"""QQ 接入端的运行时容器：把 SDK 对象与共享状态打成一包。

单独成文件是为了避免 ``gateway`` / ``inbound`` / ``sink`` 之间的循环导入——
三者都只依赖这个纯数据容器。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from interfaces.qq.config import QQConfig
from interfaces.qq.limiter import ReplyLimiter
from interfaces.qq.state import QQState


@dataclass
class QQRuntime:
    """一次进程生命周期内共享的 QQ 运行时依赖。"""

    cfg: QQConfig
    state: QQState
    loop: asyncio.AbstractEventLoop          # 宿主 loop（入站回调被派发到它上面）
    api: Any = None                          # qqbot_agent_sdk.QQApiClient
    ws: Any = None                           # qqbot_agent_sdk.QQWebSocket
    http: Any = None                         # httpx.AsyncClient（分片 PUT / 附件下载共用）
    uploader: Any = None                     # qqbot_agent_sdk.MediaUploader
    limiter: ReplyLimiter = field(default_factory=ReplyLimiter)

    def ready(self) -> bool:
        """SDK 对象是否齐备（缺 SDK 时启动会跳过，不应走到这里）。"""
        return self.api is not None and self.uploader is not None
