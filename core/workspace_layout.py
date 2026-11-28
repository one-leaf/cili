"""工作区目录布局：共享内容之外的 runtime / cache / config / tmp 统一入口。

所有路径都相对于 ``{workspace}/.cili/``；调用方不要再手写目录名。
"""

from __future__ import annotations

import re
from pathlib import Path

from core.config import get_workspace_data_dir

_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _segment(value: str) -> str:
    return _SEGMENT_RE.sub("_", str(value or "")).strip("._") or "unknown"


def workspace_runtime_dir(workspace_uuid: str) -> Path:
    return get_workspace_data_dir(workspace_uuid) / "runtime"


def workspace_cache_dir(workspace_uuid: str) -> Path:
    return get_workspace_data_dir(workspace_uuid) / "cache"


def workspace_config_dir(workspace_uuid: str) -> Path:
    return get_workspace_data_dir(workspace_uuid) / "config"


def workspace_tmp_dir(workspace_uuid: str) -> Path:
    return get_workspace_data_dir(workspace_uuid) / "tmp"


def memory_session_state_dir(workspace_uuid: str, session_id: str) -> Path:
    return workspace_runtime_dir(workspace_uuid) / "memory" / "sessions" / _segment(session_id)


def todo_dir(workspace_uuid: str) -> Path:
    return workspace_runtime_dir(workspace_uuid) / "todo"


def tool_output_dir(workspace_uuid: str, key: str) -> Path:
    return workspace_cache_dir(workspace_uuid) / "tool_output" / _segment(key)
