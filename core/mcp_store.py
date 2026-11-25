"""工作区 MCP 配置存储：``{workspace}/.cili/mcp/{server}.json``。

配置随工作区目录存放，工作区复制/迁移时 MCP 配置一并带走；每台服务器
一个文件，按文件动态发现（文件名即服务器名）。读取失败的文件只告警、
不阻断，避免单个坏配置拖垮会话。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from core.config import MCPConfig, get_workspace_data_dir
from core.fs_utils import atomic_write_json

logger = logging.getLogger(__name__)

MCP_DIRNAME = "mcp"

# 服务器名同时是文件名：限定安全字符（不允许路径分隔符 / 前导点）
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

_SAVE_LOCK = threading.Lock()


def validate_server_name(name: str) -> str | None:
    """校验服务器名；合法返回 None，否则返回错误信息。"""
    if not name:
        return "服务器名称不能为空"
    if len(name) > 64:
        return "服务器名称不能超过 64 个字符"
    if not _SERVER_NAME_RE.match(name):
        return f"服务器名称 {name!r} 含非法字符（只允许字母、数字、_ . -，且不能以符号开头）"
    return None


def mcp_dir(workspace_uuid: str) -> Path:
    """工作区 MCP 配置目录：``{workspace}/.cili/mcp/``。"""
    return get_workspace_data_dir(workspace_uuid) / MCP_DIRNAME


def load_mcp_servers(workspace_uuid: str) -> dict[str, MCPConfig]:
    """扫描并解析工作区的 MCP 服务器配置（文件名 → server 名）。"""
    directory = mcp_dir(workspace_uuid)
    if not directory.is_dir():
        return {}
    servers: dict[str, MCPConfig] = {}
    for path in sorted(directory.glob("*.json")):
        name = path.stem
        error = validate_server_name(name)
        if error:
            logger.warning(f"[MCP] 跳过无效配置文件 {path.name}: {error}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"[MCP] 跳过无法读取的配置 {path.name}: {e}")
            continue
        if not isinstance(data, dict):
            logger.warning(f"[MCP] 跳过格式错误的配置 {path.name}（应为 JSON 对象）")
            continue
        servers[name] = MCPConfig.from_dict(data)
    return servers


def save_mcp_servers(workspace_uuid: str, servers: dict[str, MCPConfig]) -> None:
    """全量写入工作区 MCP 配置：逐服务器写文件，删除已移除的服务器文件。"""
    with _SAVE_LOCK:
        directory = mcp_dir(workspace_uuid)
        directory.mkdir(parents=True, exist_ok=True)
        keep: set[str] = set()
        for name, cfg in servers.items():
            error = validate_server_name(name)
            if error:
                raise ValueError(error)
            filename = f"{name}.json"
            atomic_write_json(directory / filename, cfg.to_dict())
            keep.add(filename)
        for path in directory.glob("*.json"):
            if path.name not in keep:
                try:
                    path.unlink()
                except OSError as e:
                    logger.warning(f"[MCP] 删除已移除的配置 {path.name} 失败: {e}")
