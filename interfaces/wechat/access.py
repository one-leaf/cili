"""owner 记录：自动绑定，多账号下退化为「本账号最近对话的对方」。

这里**不做白名单**——每次对话把 owner 更新为当前发送者：

- **自动绑定**：首次私聊就记下来，不需要你去翻自己的 user_id 再填配置
- **保持最新**：user_id 会变，每次对话刷新，不用手工维护

多账号 + 多聊天模型下（设计 §3.3），owner 不再是「唯一绑定者」——它只是
本账号最近一次对话的 chat 键，恒等于 ``account.chats`` 的某个键，供
``/status`` 展示与日志用。**不是访问闸门**：任何能给机器人发私聊的人都会
成为 owner。ClawBot 的产品形态下，与 bot 私聊的就是绑定微信的主人本人；
若将来把 bot 入口分享给他人（或产品开放好友对话），需要加白名单。

与 QQ 侧 ``interfaces/qq/access.py`` 同构；改动这里时请对照那份。
"""

from __future__ import annotations

import logging

from interfaces.wechat.state import WechatAccountState

logger = logging.getLogger(__name__)


class OwnerTracker:
    """跟踪账号的 owner（每次对话自动更新，不落盘）。

    不落盘的理由：owner 恒等于某个聊天键，而聊天绑定要么由命令（/w /s
    /new）落盘、要么按默认规则即时解析（普通消息），owner 本身没有独立
    的持久化价值——重启后第一条消息会把它刷新回来。
    """

    def __init__(self, account: WechatAccountState) -> None:
        self._account = account

    @property
    def owner_user_id(self) -> str:
        return self._account.owner

    def track(self, sender_user_id: str) -> bool:
        """记录本次对话的发送者。返回是否可继续处理（空发送者返回 False）。"""
        if not sender_user_id:
            return False
        if self._account.owner != sender_user_id:
            previous = self._account.owner
            self._account.owner = sender_user_id
            if previous:
                logger.info(f"[wechat/owner] owner 已更新：{previous} → {sender_user_id}")
            else:
                logger.info(f"[wechat/owner] 已绑定 owner：{sender_user_id}")
        return True
