"""Web API 共享依赖：全局状态 + 跨路由域 helper（deps 中间层防循环导入）。

各 routes_*.py 只从本模块与 core 导入；web_api.py 只 import router 与 deps。
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from core.session_runner import SessionRunner
from core.config import (
    ensure_default_workspace,
    load_config, PROJECT_ROOT, load_workspace_config, save_workspace_config,
    GLOBAL_CONFIG_PATH, get_workspace_data_dir, load_workspaces_index,
    find_workspace_entry,
)
from core.default_sink import bind_default_sink
from core.session_registry import registry

# Configure logging（root handler 由本模块首个配置，web_api 不再重复 basicConfig）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# master runner 池下沉至 core（接口无关），Web/QQ 等接入端共享同一份
_sessions_lock = registry.lock  # 多步操作（workspace 删除/重置、配置重载）持锁

# Base directories
WEB_DIR = Path(__file__).parent.resolve()

# Working directory for default workspace
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
WORKSPACE_DIR.mkdir(exist_ok=True)

# Chrome profile directory（仅建目录副作用，browser 服务自行管理进程）
CHROME_DIR = PROJECT_ROOT / "data" / "deps" / "browser"
CHROME_DIR.mkdir(parents=True, exist_ok=True)


# 通用安全标识符校验（session/exec/tool_use_id/文件名/记忆名共用）：防止路径穿越
_SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9_-]+$')

# Localhost IP addresses for access control
# Only trust client IP, NOT Host header (which can be spoofed)
_LOCALHOST_IPS = {"127.0.0.1", "::1", "localhost"}


def _validate_session_id(session_id: str) -> None:
    """Validate session_id format to prevent path traversal."""
    if not _SAFE_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format")


def _validate_workspace_uuid(workspace_uuid: str) -> None:
    """Validate workspace_uuid format to prevent path traversal."""
    if not _SAFE_ID_RE.match(workspace_uuid):
        raise HTTPException(status_code=400, detail="Invalid workspace_uuid format")


def _validate_exec_id(exec_id: str) -> None:
    """Validate exec_id format to prevent path traversal."""
    if not _SAFE_ID_RE.match(exec_id):
        raise HTTPException(status_code=400, detail="Invalid exec_id format")


def _csrf_protect(request: Request) -> None:
    """CSRF 防护（W6）：跨站浏览器请求带 Origin/Referer，非本机来源则拒绝。

    multipart/form-data 与简单 POST 不触发 CORS 预检，恶意网页可向
    localhost 端点自动提交；本依赖校验浏览器来源头，curl 等无头客户端放行。
    注意：token 鉴权由 check_access_control 中间件统一负责，此处只防 CSRF。
    """
    origin = request.headers.get("origin") or ""
    referer = request.headers.get("referer") or ""
    if not origin and not referer:
        return  # 非浏览器客户端（curl 等），放行
    for value in (origin, referer):
        if not value:
            continue
        try:
            host = urlparse(value).hostname or ""
        except ValueError:
            host = ""
        if host not in ("localhost", "127.0.0.1"):
            raise HTTPException(status_code=403, detail="Cross-site request blocked (CSRF)")


def _require_workspace(workspace_uuid: str) -> Path:
    """FastAPI dependency: validate workspace exists, return its .cili data dir.

    Usage: ws_dir: Path = Depends(_require_workspace)
    """
    if not _SAFE_ID_RE.match(workspace_uuid):
        raise HTTPException(status_code=400, detail="Invalid workspace_uuid format")
    if not find_workspace_entry(workspace_uuid):
        raise HTTPException(status_code=404, detail="Workspace not found")
    ws_dir = get_workspace_data_dir(workspace_uuid)
    ws_dir.mkdir(parents=True, exist_ok=True)
    return ws_dir


def _new_short_id() -> str:
    """8 位十六进制短 ID（32bit 熵），用于标识符命名空间防碰撞。"""
    return secrets.token_hex(4)


def _ensure_default_workspace() -> str | None:
    """确保默认工作区存在（实现已下沉 ``core.config.ensure_default_workspace``）。

    QQ 接入端启动时也要做同一件事，故下沉共用；此处保留同名薄封装。
    """
    return ensure_default_workspace()


def _auto_init_global_config() -> None:
    """Verify global model config is available on web server startup.

    Migration is handled by main.py _init_settings() before
    this point. This function only logs the result for direct uvicorn usage.
    """
    if not GLOBAL_CONFIG_PATH.exists():
        logger.warning(f"No config file at {GLOBAL_CONFIG_PATH}. "
                       "Run 'python main.py' to set up, or configure via web UI.")
        return

    try:
        config = load_config()
        logger.info(f"Global config loaded: model={config.model.name}, interface={config.model.interface_type}")
    except RuntimeError as e:
        logger.warning(f"Config not available: {e}")
    except Exception as e:
        logger.warning(f"Config error: {e}")


def init_web_deps() -> None:
    """Web 接入端的显式初始化：确保默认工作区存在 + 全局配置自检。

    此前在模块导入时直接执行，任何 ``import web.deps``（含测试收集、只读工具）
    都会产生建目录/读配置的副作用。改为由入口显式调用：
    - ``main.py`` 启动 Web 接口前
    - ``web_api.py`` 的 lifespan（直接跑 uvicorn / 直接执行本模块的路径）

    幂等，可重复调用。
    """
    _ensure_default_workspace()
    _auto_init_global_config()


def _get_workspace_info(workspace_uuid: str) -> dict | None:
    """Read workspace info from setting.json. Returns None if not found."""
    config = load_workspace_config(workspace_uuid)
    return config or None


def _list_all_workspaces() -> list[dict]:
    """Read all workspaces from workspaces.json index."""
    workspaces = []
    for entry in load_workspaces_index():
        uuid = entry.get("uuid", "")
        workspaces.append({
            "uuid": uuid,
            "name": entry.get("workspace_name", uuid),
            "directory": entry.get("directory", ""),
            "created_at": entry.get("created_at", ""),
            "system": entry.get("system", False),
        })
    return workspaces


def _bind_default_sink(runner: SessionRunner, workspace_uuid: str, session_id: str) -> None:
    """绑定 runner 的持久输出接收端（发布到全局事件总线）。

    实现已下沉 ``core/default_sink.py``——它本就是接口无关的（只往 core 的
    EventBus 发事件），第二个接入端 QQ 也用同一个，从而 QQ 驱动的回合在 Web
    的 /api/events 全局流里同样可见。此处保留同名薄封装，兼容既有调用方。
    """
    bind_default_sink(runner, workspace_uuid, session_id)


async def _get_or_create_runner(workspace_uuid: str, session_id: str) -> SessionRunner:
    """Get or create a runner for a given workspace and session.

    池与生命周期由 core.session_registry 统一管理（接口无关）。
    """
    info = _get_workspace_info(workspace_uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_dir = info.get("directory", "")
    if not workspace_dir or not Path(workspace_dir).exists():
        raise HTTPException(status_code=404, detail="Workspace directory not found")

    try:
        return await registry.get_or_create(
            workspace_uuid, session_id, workspace_dir,
            on_create=lambda runner: _bind_default_sink(runner, workspace_uuid, session_id),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"Failed to load config: {e}")


# ---------- 会话运行认领（防 send_message/answer_ask_user 双循环 TOCTOU） ----------
# 认领状态随 runner 池一起下沉至 core.session_registry（接口无关）。


def _claim_session_run(key: str) -> bool:
    """原子认领会话执行权，返回是否认领成功。"""
    return registry.claim(key)


def _release_session_run(key: str) -> None:
    """释放会话执行权认领。"""
    registry.release(key)


def _is_session_idle(key: str) -> bool:
    """检查会话是否空闲（没有被认领且 runner 没有运行）。"""
    return registry.is_idle(key)


# SSE 适配（回调组 / 事件流 / 执行壳）已移至 web/sse.py —— 与 core 解耦。
