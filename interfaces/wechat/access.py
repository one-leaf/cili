"""owner 记录（微信侧）：与 bot 私聊的就是绑定微信的主人本人。

语义与实现见 ``interfaces/common/access.py``；改动共享实现时无需对照本文件。
"""

from interfaces.common.access import OwnerTracker

__all__ = ["OwnerTracker"]
