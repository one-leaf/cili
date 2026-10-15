"""TUI 客户端配置：服务端发现与鉴权（方案 §三的解析链）。

客户端不依赖 cwd（cwd 是用户的 workspace），项目根由 ``__file__`` 自定位——
服务端的 ``data/cili/server.json`` / ``setting.json`` 都在安装目录下，同机场景
直接读；``~/.cili/config.json`` 与环境变量用于覆盖（远程服务端 / 脚本场景）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# cli.py → interfaces/tui/ → interfaces/ → 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_INFO_PATH = PROJECT_ROOT / "data" / "cili" / "server.json"
SETTING_PATH = PROJECT_ROOT / "data" / "cili" / "setting.json"
USER_CONFIG_PATH = Path.home() / ".cili" / "config.json"

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


@dataclass
class ClientConfig:
    base_url: str
    token: str = ""


def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _setting_token() -> str:
    """服务端 setting.json 的 system.access_token（同机场景直接读）。"""
    system = _read_json(SETTING_PATH).get("system", {})
    token = system.get("access_token", "") if isinstance(system, dict) else ""
    return token if isinstance(token, str) else ""


def discover_config(server_override: str | None = None) -> ClientConfig:
    """服务端地址与 token 的解析链（优先级从高到低）：

    地址：--server 参数 > 环境变量 CILI_SERVER > ~/.cili/config.json >
          data/cili/server.json（服务端启动落盘，兼容自定义 --port）> 默认端口
    token：环境变量 CILI_TOKEN > ~/.cili/config.json > 服务端 setting.json
    """
    user_cfg = _read_json(USER_CONFIG_PATH)

    if server_override:
        base_url = server_override
    elif os.environ.get("CILI_SERVER"):
        base_url = os.environ["CILI_SERVER"]
    elif user_cfg.get("server"):
        base_url = str(user_cfg["server"])
    else:
        info = _read_json(SERVER_INFO_PATH)
        port = info.get("port")
        host = info.get("host") or "127.0.0.1"
        base_url = f"http://{host}:{port}" if port else DEFAULT_BASE_URL

    base_url = base_url.rstrip("/")

    token = (
        os.environ.get("CILI_TOKEN")
        or (str(user_cfg["token"]) if user_cfg.get("token") else "")
        or _setting_token()
    )
    return ClientConfig(base_url=base_url, token=token)
