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

from core.channels import (
    DEFAULT_SESSION_MODE,
    SESSION_MODE_DEDICATED,
    SESSION_MODES,
    get_channel,
    marker_metadata,
    normalize_session_mode,
    session_marker,
)
from core.fs_utils import atomic_write_json, load_json_or_backup

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


# ─── 会话读取/创建 ──────────────────────────────────────────────────


def _sessions_dir(workspace_uuid: str) -> Path:
    from core.config import get_workspace_sessions_dir

    return get_workspace_sessions_dir(workspace_uuid)


def _list_sessions(workspace_uuid: str) -> list[dict]:
    """工作区会话摘要（已按 updated_at 倒序；metadata 已摊平）。"""
    from core.session import SessionStore

    if not workspace_uuid:
        return []
    return SessionStore.list_sessions(_sessions_dir(workspace_uuid))


def _session_exists(workspace_uuid: str, session_id: str) -> bool:
    """绑定的会话是否仍存在（可能已被 Web 删除）。"""
    if not workspace_uuid or not session_id:
        return False
    return (_sessions_dir(workspace_uuid) / session_id / "index.json").exists()


def newest_visible_session(workspace_uuid: str) -> str:
    """工作区最近更新的**未隐藏、非定时任务**会话 id（latest 模式口径）。

    cron 自动化会话不属于「人和 AI 的对话」：任务跑得再新，也不该成为
    下一条消息的落点（用户仍可在 /s 列表里手动指过去）。
    """
    for s in _list_sessions(workspace_uuid):
        if s.get("hidden"):
            continue
        if session_marker(s)[0] == "cron":
            continue
        if str(s.get("name") or "").startswith("[Cron]"):   # 旧数据无标记
            continue
        return str(s.get("session_id") or "")
    return ""


def latest_session_id(workspace_uuid: str) -> str:
    """兼容旧导入：等于 :func:`newest_visible_session`。"""
    return newest_visible_session(workspace_uuid)


def newest_marked_session(
    workspace_uuid: str, channel: str, account_id: str = "", chat_id: str = "",
) -> str:
    """最近更新的、归属标记与三元组完全一致的会话 id（dedicated 模式口径）。"""
    want = (channel, account_id or "", chat_id or "")
    for s in _list_sessions(workspace_uuid):
        if session_marker(s) == want:
            return str(s.get("session_id") or "")
    return ""


def create_marked_session(
    workspace_uuid: str, title: str, channel: str,
    account_id: str = "", chat_id: str = "", log_tag: str = "",
) -> str:
    """新建会话并盖通道归属标记（§3.6：没有合适会话就新建）。"""
    from core.session import SessionStore

    sessions_dir = _sessions_dir(workspace_uuid)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore.create_new_session(
        sessions_dir, title,
        metadata=marker_metadata(channel, account_id, chat_id),
    )
    logger.info(
        f"[{log_tag or channel}/state] 新建独立会话："
        f"{workspace_uuid} → {store.session_id}（{title}）"
    )
    return store.session_id


def _session_meta(workspace_uuid: str, session_id: str) -> dict:
    from core.session import read_meta

    return read_meta(_sessions_dir(workspace_uuid) / session_id)


def _stamp_marker(
    workspace_uuid: str, session_id: str, channel: str,
    account_id: str, chat_id: str,
) -> None:
    """给已存在会话补写标记。

    有活跃 runner 时改它的内存对象并 save（否则 runner 下次 checkpoint
    会用旧 metadata 覆盖磁盘标记）；没有则直接原子改写 meta.json。
    """
    marker = marker_metadata(channel, account_id, chat_id)
    try:
        from core.session_registry import registry, session_key

        runner = registry.get(session_key(workspace_uuid, session_id))
        session = getattr(runner, "session", None) if runner is not None else None
        if session is not None and hasattr(session, "metadata"):
            session.metadata.update(marker)
            session.save(force=True)
            return
    except Exception:  # noqa: BLE001 — 标记失败不该阻断回合
        logger.warning("[channel/state] 经 runner 补写会话标记失败，改走 meta.json", exc_info=True)

    from core.session import mark_session_meta

    mark_session_meta(_sessions_dir(workspace_uuid) / session_id, marker)


def _legacy_adoptable(chat: ChatState, channel: str, title: str) -> bool:
    """旧绑定是否可被收养为该通道的独立会话。

    条件：会话存在、没有任何通道标记、标题等于该通道的旧默认标题
    （「QQ 会话」/「微信会话」）。旧回退可能把绑定指到 Web 会话上，
    标题不符即排除，避免把别家的会话改写成自己的。
    """
    if not chat.session_id or not _session_exists(chat.workspace_uuid, chat.session_id):
        return False
    meta = _session_meta(chat.workspace_uuid, chat.session_id)
    if not meta:
        return False
    if session_marker(meta.get("metadata")) != ("", "", ""):
        return False
    return str(meta.get("name") or "") == title


def _effective_mode(chat: ChatState, cfg) -> str:
    """锁定 > 聊天级覆盖 > 账号级默认（非法值回落 dedicated）。"""
    if bool(getattr(cfg, "session_locked", False)):
        return SESSION_MODE_DEDICATED
    if chat.session_mode in SESSION_MODES:
        return chat.session_mode
    return normalize_session_mode(getattr(cfg, "session_mode", ""), DEFAULT_SESSION_MODE)


def effective_session_mode(chat: ChatState, cfg) -> str:
    """公开的只读版本（指令层展示用）。"""
    return _effective_mode(chat, cfg)


def bind_workspace(account: AccountState, chat_id: str, cfg) -> ChatState:
    """解析某个聊天的当前**工作区**（会话由 :func:`resolve_turn_session` 决定）。

    锁定 → 强制默认工作空间；否则「聊天已绑定 → 账号默认 → Default」。
    绑定失效（会话被删）时清空会话与 pin——**不再回退到「工作区最新会话」**，
    那条旧路径正是 QQ/微信串进 Web 会话的来源。
    """
    from core.config import ensure_default_workspace, find_workspace_entry

    def _valid_ws(uuid: str) -> bool:
        return bool(uuid) and find_workspace_entry(uuid) is not None

    chat = account.chat(chat_id)

    if cfg.workspace_locked:
        target = cfg.default_workspace_uuid if _valid_ws(cfg.default_workspace_uuid) \
            else (ensure_default_workspace() or "")
    elif _valid_ws(chat.workspace_uuid):
        target = chat.workspace_uuid
    elif _valid_ws(cfg.default_workspace_uuid):
        target = cfg.default_workspace_uuid
    else:
        target = ensure_default_workspace() or ""

    if chat.workspace_uuid != target:
        chat.workspace_uuid = target
        chat.session_id = ""      # 换了工作区，原会话不再适用
        chat.pinned = False
    if not _session_exists(chat.workspace_uuid, chat.session_id):
        chat.session_id = ""
        chat.pinned = False
    return chat


def resolve_turn_session(
    account: AccountState,
    chat_id: str,
    cfg,
    *,
    channel: str,
    account_id: str = "",
    title: str = "",
    create: bool = True,
) -> ChatState:
    """解析本回合的目标会话（模式的唯一入口）。

    顺序：pin → 旧绑定收养（仅独立模式）→ 模式匹配 → 新建并盖标记。
    """
    chat = bind_workspace(account, chat_id, cfg)
    if not chat.workspace_uuid:
        return chat

    chan = get_channel(channel)
    title = title or (chan.default_session_title if chan else channel)

    if chat.pinned and _session_exists(chat.workspace_uuid, chat.session_id):
        return chat

    mode = _effective_mode(chat, cfg)
    if mode == SESSION_MODE_DEDICATED and _legacy_adoptable(chat, channel, title):
        _stamp_marker(chat.workspace_uuid, chat.session_id, channel, account_id, chat_id)
        chat.pinned = True
        logger.info(
            f"[{channel}/state] 收养旧绑定会话：{chat.workspace_uuid} → {chat.session_id}"
        )
        return chat

    if mode == SESSION_MODE_DEDICATED:
        sid = newest_marked_session(chat.workspace_uuid, channel, account_id, chat_id)
    else:
        sid = newest_visible_session(chat.workspace_uuid)

    if not sid and create:
        sid = create_marked_session(
            chat.workspace_uuid, title, channel, account_id, chat_id, log_tag=channel,
        )
    chat.session_id = sid
    return chat


def peek_turn_session(
    chat: ChatState,
    cfg,
    *,
    channel: str,
    account_id: str = "",
    chat_id: str = "",
    title: str = "",
) -> str:
    """按模式的只读预览（不建会话、不写标记）——供 /status、/mode、/w 展示。"""
    if not chat.workspace_uuid:
        return ""
    if chat.pinned and _session_exists(chat.workspace_uuid, chat.session_id):
        return chat.session_id

    chan = get_channel(channel)
    title = title or (chan.default_session_title if chan else channel)
    mode = _effective_mode(chat, cfg)
    if mode == SESSION_MODE_DEDICATED:
        sid = newest_marked_session(chat.workspace_uuid, channel, account_id, chat_id)
        if not sid and _legacy_adoptable(chat, channel, title):
            return chat.session_id
        return sid
    return newest_visible_session(chat.workspace_uuid)


def ensure_session(
    account: AccountState,
    chat_id: str,
    cfg,
    *,
    session_title: str,
    log_tag: str,
    channel: str = "",
    account_id: str = "",
) -> ChatState:
    """兼容壳：等价于 ``resolve_turn_session``（channel 缺失时按标题推定）。"""
    if not channel:
        channel = "wechat" if "微信" in session_title else "qq"
    return resolve_turn_session(
        account, chat_id, cfg,
        channel=channel, account_id=account_id, title=session_title,
        create=True,
    )
