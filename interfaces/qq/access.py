"""owner 记录：单用户模型下的自动绑定。

Cili 一个实例只有一个 QQ 接口、一个 owner。这里**不做白名单**——每次对话把 owner
更新为当前发送者并落盘：

- **自动绑定**：首次私聊就记下来，不需要你去翻自己的 openid 再填配置
- **保持最新**：openid 会变（换设备、换号），每次对话刷新，不用手工维护

⚠️ 这意味着**任何能给机器人发私聊的人都会成为 owner**。安全性依赖 QQ 平台自身的
限制：未认证机器人只有管理员能用，其他人根本发不进来。若将来机器人公开使用，这里
需要重新加白名单。

owner 的用途是**状态记录与日志**（`/status` 可查、启动时打印），不是访问闸门。
"""

from __future__ import annotations

import logging

from interfaces.qq.state import QQState, save_state

logger = logging.getLogger(__name__)


class OwnerTracker:
    """跟踪当前 owner（每次对话自动更新）。"""

    def __init__(self, state: QQState) -> None:
        self._state = state

    @property
    def owner_openid(self) -> str:
        return self._state.owner_openid

    def track(self, sender_openid: str) -> bool:
        """记录本次对话的发送者为 owner。返回是否可继续处理（空发送者返回 False）。"""
        if not sender_openid:
            return False
        if self._state.owner_openid != sender_openid:
            self._adopt(sender_openid)
        return True

    def _adopt(self, sender_openid: str) -> None:
        previous = self._state.owner_openid
        self._state.owner_openid = sender_openid
        save_state(self._state)
        if previous:
            logger.info(f"[qq/owner] owner 已更新：{previous} → {sender_openid}")
        else:
            logger.info(f"[qq/owner] 已绑定 owner：{sender_openid}（单用户模式）")
