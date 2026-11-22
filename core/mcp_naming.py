"""MCP 工具名规范化：模型 API 只接受 [a-zA-Z0-9_-]。"""

from __future__ import annotations

import hashlib
import re

_SANITIZE_RE = re.compile(r"_+")
_MAX_TOOL_NAME_LENGTH = 64
_HASH_LENGTH = 8

def _sanitize_name(name: str) -> str:
    return _SANITIZE_RE.sub("_", re.sub(r"[^a-zA-Z0-9_-]", "_", name))


def _limit_tool_name(name: str, max_length: int = _MAX_TOOL_NAME_LENGTH) -> str:
    """限长工具名：超长时用 sha1 哈希后缀替代，保证唯一。"""
    if len(name) <= max_length:
        return name
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    return f"{name[:max_length - _HASH_LENGTH - 1]}_{digest}"


def _sanitize_mcp_tool_name(name: str) -> str:
    return _limit_tool_name(_sanitize_name(name))
