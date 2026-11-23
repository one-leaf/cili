"""工作区索引（workspaces.json）读写、缓存与名称校验。

调用方传入索引路径：config 侧保留同名薄委托，测试对 ``core.config`` 的
路径重定向仍然生效。
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path

from core.fs_utils import atomic_write_json

logger = logging.getLogger(__name__)

_WORKSPACES_CACHE: list[dict] = []
_WORKSPACES_SIG: tuple | None = None
_WORKSPACES_LOCK = threading.RLock()


def validate_workspace_name(name: str) -> str | None:
    """校验工作区显示名；返回错误信息，合法返回 None。"""
    if not name:
        return "Workspace name is required"
    if len(name) > 50:
        return "Workspace name must be at most 50 characters"
    if any(c in name for c in ('/', '\\', '\x00', '\n', '\r')):
        return "Workspace name contains invalid characters"
    return None


def load_index(path: Path) -> list[dict]:
    """读取索引（mtime+size 缓存；返回深拷贝，调用方可就地把修改）。"""
    global _WORKSPACES_CACHE, _WORKSPACES_SIG
    path_key = str(path)
    try:
        st = path.stat()
        sig = (path_key, st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    with _WORKSPACES_LOCK:
        if sig is not None and sig == _WORKSPACES_SIG:
            return copy.deepcopy(_WORKSPACES_CACHE)
        data: list[dict] = []
        if sig is not None:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    data = loaded
                else:
                    logger.warning(f"Invalid workspaces.json format (expected list): {path}")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to read {path}: {e}")
        _WORKSPACES_CACHE = data
        _WORKSPACES_SIG = sig
        return copy.deepcopy(data)


def save_index(path: Path, workspaces: list[dict]) -> bool:
    """原子写入索引并刷新缓存。"""
    global _WORKSPACES_CACHE, _WORKSPACES_SIG
    with _WORKSPACES_LOCK:
        try:
            atomic_write_json(path, workspaces)
            _WORKSPACES_CACHE = copy.deepcopy(workspaces)
            try:
                st = path.stat()
                _WORKSPACES_SIG = (str(path), st.st_mtime_ns, st.st_size)
            except OSError:
                _WORKSPACES_SIG = None
            return True
        except OSError as e:
            logger.warning(f"Failed to save {path}: {e}")
            return False


def find_entry(path: Path, workspace_uuid: str) -> dict | None:
    for entry in load_index(path):
        if entry.get("uuid") == workspace_uuid:
            return entry
    return None


def upsert_entry(path: Path, entry: dict) -> bool:
    with _WORKSPACES_LOCK:
        workspaces = load_index(path)
        uuid = entry.get("uuid")
        for i, e in enumerate(workspaces):
            if e.get("uuid") == uuid:
                workspaces[i] = entry
                break
        else:
            workspaces.append(entry)
        return save_index(path, workspaces)


def remove_entry(path: Path, workspace_uuid: str) -> bool:
    with _WORKSPACES_LOCK:
        workspaces = load_index(path)
        filtered = [e for e in workspaces if e.get("uuid") != workspace_uuid]
        if len(filtered) == len(workspaces):
            return True  # not found
        return save_index(path, filtered)
