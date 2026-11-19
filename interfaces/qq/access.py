"""owner 记录（QQ 侧）：平台即闸门——未认证机器人只有管理员能用。

语义与实现见 ``interfaces/common/access.py``；改动共享实现时无需对照本文件。
"""

from interfaces.common.access import OwnerTracker

__all__ = ["OwnerTracker"]
