"""邮件接入端：IMAP 收 + SMTP 回，多账号，仅白名单发件人。

**公开面**：启停函数（``account_id`` 为 None 时作用于全部账号）与配置读写。
其余模块视为包内私有。导入本包会注册邮件能力档案（``profile.py`` 的副作用），
不拉进任何第三方依赖（stdlib ``imaplib`` / ``smtplib``）。
"""

from interfaces.email import profile as _profile  # noqa: F401  —— import 副作用：注册能力档案
from interfaces.email.config import (
    EmailConfig,
    load_email_accounts,
    save_email_accounts,
)
from interfaces.email.service import (
    email_interface_running,
    email_status_snapshot,
    start_email_interface,
    stop_email_interface,
)

__all__ = [
    "EmailConfig",
    "email_interface_running",
    "email_status_snapshot",
    "load_email_accounts",
    "save_email_accounts",
    "start_email_interface",
    "stop_email_interface",
]
