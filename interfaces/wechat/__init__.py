"""微信（iLink）接入端：个人微信私聊，多账号。

**公开面只有这几个符号**（对齐 QQ 接入端的 barrel 约定）：其余模块视为包内私有，
不要在包外直接 import——``config`` / ``account`` 除外，它们被设置读写与测试需要。

与 QQ 接入端的最大差别：**没有官方 Python SDK**——iLink 是纯 HTTP 协议
（``ilinkai.weixin.qq.com``），所以 ``protocol`` / ``client`` / ``login`` / ``poller``
都是本项目自己实现的。也因此 import 本包不会拉进任何重依赖（``client`` 等重模块都在
函数内部延迟导入）。

凭证读写按账号（设计 §5.1）：``load_account`` / ``save_account`` / ``clear_account``
必填 ``account_id``；启停函数的 ``account_id`` 为 None 时作用于全部账号。
"""

from interfaces.wechat import profile as _profile  # noqa: F401  —— import 副作用：注册微信能力档案
from interfaces.wechat.account import (
    WechatAccount,
    clear_account,
    list_account_ids,
    load_account,
    save_account,
)
from interfaces.wechat.config import (
    WechatConfig,
    load_wechat_accounts,
    load_wechat_config,
    save_wechat_accounts,
    save_wechat_config,
)
from interfaces.wechat.service import (
    start_wechat_interface,
    stop_wechat_interface,
    wechat_interface_running,
)

__all__ = [
    "WechatAccount",
    "WechatConfig",
    "clear_account",
    "list_account_ids",
    "load_account",
    "load_wechat_accounts",
    "load_wechat_config",
    "save_account",
    "save_wechat_accounts",
    "save_wechat_config",
    "start_wechat_interface",
    "stop_wechat_interface",
    "wechat_interface_running",
]
