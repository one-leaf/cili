"""owner 记录：单用户模型下的自动绑定。

Cili 一个实例只有一个微信接口、一个 owner。这里**不做白名单**——每次对话把 owner
更新为当前发送者并落盘：

- **自动绑定**：首次私聊就记下来，不需要你去翻自己的 user_id 再填配置
- **保持最新**：user_id 会变，每次对话刷新，不用手工维护

⚠️ 这意味着**任何能给这个微信号发私聊的人都会成为 owner**。QQ 那侧靠平台限制兜底
（未认证机器人只有管理员能用）；微信这侧没有等价保障——bot 号被谁加了好友，谁就能
发消息进来。若将来要公开使用，这里必须加白名单。

owner 的用途是**状态记录与日志**（``/status`` 可查、启动时打印），不是访问闸门。
"""

from __future__ import annotations

import logging

from interfaces.wechat.state import WechatState, save_state

logger = logging.getLogger(__name__)


class OwnerTracker:
    """跟踪当前 owner（每次对话自动更新）。"""

    def __init__(self, state: WechatState) -> None:
        self._state = state

    @property
    def owner_user_id(self) -> str:
        return self._state.owner_user_id

    def track(self, sender_user_id: str) -> bool:
        """记录本次对话的发送者为 owner。返回是否可继续处理（空发送者返回 False）。"""
        if not sender_user_id:
            return False
        if self._state.owner_user_id != sender_user_id:
            self._adopt(sender_user_id)
        return True

    def _adopt(self, sender_user_id: str) -> None:
        previous = self._state.owner_user_id
        self._state.owner_user_id = sender_user_id
        save_state(self._state)
        if previous:
            logger.info(f"[wechat/owner] owner 已更新：{previous} → {sender_user_id}")
        else:
            logger.info(f"[wechat/owner] 已绑定 owner：{sender_user_id}（单用户模式）")
