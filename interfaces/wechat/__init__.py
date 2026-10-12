"""微信（iLink）接入端：个人微信私聊，仅单人使用。

**公开面只有这几个符号**（对齐 QQ 接入端的 barrel 约定）：其余模块视为包内私有，
不要在包外直接 import——``config`` / ``account`` 除外，它们被设置读写与测试需要。

与 QQ 接入端的最大差别：**没有官方 Python SDK**——iLink 是纯 HTTP 协议
（``ilinkai.weixin.qq.com``），所以 ``protocol`` / ``client`` / ``login`` / ``poller``
都是本项目自己实现的。也因此 import 本包不会拉进任何重依赖（``client`` 等重模块都在
函数内部延迟导入）。
"""

from interfaces.wechat.account import WechatAccount, load_account, save_account
from interfaces.wechat.config import WechatConfig, load_wechat_config, save_wechat_config
from interfaces.wechat.service import (
    start_wechat_interface,
    stop_wechat_interface,
    wechat_interface_running,
)

__all__ = [
    "WechatAccount",
    "WechatConfig",
    "load_account",
    "load_wechat_config",
    "save_account",
    "save_wechat_config",
    "start_wechat_interface",
    "stop_wechat_interface",
    "wechat_interface_running",
]
