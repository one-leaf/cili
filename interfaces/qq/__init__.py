"""QQ 接入端（官方 QQ 机器人，多账号）。

**公开面只有这几个符号**（对齐官方插件每个模块一个 barrel 的约定）：其余模块
视为包内私有，不要在包外直接 import——``config`` / ``state`` 除外，它们被设置
读写与测试需要。

SDK 是**延迟导入**的（``service.py`` 内部导入 ``gateway``），所以未启用 / 未安装
``qqbot-agent-sdk`` 时，import 本包不会报错，也不会加载 SDK。

启停函数的 ``account_id`` 参数为 None 时作用于全部账号（设计 §4.4）。
"""

from interfaces.qq import profile as _profile  # noqa: F401  —— import 副作用：注册 QQ 能力档案
from interfaces.qq.config import (
    QQConfig,
    load_qq_accounts,
    load_qq_config,
    save_qq_accounts,
    save_qq_config,
)
from interfaces.qq.service import (
    qq_interface_running,
    start_qq_interface,
    stop_qq_interface,
)

__all__ = [
    "QQConfig",
    "load_qq_accounts",
    "load_qq_config",
    "save_qq_accounts",
    "save_qq_config",
    "start_qq_interface",
    "stop_qq_interface",
    "qq_interface_running",
]
