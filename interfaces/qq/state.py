"""QQ 接入端的持久化状态（共享实现见 ``interfaces/common/state.py``）。

存放位置 ``data/cili/tools/qq/state.json``——系统级而非 per-workspace，
因为「当前工作区」本身就是状态的一部分。旧扁平字段名是 ``owner_openid``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.config import DATA_DIR
from interfaces.common.state import (
    AccountState as _AccountState,
)
from interfaces.common.state import (
    ChatState as _ChatState,
)
from interfaces.common.state import (
    ensure_session as _ensure_session,
)
from interfaces.common.state import (
    latest_session_id,  # noqa: F401 —— 兼容旧导入
)
from interfaces.common.state import (
    load_account_state as _load_account_state,
)
from interfaces.common.state import (
    resolve_current as _resolve_current,
)
from interfaces.common.state import (
    save_account_state as _save_account_state,
)

resolve_current = _resolve_current

_LEGACY_OWNER_KEY = "owner_openid"
_LOG_TAG = "qq"


def state_path() -> Path:
    """状态文件路径（惰性求值，便于测试 monkeypatch ``DATA_DIR``）。"""
    return DATA_DIR / "tools" / "qq" / "state.json"


@dataclass
class QQChatState(_ChatState):
    """一个聊天的当前工作区/会话绑定。"""


@dataclass
class QQAccountState(_AccountState):
    """一个账号的状态切片。"""

    CHAT_TYPE = QQChatState


def load_account_state(account_id: str) -> QQAccountState:
    return _load_account_state(
        account_id, state_path=state_path,
        account_cls=QQAccountState, legacy_owner_key=_LEGACY_OWNER_KEY,
    )


def save_account_state(account_id: str, account: QQAccountState) -> bool:
    return _save_account_state(
        account_id, account, state_path=state_path,
        legacy_owner_key=_LEGACY_OWNER_KEY, log_tag=_LOG_TAG,
    )


def ensure_session(account: QQAccountState, chat_id: str, cfg) -> QQChatState:
    return _ensure_session(
        account, chat_id, cfg, session_title="QQ 会话", log_tag=_LOG_TAG,
    )
