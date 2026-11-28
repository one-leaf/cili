"""本机节点身份：稳定 node id + 可读设备名。

节点 id 用于 cron 归属、课件记录分文件和上下文标识；它不是凭证，
不包含用户名、密钥或路径。首次启动生成 ``data/cili/node.json``，
之后每次启动复用。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

from core.fs_utils import atomic_write_json

NODE_FILE_NAME = "node.json"
NODE_ID_PREFIX = "node-"
NODE_ID_RE = re.compile(r"^node-[0-9a-f]{12}$")
ENV_NODE_ID = "CILI_NODE_ID"
ENV_NODE_NAME = "CILI_NODE_NAME"
_NAME_MAX = 120
_VERSION = 1


def node_path() -> Path:
    """节点文件位置；延迟导入 config，避免循环依赖。"""
    from core.config import DATA_DIR

    return Path(DATA_DIR) / NODE_FILE_NAME


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_name(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    return text[:_NAME_MAX]


def _default_name() -> str:
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    return _clean_name(host) or "cili"


def _new_id() -> str:
    return NODE_ID_PREFIX + secrets.token_hex(6)


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _publish(node_id: str, name: str) -> None:
    os.environ[ENV_NODE_ID] = node_id
    os.environ[ENV_NODE_NAME] = name


def ensure_node_identity() -> dict[str, str]:
    """读取或生成节点身份，并写入环境变量；幂等。"""
    path = node_path()
    data = _read(path)
    node_id = str(data.get("id") or "").strip()
    name = _clean_name(data.get("name"))
    changed = False

    if not NODE_ID_RE.fullmatch(node_id):
        node_id = _new_id()
        changed = True
    if not name:
        name = _default_name()
        changed = True

    created_at = str(data.get("created_at") or "").strip() or _now()
    updated_at = str(data.get("updated_at") or "").strip() or created_at
    if changed:
        updated_at = _now()
        atomic_write_json(
            path,
            {
                "version": _VERSION,
                "id": node_id,
                "name": name,
                "created_at": created_at,
                "updated_at": updated_at,
            },
        )
    _publish(node_id, name)
    return {"id": node_id, "name": name}


def get_node_identity() -> dict[str, str]:
    return ensure_node_identity()


def get_node_id() -> str:
    return ensure_node_identity()["id"]


def get_node_name() -> str:
    return ensure_node_identity()["name"]


def update_node_name(name: str) -> dict[str, str]:
    """修改设备名；id 不变。空值回退主机名。"""
    identity = ensure_node_identity()
    clean = _clean_name(name) or _default_name()
    path = node_path()
    data = _read(path)
    data.update(
        {
            "version": _VERSION,
            "id": identity["id"],
            "name": clean,
            "created_at": str(data.get("created_at") or _now()),
            "updated_at": _now(),
        }
    )
    atomic_write_json(path, data)
    _publish(identity["id"], clean)
    return {"id": identity["id"], "name": clean}


def regenerate_node_identity() -> dict[str, str]:
    """生成新的 node id，保留设备名；用于恢复/复制安装目录后人工重建。"""
    identity = ensure_node_identity()
    node_id = _new_id()
    path = node_path()
    data = _read(path)
    data.update(
        {
            "version": _VERSION,
            "id": node_id,
            "name": identity["name"],
            "created_at": _now(),
            "updated_at": _now(),
        }
    )
    atomic_write_json(path, data)
    _publish(node_id, identity["name"])
    return {"id": node_id, "name": identity["name"]}
