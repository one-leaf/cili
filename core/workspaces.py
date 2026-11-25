"""工作区索引（workspaces.json）读写、缓存与名称校验。

调用方传入索引路径：config 侧保留同名薄委托，测试对 ``core.config`` 的
路径重定向仍然生效。

存储层负责路径编码：``directory`` 项目内写相对路径，项目外写绝对路径；
读出的条目统一解析为绝对路径，调用方只看到绝对路径（见 core.paths）。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path

from core.fs_utils import atomic_write_json
from core.paths import resolve_storage_path, to_storage_path

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


def _root_key(root: Path | str | None) -> str:
    if root is None:
        return ""
    return os.path.normcase(os.path.abspath(str(root)))


def _hydrate_entries(entries: list[dict], root: Path | str | None) -> list[dict]:
    """把条目中的 directory 解析为运行时绝对路径（原地修改并返回）。"""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        directory = entry.get("directory")
        if not isinstance(directory, str) or not directory:
            continue
        try:
            entry["directory"] = str(resolve_storage_path(directory, root=root))
        except ValueError as e:
            logger.warning(f"[workspaces] 无法解析工作区目录 {directory!r}: {e}")
    return entries


def _encode_entries(entries: list[dict], root: Path | str | None) -> list[dict]:
    """把条目中的绝对 directory 编码为持久化值（原地修改并返回）。"""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        directory = entry.get("directory")
        if not isinstance(directory, str) or not directory:
            continue
        entry["directory"] = to_storage_path(directory, root=root)
    return entries


def load_index(path: Path, *, root: Path | str | None = None) -> list[dict]:
    """读取索引（mtime+size 缓存；返回深拷贝，调用方可就地把修改）。

    ``root`` 是相对路径的解析根（通常是 PROJECT_ROOT）；None 时由
    core.paths 延迟取当前项目根。返回值中的 directory 一律是绝对路径。
    """
    global _WORKSPACES_CACHE, _WORKSPACES_SIG
    path_key = str(path)
    root_key = _root_key(root)
    try:
        st = path.stat()
        sig = (path_key, root_key, st.st_mtime_ns, st.st_size)
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
        _hydrate_entries(data, root)
        _WORKSPACES_CACHE = data
        _WORKSPACES_SIG = sig
        return copy.deepcopy(data)


def save_index(path: Path, workspaces: list[dict], *,
               root: Path | str | None = None) -> bool:
    """原子写入索引并刷新缓存（项目内目录编码为相对路径）。"""
    global _WORKSPACES_CACHE, _WORKSPACES_SIG
    with _WORKSPACES_LOCK:
        try:
            stored = _encode_entries(copy.deepcopy(workspaces), root)
            atomic_write_json(path, stored)
            _WORKSPACES_CACHE = _hydrate_entries(copy.deepcopy(workspaces), root)
            try:
                st = path.stat()
                _WORKSPACES_SIG = (str(path), _root_key(root), st.st_mtime_ns, st.st_size)
            except OSError:
                _WORKSPACES_SIG = None
            return True
        except OSError as e:
            logger.warning(f"Failed to save {path}: {e}")
            return False


def find_entry(path: Path, workspace_uuid: str, *,
               root: Path | str | None = None) -> dict | None:
    for entry in load_index(path, root=root):
        if entry.get("uuid") == workspace_uuid:
            return entry
    return None


def upsert_entry(path: Path, entry: dict, *,
                 root: Path | str | None = None) -> bool:
    with _WORKSPACES_LOCK:
        workspaces = load_index(path, root=root)
        uuid = entry.get("uuid")
        for i, e in enumerate(workspaces):
            if e.get("uuid") == uuid:
                workspaces[i] = entry
                break
        else:
            workspaces.append(entry)
        return save_index(path, workspaces, root=root)


def remove_entry(path: Path, workspace_uuid: str, *,
                 root: Path | str | None = None) -> bool:
    with _WORKSPACES_LOCK:
        workspaces = load_index(path, root=root)
        filtered = [e for e in workspaces if e.get("uuid") != workspace_uuid]
        if len(filtered) == len(workspaces):
            return True  # not found
        return save_index(path, filtered, root=root)
