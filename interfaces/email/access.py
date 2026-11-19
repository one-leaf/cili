"""owner 记录：本账号最近一次有效来信的发件人。

与 QQ/微信同构，但**语义更弱**：邮箱没有平台闸门，owner **不是**访问授权——
谁能触发回合完全由白名单（``allowed_senders``）决定（``filter.check_message``）。
owner 只作 ``/status`` 展示与日志。
"""

from __future__ import annotations

import logging

from interfaces.email.state import EmailAccountState

logger = logging.getLogger(__name__)


class OwnerTracker:
    """跟踪账号的 owner（每次有效来信自动刷新）。"""

    def __init__(self, account: EmailAccountState) -> None:
        self._account = account

    @property
    def owner(self) -> str:
        return self._account.owner

    def track(self, sender: str) -> bool:
        """记录本次来信的发件人。返回是否可继续处理（空发件人返回 False）。"""
        if not sender:
            return False
        if self._account.owner != sender:
            previous = self._account.owner
            self._account.owner = sender
            if previous:
                logger.info(f"[email/owner] owner 已更新：{previous} → {sender}")
            else:
                logger.info(f"[email/owner] 已记录 owner：{sender}")
        return True
