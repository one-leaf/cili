"""邮件接入端配置（实现位于 ``core.mail.accounts``）。

账号模型与读写函数已下沉到 core：``mail`` 工具和接入端共用同一份账号配置，
而 core 不反向依赖 interfaces。本模块只做 re-export，保持既有导入路径不变。

配置格式、凭证存放与调参默认值的说明见 ``core/mail/accounts.py`` 的模块 docstring。
"""

from __future__ import annotations

from core.mail.accounts import (
    CONFIG_SECTION,
    DEFAULT_ALLOWED_RECIPIENTS,
    DEFAULT_IMAP_PORT,
    DEFAULT_MAX_BODY_CHARS,
    DEFAULT_MAX_INBOUND_BYTES,
    DEFAULT_MAX_MESSAGES_PER_POLL,
    DEFAULT_MAX_OUTBOUND_BYTES,
    DEFAULT_MAX_OUTBOUND_FILES,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SMTP_PORT,
    DEFAULT_SMTP_SECURITY,
    DEFAULT_TURNS_PER_ACCOUNT_PER_DAY,
    DEFAULT_TURNS_PER_SENDER_PER_HOUR,
    EmailConfig,
    address_allowed,
    email_account_by_id,
    load_email_accounts,
    save_email_accounts,
)

__all__ = [
    "CONFIG_SECTION",
    "DEFAULT_ALLOWED_RECIPIENTS",
    "DEFAULT_IMAP_PORT",
    "DEFAULT_MAX_BODY_CHARS",
    "DEFAULT_MAX_INBOUND_BYTES",
    "DEFAULT_MAX_MESSAGES_PER_POLL",
    "DEFAULT_MAX_OUTBOUND_BYTES",
    "DEFAULT_MAX_OUTBOUND_FILES",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_SMTP_PORT",
    "DEFAULT_SMTP_SECURITY",
    "DEFAULT_TURNS_PER_ACCOUNT_PER_DAY",
    "DEFAULT_TURNS_PER_SENDER_PER_HOUR",
    "EmailConfig",
    "address_allowed",
    "email_account_by_id",
    "load_email_accounts",
    "save_email_accounts",
]