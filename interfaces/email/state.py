"""邮件接入端的状态与游标（会话状态共享实现见 ``interfaces/common/state.py``）。

两层分离：

- ``data/cili/tools/email/state.json``：账号 → owner + chats（与 QQ/微信同构）
- ``data/cili/tools/email/accounts/{id}.json``：IMAP 游标（UIDVALIDITY + last_uid）
  与拦截统计。按账号分文件——游标每轮落盘，读-改-写互不覆盖（与微信凭证分文件
  同理）。

文件不存在 = 首次启动：轮询层据此跳过历史邮件（§3.7）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.config import DATA_DIR
from interfaces.common.session_routing import (
    bind_workspace as _bind_workspace,
)
from interfaces.common.session_routing import (
    ensure_session as _ensure_session,
)
from interfaces.common.state import (
    AccountState as _AccountState,
)
from interfaces.common.state import (
    ChatState as _ChatState,
)
from interfaces.common.state import (
    load_account_state as _load_account_state,
)
from interfaces.common.state import (
    save_account_state as _save_account_state,
)

bind_workspace = _bind_workspace

_LEGACY_OWNER_KEY = "owner_email"
_LOG_TAG = "email"

# ─── 会话状态（与 QQ/微信同构） ───────────────────────────────────────


def state_path() -> Path:
    """状态文件路径（惰性求值，便于测试 monkeypatch ``DATA_DIR``）。"""
    return DATA_DIR / "tools" / "email" / "state.json"


@dataclass
class EmailChatState(_ChatState):
    """一个发件人（聊天）的当前工作区/会话绑定。"""


@dataclass
class EmailAccountState(_AccountState):
    """一个账号的状态切片。"""

    CHAT_TYPE = EmailChatState


def load_account_state(account_id: str) -> EmailAccountState:
    return _load_account_state(
        account_id, state_path=state_path,
        account_cls=EmailAccountState, legacy_owner_key=_LEGACY_OWNER_KEY,
    )


def save_account_state(account_id: str, account: EmailAccountState) -> bool:
    return _save_account_state(
        account_id, account, state_path=state_path,
        legacy_owner_key=_LEGACY_OWNER_KEY, log_tag=_LOG_TAG,
    )


def ensure_session(
    account: EmailAccountState, chat_id: str, cfg, *, account_id: str = "",
) -> EmailChatState:
    return _ensure_session(
        account, chat_id, cfg,
        session_title="邮件会话", log_tag=_LOG_TAG,
        channel="email", account_id=account_id,
    )
