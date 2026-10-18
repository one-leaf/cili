"""QQ 接入端的持久化状态：按「账号 → 聊天」两级分键（设计 §3.3）。

存放位置 ``data/cili/tools/qq/state.json``——**系统级**而非 per-workspace，
因为「当前工作区」本身就是状态的一部分。

**两级结构**：外层按账号（多账号各自独立），内层按聊天（同一账号下每个
聊天绑定自己的工作区/会话）。当前每个账号通常只有一个聊天；聊天级键让
「一个机器人服务多个人」与「多个机器人」共用同一个状态模型，不用再做
第二次迁移。

**兼容读**：旧扁平格式（``{owner_openid, workspace_uuid, session_id}``）按
单账号解析——owner 即聊天键。写函数只写新形态，旧键落盘即消失。

**并发**：多个账号的宿主线程会并发读改写同一份文件，``_STATE_LOCK``
串行化「读-改-写」，避免一个账号的保存覆盖另一个账号刚写的状态。

与 ``interfaces/wechat/state.py`` 是同构的两份实现：解析与兜底逻辑只有
几十行、且两边行为必须一致，但把它抽到 core 要改动已在跑的模块，收益
不抵改动成本，故各自保留一份。改动这里时请对照微信那份。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from core.config import DATA_DIR
from core.fs_utils import atomic_write_json, load_json_or_backup

logger = logging.getLogger(__name__)


def state_path() -> Path:
    """状态文件路径（惰性求值，便于测试 monkeypatch ``DATA_DIR``）。"""
    return DATA_DIR / "tools" / "qq" / "state.json"


# 串行化整文件「读-改-写」：两个账号的宿主线程并发保存时，后写的会覆盖
# 先写的账号切片。锁内不做任何耗时 IO 以外的操作。
_STATE_LOCK = threading.Lock()


@dataclass
class QQChatState:
    """一个聊天的当前工作区/会话绑定。"""

    workspace_uuid: str = ""
    session_id: str = ""

    @classmethod
    def from_dict(cls, data: dict | None) -> "QQChatState":
        if not isinstance(data, dict):
            return cls()
        return cls(
            workspace_uuid=str(data.get("workspace_uuid") or ""),
            session_id=str(data.get("session_id") or ""),
        )

    def to_dict(self) -> dict:
        return {"workspace_uuid": self.workspace_uuid, "session_id": self.session_id}


@dataclass
class QQAccountState:
    """一个账号的状态切片：owner（最近对话的对方）+ 各聊天的绑定。

    ``owner`` 只是状态记录与日志用（``/status`` 可查），不是访问闸门；
    它的值恒等于某个聊天键。
    """

    owner: str = ""
    chats: dict[str, QQChatState] = field(default_factory=dict)

    def chat(self, chat_id: str) -> QQChatState:
        """取（或建）某个聊天的状态切片。"""
        chat = self.chats.get(chat_id)
        if chat is None:
            chat = QQChatState()
            self.chats[chat_id] = chat
        return chat

    def is_empty(self) -> bool:
        return not self.owner and not self.chats

    @classmethod
    def from_dict(cls, data: dict | None) -> "QQAccountState":
        if not isinstance(data, dict):
            return cls()
        chats_raw = data.get("chats")
        chats: dict[str, QQChatState] = {}
        if isinstance(chats_raw, dict):
            for k, v in chats_raw.items():
                if isinstance(v, dict):
                    chats[str(k)] = QQChatState.from_dict(v)
        return cls(owner=str(data.get("owner") or ""), chats=chats)

    def to_dict(self) -> dict:
        return {
            "owner": self.owner,
            "chats": {k: v.to_dict() for k, v in self.chats.items()},
        }


def _legacy_account_state(data: dict) -> QQAccountState:
    """旧扁平格式 → 单账号切片（owner 即聊天键）。"""
    owner = str(data.get("owner_openid") or "")
    acct = QQAccountState(owner=owner)
    if owner:
        acct.chats[owner] = QQChatState(
            workspace_uuid=str(data.get("workspace_uuid") or ""),
            session_id=str(data.get("session_id") or ""),
        )
    return acct


def _load_all_unlocked() -> dict[str, QQAccountState]:
    """读整份状态文件（调用方必须持锁）。旧扁平格式映射到键 ``""``。"""
    data = load_json_or_backup(state_path(), {})
    if not isinstance(data, dict) or not data:
        return {}
    accounts_raw = data.get("accounts")
    if isinstance(accounts_raw, dict):
        return {
            str(k): QQAccountState.from_dict(v)
            for k, v in accounts_raw.items()
            if isinstance(v, dict)
        }
    # 旧扁平格式：内容属于「当时唯一的那个账号」，先挂在空键上，
    # 由 load_account_state 兜底交给来问的账号。
    return {"": _legacy_account_state(data)}


def load_account_state(account_id: str) -> QQAccountState:
    """读取一个账号的状态切片。

    兼容：文件还是旧扁平格式时，内容交给**任何**来问的账号——旧文件只
    会在单账号时期存在，迁移（core.migration）随启动把它改写成新形态。
    """
    with _STATE_LOCK:
        all_state = _load_all_unlocked()
    if account_id in all_state:
        return all_state[account_id]
    legacy = all_state.get("")
    if legacy is not None and not legacy.is_empty():
        return legacy
    return QQAccountState()


def save_account_state(account_id: str, account: QQAccountState) -> bool:
    """写回一个账号的状态切片（读-改-写整份文件，持锁）。

    旧扁平内容（键 ``""``）在第一次保存时消失——它已在兼容读时并入具体
    账号的切片。
    """
    try:
        with _STATE_LOCK:
            all_state = _load_all_unlocked()
            all_state.pop("", None)
            all_state[account_id] = account
            payload = {"accounts": {k: v.to_dict() for k, v in all_state.items()}}
            path = state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, payload)
            return True
    except OSError as e:
        logger.warning(f"[qq] 保存状态失败: {e}")
        return False


# ─── 当前工作区/会话的解析与兜底 ──────────────────────────────────────


def latest_session_id(workspace_uuid: str) -> str:
    """该工作区里最近更新的会话 id（无会话返回 ""）。"""
    from core.config import get_workspace_data_dir
    from core.session import SessionStore

    sessions_dir = get_workspace_data_dir(workspace_uuid) / "sessions"
    sessions = SessionStore.list_sessions(sessions_dir)   # 已按 updated_at 倒序
    if not sessions:
        return ""
    return str(sessions[0].get("session_id") or "")


def _session_exists(workspace_uuid: str, session_id: str) -> bool:
    """绑定的会话是否仍存在（可能已被 Web 删除）。"""
    if not workspace_uuid or not session_id:
        return False
    from core.config import get_workspace_data_dir

    return (get_workspace_data_dir(workspace_uuid) / "sessions" / session_id / "index.json").exists()


def _create_session(workspace_uuid: str) -> str:
    """工作区一个会话都没有时自动新建（§3.6），返回新会话 id。

    新建即落盘（SessionStore.create_new_session 内部 force 写三件套），
    所以即使调用方不保存状态，下一条消息也能经 ``latest_session_id``
    找到同一个会话，不会重复创建。
    """
    from core.config import get_workspace_data_dir
    from core.session import SessionStore

    sessions_dir = get_workspace_data_dir(workspace_uuid) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore.create_new_session(sessions_dir, "QQ 会话")
    logger.info(f"[qq/state] 工作区无会话，已自动创建：{workspace_uuid} → {store.session_id}")
    return store.session_id


def resolve_current(account: QQAccountState, chat_id: str, cfg) -> QQChatState:
    """补全某个聊天的当前工作区/会话绑定，返回该聊天的切片（§3.5）。

    结果写回 ``account``（不落盘——由调用方决定何时 save）。工作区解析
    顺序：锁定 → 强制默认工作空间；未锁定 → 聊天已绑定的；缺失/失效 →
    账号默认；再缺失 → Default 工作区。会话：已绑定的（仍存在）→ 该工作
    区最近的；**不新建**——自动建只在「真的要跑回合」时发生
    （:func:`ensure_session`，§3.6），否则一条 `/h` 也会凭空造出会话。

    ``cfg`` 只用到 ``workspace_locked`` 与 ``default_workspace_uuid`` 两个
    属性，QQ / 微信的配置对象都满足。
    """
    from core.config import ensure_default_workspace, find_workspace_entry

    def _valid_ws(uuid: str) -> bool:
        return bool(uuid) and find_workspace_entry(uuid) is not None

    chat = account.chat(chat_id)

    if cfg.workspace_locked:
        # 锁定：强制默认工作空间。配置校验保证非空，这里兜底防悬空（§7.4）。
        if _valid_ws(cfg.default_workspace_uuid):
            target = cfg.default_workspace_uuid
        else:
            target = ensure_default_workspace() or ""
    elif _valid_ws(chat.workspace_uuid):
        target = chat.workspace_uuid
    elif _valid_ws(cfg.default_workspace_uuid):
        target = cfg.default_workspace_uuid
    else:
        target = ensure_default_workspace() or ""

    if chat.workspace_uuid != target:
        chat.workspace_uuid = target
        chat.session_id = ""      # 换了工作区，原会话不再适用

    if not _session_exists(chat.workspace_uuid, chat.session_id):
        chat.session_id = latest_session_id(chat.workspace_uuid)
    return chat


def ensure_session(account: QQAccountState, chat_id: str, cfg) -> QQChatState:
    """在 :func:`resolve_current` 之上补齐会话绑定：没有就自动新建（§3.6）。

    只在「真的要跑回合」的入站路径上调用——指令（/h /w /s）只解析不创建，
    ``/new`` 自己会建；否则空工作区里随便一条指令都会留下匿名空会话。
    """
    chat = resolve_current(account, chat_id, cfg)
    if not chat.session_id:
        chat.session_id = _create_session(chat.workspace_uuid)
    return chat
