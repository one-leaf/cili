"""owner 记录（邮件侧）：邮箱没有平台闸门，owner 由白名单兜底。

与 QQ/微信不同，owner **不是**访问授权——谁能触发回合完全由白名单
（``allowed_senders``，``filter.check_message``）决定。语义与实现见
``interfaces/common/access.py``。
"""

from interfaces.common.access import OwnerTracker

__all__ = ["OwnerTracker"]
