"""owner 记录：QQ / 微信 / 邮件三接口共用的「最近对话对象」跟踪。

owner 是「本账号最近一次对话的对方」——恒等于 ``account.chats`` 的某个键，
供 ``/status`` 展示与日志用。**不是访问闸门**，谁能触发回合由各通道的
入口把关：

- QQ：平台即闸门——未认证机器人只有管理员能用，其他人发不进来
- 微信：与 bot 私聊的就是绑定微信的主人本人
- 邮箱：没有平台闸门，由白名单把关（``interfaces.email.filter.check_message``）

若将来某个通道对公众开放（机器人公开、好友对话、转发邮箱），需要加白名单。

不落盘的理由：owner 恒等于某个聊天键，而聊天绑定要么由命令（/w /s /new）
落盘、要么按默认规则即时解析（普通消息），owner 本身没有独立的持久化价值
——重启后第一条消息会把它刷新回来。
"""

from __future__ import annotations

import logging

from interfaces.common.state import AccountState

logger = logging.getLogger(__name__)


class OwnerTracker:
    """跟踪账号的 owner（每次对话自动更新，不落盘）。"""

    def __init__(self, account: AccountState, *, log_tag: str = "interface") -> None:
        self._account = account
        self._log_tag = log_tag

    @property
    def owner(self) -> str:
        return self._account.owner

    def track(self, sender: str) -> bool:
        """记录本次对话的发送者。返回是否可继续处理（空发送者返回 False）。"""
        if not sender:
            return False
        if self._account.owner != sender:
            previous = self._account.owner
            self._account.owner = sender
            if previous:
                logger.info(f"[{self._log_tag}/owner] owner 已更新：{previous} → {sender}")
            else:
                logger.info(f"[{self._log_tag}/owner] 已绑定 owner：{sender}")
        return True
