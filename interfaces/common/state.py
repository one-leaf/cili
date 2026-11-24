"""接入端共享状态：按「账号 → 聊天」两级分键（设计 §3.3）。

QQ 与微信各自保留状态文件和旧扁平字段名，但解析、并发读改写、
工作区/会话兜底逻辑完全一致，统一收敛在这里。

会话归属与模式（``workspace/default/session-channel-routing-plan-2026-11-16.md``）
--------------------------------------------------------------------------------
- 会话创建时盖通道三元组标记（``channel/channel_account/channel_chat``）；
- 两种模式：``latest``（工作区最新未隐藏会话）/ ``dedicated``（三元组匹配）；
- 账号级 ``session_locked`` 忽略 latest；聊天级 pin（``/s``）最高优先；
- 都没有合适会话就新建并盖标记。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.fs_utils import atomic_write_json, load_json_or_backup
from core.session_modes import SESSION_MODES

logger = logging.getLogger(__name__)


@dataclass
class ChatState:
    """一个聊天的当前工作区/会话绑定。"""

    workspace_uuid: str = ""
    session_id: str = ""
    # "" = 跟随账号级默认；latest | dedicated
    session_mode: str = ""
    # /s 显式指定过：解析时优先级最高（/mode、/new、切工作区会重置）
    pinned: bool = False

    @classmethod
    def from_dict(cls, data: dict | None) -> "ChatState":
        if not isinstance(data, dict):
            return cls()
        mode = str(data.get("session_mode") or "").strip().lower()
        return cls(
            workspace_uuid=str(data.get("workspace_uuid") or ""),
            session_id=str(data.get("session_id") or ""),
            session_mode=mode if mode in SESSION_MODES else "",
            pinned=bool(data.get("pinned", False)),
        )

    def to_dict(self) -> dict:
        return {
            "workspace_uuid": self.workspace_uuid,
            "session_id": self.session_id,
            "session_mode": self.session_mode,
            "pinned": self.pinned,
        }


@dataclass
class AccountState:
    """一个账号的状态切片：owner（最近对话的对方）+ 各聊天的绑定。"""

    owner: str = ""
    chats: dict[str, ChatState] = field(default_factory=dict)

    # 子类替换为具体的 ChatState 子类（不参与 dataclass 字段）
    CHAT_TYPE = ChatState

    def chat(self, chat_id: str) -> ChatState:
        """取（或建）某个聊天的状态切片。"""
        chat = self.chats.get(chat_id)
        if chat is None:
            chat = self.CHAT_TYPE()
            self.chats[chat_id] = chat
        return chat

    def is_empty(self) -> bool:
        return not self.owner and not self.chats

    @classmethod
    def from_dict(cls, data: dict | None) -> "AccountState":
        if not isinstance(data, dict):
            return cls()
        chats_raw = data.get("chats")
        chats: dict[str, ChatState] = {}
        if isinstance(chats_raw, dict):
            for k, v in chats_raw.items():
                if isinstance(v, dict):
                    chats[str(k)] = cls.CHAT_TYPE.from_dict(v)
        return cls(owner=str(data.get("owner") or ""), chats=chats)

    def to_dict(self) -> dict:
        return {
            "owner": self.owner,
            "chats": {k: v.to_dict() for k, v in self.chats.items()},
        }


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _legacy_account_state(
    data: dict, account_cls: type[AccountState], legacy_owner_key: str,
) -> AccountState:
    """旧扁平格式 → 单账号切片（owner 即聊天键）。"""
    owner = str(data.get(legacy_owner_key) or "")
    acct = account_cls(owner=owner)
    if owner:
        acct.chats[owner] = account_cls.CHAT_TYPE(
            workspace_uuid=str(data.get("workspace_uuid") or ""),
            session_id=str(data.get("session_id") or ""),
        )
    return acct


def _load_all_unlocked(
    path: Path, account_cls: type[AccountState], legacy_owner_key: str,
) -> dict[str, AccountState]:
    """读整份状态文件（调用方必须持锁）。旧扁平格式映射到键 ``""``。"""
    data = load_json_or_backup(path, {})
    if not isinstance(data, dict) or not data:
        return {}
    accounts_raw = data.get("accounts")
    if isinstance(accounts_raw, dict):
        return {
            str(k): account_cls.from_dict(v)
            for k, v in accounts_raw.items()
            if isinstance(v, dict)
        }
    return {"": _legacy_account_state(data, account_cls, legacy_owner_key)}


def load_account_state(
    account_id: str,
    *,
    state_path: Callable[[], Path],
    account_cls: type[AccountState],
    legacy_owner_key: str,
) -> AccountState:
    """读取一个账号的状态切片；旧扁平文件内容交给任何来问的账号。"""
    path = state_path()
    with _lock_for(path):
        all_state = _load_all_unlocked(path, account_cls, legacy_owner_key)
    if account_id in all_state:
        return all_state[account_id]
    legacy = all_state.get("")
    if legacy is not None and not legacy.is_empty():
        return legacy
    return account_cls()


def save_account_state(
    account_id: str,
    account: AccountState,
    *,
    state_path: Callable[[], Path],
    legacy_owner_key: str,
    log_tag: str,
) -> bool:
    """写回一个账号的状态切片（读-改-写整份文件，持锁）。"""
    path = state_path()
    try:
        with _lock_for(path):
            all_state = _load_all_unlocked(path, account_cls=type(account),
                                           legacy_owner_key=legacy_owner_key)
            all_state.pop("", None)
            all_state[account_id] = account
            payload = {"accounts": {k: v.to_dict() for k, v in all_state.items()}}
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, payload)
            return True
    except OSError as e:
        logger.warning(f"[{log_tag}] 保存状态失败: {e}")
        return False
