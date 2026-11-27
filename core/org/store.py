"""组织会话与本地状态文件。

- ``session.json``：服务器地址、设备令牌、用户信息（令牌只在本地）
- ``state.json``：provider/角色快照/同步状态（不含令牌）
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from core.fs_utils import atomic_write_json, load_json_or_backup

logger = logging.getLogger(__name__)

SESSION_FILE = "session.json"
STATE_FILE = "state.json"


def org_dir() -> Path:
    from core.config import DATA_DIR

    return Path(DATA_DIR) / "org"


def _ensure_dir() -> Path:
    path = org_dir()
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _load(name: str) -> dict:
    return load_json_or_backup(org_dir() / name, {})


def _save(name: str, data: dict) -> None:
    _ensure_dir()
    path = org_dir() / name
    atomic_write_json(path, data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_session() -> dict:
    data = _load(SESSION_FILE)
    return data if isinstance(data, dict) else {}


def save_session(data: dict) -> None:
    _save(SESSION_FILE, dict(data))


def clear_session() -> None:
    directory = org_dir()
    for path in [directory / SESSION_FILE, *directory.glob(SESSION_FILE + ".corrupt-*")]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("[org] 删除 %s 失败", path.name, exc_info=True)


def load_state() -> dict:
    data = _load(STATE_FILE)
    return data if isinstance(data, dict) else {}


def save_state(data: dict) -> None:
    _save(STATE_FILE, dict(data))


def clear_state() -> None:
    directory = org_dir()
    for path in [directory / STATE_FILE, *directory.glob(STATE_FILE + ".corrupt-*")]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("[org] 删除 %s 失败", path.name, exc_info=True)


def clear_all() -> None:
    clear_session()
    clear_state()
