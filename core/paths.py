"""持久化路径的存储编码：项目内相对、项目外绝对。

目标：整个 cili 目录改名或搬到其他位置后，``workspaces.json`` 与
``setting.json`` 里的工作区路径仍然有效。运行时仍统一使用绝对路径，
相对编码只发生在写盘/读盘的边界。

只处理「持久化到磁盘」的路径；临时路径、上传路径等运行时值不经过这里。
"""

from __future__ import annotations

import os
from pathlib import Path

from core.fs_utils import is_path_within


def _project_root() -> Path:
    """延迟导入，避免 core.config 顶层导入本模块时形成循环。"""
    from core.config import PROJECT_ROOT

    return Path(PROJECT_ROOT)


def _root_path(root: str | Path | None) -> Path:
    value = root if root is not None else _project_root()
    return Path(os.path.abspath(str(value)))


def to_storage_path(path: str | Path, *, root: str | Path | None = None) -> str:
    """把运行时路径编码为持久化值：项目内相对（POSIX 分隔符），项目外绝对。"""
    raw = str(path)
    value = Path(os.path.expanduser(raw))
    root_path = _root_path(root)

    if not value.is_absolute():
        # Windows 盘符相对路径（D:foo）与根相对路径（\foo）都不能安全持久化
        if value.drive or raw.startswith(("/", "\\")):
            raise ValueError(f"非法的相对路径: {raw!r}")
        return value.as_posix()

    abs_path = Path(os.path.abspath(raw))
    if is_path_within(str(abs_path), str(root_path)):
        rel = os.path.relpath(str(abs_path), str(root_path))
        return Path(rel).as_posix()
    return str(abs_path)


def resolve_storage_path(value: str | Path, *, root: str | Path | None = None) -> Path:
    """把持久化值解析为运行时绝对路径；相对值按当前项目根解析。

    相对值越出项目目录、或使用盘符相对形式时抛 ``ValueError``。
    """
    raw = str(value)
    path = Path(os.path.expanduser(raw))
    root_path = _root_path(root)

    if path.is_absolute():
        return path
    if path.drive or raw.startswith(("/", "\\")):
        raise ValueError(f"非法的相对路径: {raw!r}")

    resolved = Path(os.path.abspath(str(root_path / path)))
    if not is_path_within(str(resolved), str(root_path)):
        raise ValueError(f"相对路径越出项目目录: {raw!r}")
    return resolved