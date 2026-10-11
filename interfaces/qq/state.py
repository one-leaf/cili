"""QQ 接入端的持久化状态：owner 绑定 + 当前工作区/会话。

**单用户模型**：一个 Cili 实例只有一个 QQ 接口、一个 owner，所以这里是全局
一份状态（不按 chat 分键）。

存放位置 ``data/cili/tools/qq/state.json``——**系统级**而非 per-workspace，
因为「当前工作区」本身就是这份状态的一部分。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from core.config import DATA_DIR
from core.fs_utils import atomic_write_json, load_json_or_backup

logger = logging.getLogger(__name__)


def state_path() -> Path:
    """状态文件路径（惰性求值，便于测试 monkeypatch ``DATA_DIR``）。"""
    return DATA_DIR / "tools" / "qq" / "state.json"


@dataclass
class QQState:
    """owner + 当前工作区/会话。"""

    owner_openid: str = ""
    workspace_uuid: str = ""
    session_id: str = ""

    @classmethod
    def from_dict(cls, data: dict | None) -> "QQState":
        if not isinstance(data, dict):
            return cls()
        return cls(
            owner_openid=str(data.get("owner_openid") or ""),
            workspace_uuid=str(data.get("workspace_uuid") or ""),
            session_id=str(data.get("session_id") or ""),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def load_state() -> QQState:
    """读取状态（文件缺失/损坏时返回空状态）。"""
    return QQState.from_dict(load_json_or_backup(state_path(), {}))


def save_state(state: QQState) -> bool:
    """原子写入状态。"""
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, state.to_dict())
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


def resolve_current(state: QQState) -> QQState:
    """补全「当前工作区/会话」：缺工作区则用默认工作区，缺会话则用其最近会话。

    结果写回 ``state``（但不落盘——由调用方决定何时 save）。
    """
    from core.config import ensure_default_workspace, find_workspace_entry

    if state.workspace_uuid and find_workspace_entry(state.workspace_uuid):
        uuid = state.workspace_uuid
    else:
        uuid = ensure_default_workspace() or ""
        state.workspace_uuid = uuid
        state.session_id = ""      # 换了工作区，原会话不再适用

    if uuid and not state.session_id:
        state.session_id = latest_session_id(uuid)

    return state
