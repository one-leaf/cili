"""Web server for Cili - provides HTTP API and serves static files."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import shutil
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Form, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.config import (
    load_config, Config, PROJECT_ROOT, DATA_DIR,
    validate_workspace_name, get_workspace_data_dir,
    load_workspace_config, save_workspace_config,
    GLOBAL_CONFIG_PATH, load_global_config, save_global_config,
)
from core.root_agent import RootAgent
from core.session import SessionManager
from core.message_bus import get_message_bus

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global agents dict: session_id -> RootAgent
agents: dict[str, RootAgent] = {}
# LRU tracking: key -> last access timestamp
_agent_access: dict[str, float] = {}
_MAX_AGENTS = 20  # Maximum number of agents to keep in memory
# Lock for concurrent access to agents dict
_agents_lock = asyncio.Lock()


def _evict_idle_agent() -> None:
    """Evict the oldest non-running agent if agent count exceeds limit."""
    if len(agents) <= _MAX_AGENTS:
        return
    # Find the oldest non-running agent
    idle_keys = [
        k for k in agents
        if not agents[k].is_running()
    ]
    if not idle_keys:
        return
    oldest = min(idle_keys, key=lambda k: _agent_access.get(k, 0))
    logger.info(f"[RootAgent LRU] 淘汰闲置 RootAgent: {oldest}")
    evicted = agents.pop(oldest)
    _agent_access.pop(oldest, None)
    try:
        evicted.cleanup()
    except Exception as e:
        logger.warning(f"[RootAgent LRU] 清理被淘汰的 RootAgent 失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 - 优雅关闭时清理资源"""
    # 确保浏览器服务实例已创建（Playwright 延迟到首次操作时启动）
    from core.browser_service import get_service
    get_service()
    # 初始化 MessageBus
    from core.message_bus import start_message_bus
    start_message_bus()
    yield
    # 关闭时停止 MessageBus
    try:
        from core.message_bus import stop_message_bus
        stop_message_bus()
    except Exception as e:
        logger.warning(f"[Server] 停止 MessageBus 失败: {e}")
    # 关闭时停止浏览器服务
    try:
        from core.browser_service import stop_browser_service
        stop_browser_service()
    except Exception as e:
        logger.warning(f"[Server] 停止浏览器服务失败: {e}")
    # 关闭时停止 cron 调度器
    try:
        from core.cron import stop_scheduler
        stop_scheduler()
    except Exception as e:
        logger.warning(f"[Server] 停止 cron 调度器失败: {e}")
    # 关闭时清理所有 RootAgent 资源
    logger.info(f"[Server] 正在关闭，清理 {len(agents)} 个 RootAgent...")
    for key, agent in list(agents.items()):
        try:
            agent.stop()
            agent.cleanup()
        except Exception as e:
            logger.warning(f"[Server] 清理 RootAgent {key} 失败: {e}")
    agents.clear()
    logger.info("[Server] 资源清理完成")


# Initialize FastAPI app with lifespan
app = FastAPI(title="Cili Web API", version="1.0.0", lifespan=lifespan)

# Add CORS middleware - restrict to localhost by default for security
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Localhost IP addresses for access control
# Only trust client IP, NOT Host header (which can be spoofed)
_LOCALHOST_IPS = {"127.0.0.1", "::1", "localhost"}


@app.middleware("http")
async def check_access_control(request: Request, call_next):
    """Middleware to enforce IP-based access control.

    Access is granted if:
    - Client IP is localhost (always allowed)
    - Client IP is in the allowed_ips whitelist

    Only trusts request.client.host (the real TCP connection IP).
    Host header is NOT checked because it can be spoofed by attackers.
    """
    client_ip = request.client.host if request.client else ""

    try:
        config = load_config()
    except Exception:
        # Config not ready (e.g. no API key yet) - allow localhost only
        # so user can open UI to configure settings
        if client_ip in _LOCALHOST_IPS:
            return await call_next(request)
        logger.warning(f"Config not ready, blocked access from {client_ip}")
        return JSONResponse(
            status_code=403,
            content={"detail": "Server not configured yet"}
        )

    # Always allow localhost
    if client_ip in _LOCALHOST_IPS:
        return await call_next(request)

    # Check whitelist
    allowed_ips = config.system.allowed_ips or []
    if client_ip in allowed_ips:
        return await call_next(request)

    # Blocked
    logger.warning(f"Blocked access from {client_ip}")
    return JSONResponse(
        status_code=403,
        content={"detail": "Access denied: IP not allowed"}
    )

# Base directories (imported from core.config for consistency)
WEB_DIR = Path(__file__).parent.resolve()
WORKSPACE_DATA_DIR = PROJECT_ROOT / "data" / "agents"
WORKSPACE_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Working directory for default workspace
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
WORKSPACE_DIR.mkdir(exist_ok=True)

# Chrome profile directory
CHROME_DIR = PROJECT_ROOT / "data" / "deps" / "browser"
CHROME_DIR.mkdir(parents=True, exist_ok=True)


def _require_workspace(workspace_uuid: str) -> Path:
    """FastAPI dependency: validate workspace exists, return its data dir.

    Usage: ws_dir: Path = Depends(_require_workspace)
    """
    ws_dir = WORKSPACE_DATA_DIR / workspace_uuid
    if not ws_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws_dir


def _ensure_default_workspace() -> str | None:
    """Ensure default workspace exists, create if not. Returns workspace UUID or None on failure."""
    try:
        # Check if any workspace exists with name "Default" or "default" (legacy)
        for item in WORKSPACE_DATA_DIR.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                config = load_workspace_config(item.name)
                if config and config.get("workspace_name") in ("Default", "default"):
                    # Migrate legacy "default" to "Default"
                    if config.get("workspace_name") == "default":
                        config["workspace_name"] = "Default"
                        save_workspace_config(item.name, config)
                        logger.info(f"Migrated default workspace name: {item.name}")
                    logger.info(f"Default workspace found: {item.name}")
                    return item.name

        # Create default workspace
        workspace_uuid = secrets.token_hex(4)
        ws_data_dir = WORKSPACE_DATA_DIR / workspace_uuid
        ws_data_dir.mkdir(parents=True, exist_ok=True)
        (ws_data_dir / "sessions").mkdir(exist_ok=True)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws_config = {
            "workspace_name": "Default",
            "directory": str(WORKSPACE_DIR),
            "created_at": now,
            "updated_at": now,
        }

        save_workspace_config(workspace_uuid, ws_config)
        logger.info(f"Created default workspace: {workspace_uuid}")
        return workspace_uuid
    except (OSError, PermissionError) as e:
        logger.error(f"Failed to create default workspace: {e}")
        return None


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


# Initialize default workspace on startup
DEFAULT_WORKSPACE_UUID = _ensure_default_workspace()

# Auto-configure global model config on startup
_auto_init_global_config()


def _get_workspace_info(workspace_uuid: str) -> dict | None:
    """Read workspace info from setting.json. Returns None if not found."""
    config = load_workspace_config(workspace_uuid)
    return config or None


def _list_all_workspaces() -> list[dict]:
    """Scan data/agents/ and return all workspaces."""
    workspaces = []
    if not WORKSPACE_DATA_DIR.exists():
        return workspaces
    for item in WORKSPACE_DATA_DIR.iterdir():
        if item.is_dir() and not item.name.startswith('.'):
            info = _get_workspace_info(item.name)
            if info:
                workspaces.append({
                    "uuid": item.name,
                    "name": info.get("workspace_name", item.name),
                    "directory": info.get("directory", ""),
                    "created_at": info.get("created_at", ""),
                    "system": info.get("system", False),
                })
    return workspaces


async def _get_or_create_agent(workspace_uuid: str, session_id: str) -> RootAgent:
    """Get or create an agent for a given workspace and session.

    Thread-safe: acquires _agents_lock to prevent concurrent creation of
    duplicate agents for the same workspace:session key.
    """
    info = _get_workspace_info(workspace_uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_dir = info.get("directory", "")
    if not workspace_dir or not Path(workspace_dir).exists():
        raise HTTPException(status_code=404, detail="Workspace directory not found")

    key = f"{workspace_uuid}:{session_id}"

    async with _agents_lock:
        _agent_access[key] = time.time()

        if key not in agents:
            _evict_idle_agent()
            try:
                config = load_config()  # 全局配置，不需要 workspace 参数
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to load config: {e}")

            agent = RootAgent(config, cwd=workspace_dir, workspace_uuid=workspace_uuid)

            # Load the requested session if different from default
            if session_id != agent.current_session_id:
                session_dir = agent.sessions_dir / session_id
                index_file = session_dir / "index.json"
                if index_file.exists():
                    # Load existing session
                    agent.switch_session(session_id)
                    logger.info(f"Loaded existing session: {session_id}")
                else:
                    # Session doesn't exist on disk, rename the default session
                    old_session_dir = agent.session_manager.session_dir
                    agent.session_manager.session_id = session_id
                    agent.session_manager.session_dir = agent.session_manager.sessions_dir / session_id
                    agent.current_session_id = session_id
                    agent._session_id = session_id
                    agent.session_dir = session_dir
                    agent.messages = agent.session_manager.messages  # Update reference
                    agent.session_manager.name = f"Session {session_id[:8]}"
                    # Remove the empty old directory to avoid orphan dirs
                    if old_session_dir.exists() and not any(old_session_dir.iterdir()):
                        old_session_dir.rmdir()
                    logger.info(f"Creating new session: {session_id}")

            agents[key] = agent

            # Register session with MessageBus for cross-session messaging
            try:
                bus = get_message_bus()
                bus.register_session(session_id, agent.session_manager.name)
            except Exception as e:
                logger.warning(f"Failed to register session with MessageBus: {e}")

        return agents[key]


# Static files
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


# ---------- Models ----------

class SendMessageRequest(BaseModel):
    content: str


class CreateSessionRequest(BaseModel):
    name: str = "New Session"


class CreateWorkspaceRequest(BaseModel):
    name: str
    directory: str = ""


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    directory: str | None = None


# ---------- Routes ----------

@app.get("/favicon.ico")
async def favicon():
    """Serve favicon (browsers request this at root)."""
    return FileResponse(str(WEB_DIR / "static" / "favicon.ico"))


@app.get("/")
async def root():
    """Serve the main page."""
    return FileResponse(str(WEB_DIR / "static" / "index.html"))


@app.get("/api/health")
async def health_check():
    """健康检查端点"""
    return {
        "status": "ok",
        "active_agents": len(agents),
        "timestamp": datetime.now().isoformat()
    }


# ----- Workspaces -----

@app.get("/api/workspaces")
async def list_workspaces():
    """List all workspaces."""
    return {"workspaces": _list_all_workspaces()}


@app.post("/api/workspaces")
async def create_workspace(request: CreateWorkspaceRequest):
    """Create a new workspace.

    Args:
        name: Display name for the workspace
        directory: Working directory path (optional, defaults to workspace/ inside data)
    """
    # Validate name
    err = validate_workspace_name(request.name)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Generate UUID for data directory
    workspace_uuid = secrets.token_hex(4)  # 8 位十六进制短 ID

    # Default directory: use workspace/ subdir if not specified
    if request.directory:
        workspace_dir = os.path.abspath(request.directory)
    else:
        workspace_dir = str(PROJECT_ROOT / "workspace" / request.name)

    # Create workspace data directory
    ws_data_dir = WORKSPACE_DATA_DIR / workspace_uuid
    ws_data_dir.mkdir(parents=True, exist_ok=True)

    # Create sessions directory
    sessions_dir = ws_data_dir / "sessions"
    sessions_dir.mkdir(exist_ok=True)

    # Create working directory
    os.makedirs(workspace_dir, exist_ok=True)

    # Save workspace config (only metadata, no model config)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws_config = {
        "workspace_name": request.name,
        "directory": workspace_dir,
        "created_at": now,
        "updated_at": now,
    }

    save_workspace_config(workspace_uuid, ws_config)

    return {
        "uuid": workspace_uuid,
        "name": request.name,
        "directory": workspace_dir,
    }


def _remove_workspace_data(workspace_uuid: str) -> None:
    """Remove workspace data directory (filesystem only, does not touch agents dict)."""
    ws_data_dir = WORKSPACE_DATA_DIR / workspace_uuid
    if not ws_data_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")
    # Delete workspace data directory
    shutil.rmtree(ws_data_dir)


def _cleanup_agents_for_workspace(workspace_uuid: str) -> None:
    """Remove agents belonging to the given workspace from the agents dict.

    Must be called under _agents_lock.
    """
    keys_to_remove = [k for k in agents if k.startswith(f"{workspace_uuid}:")]
    for key in keys_to_remove:
        evicted = agents.pop(key)
        _agent_access.pop(key, None)
        try:
            evicted.cleanup()
        except Exception as e:
            logger.warning(f"[RootAgent] 清理 RootAgent {key} 失败: {e}")


@app.delete("/api/workspaces/{workspace_uuid}")
async def delete_workspace(workspace_uuid: str):
    """Delete a workspace and all its data."""
    if workspace_uuid == "system":
        raise HTTPException(status_code=403, detail="System workspace cannot be deleted")
    async with _agents_lock:
        _cleanup_agents_for_workspace(workspace_uuid)
    _remove_workspace_data(workspace_uuid)
    return {"success": True}


@app.put("/api/workspaces/{workspace_uuid}")
async def update_workspace(workspace_uuid: str, request: UpdateWorkspaceRequest):
    """Update workspace name and/or directory."""
    if workspace_uuid == "system":
        raise HTTPException(status_code=403, detail="System workspace cannot be modified")
    ws_data_dir = WORKSPACE_DATA_DIR / workspace_uuid
    if not ws_data_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Load existing config
    config = load_workspace_config(workspace_uuid)
    if not config:
        raise HTTPException(status_code=404, detail="Workspace config not found")

    # Update name if provided
    if request.name is not None:
        err = validate_workspace_name(request.name)
        if err:
            raise HTTPException(status_code=400, detail=err)
        config["workspace_name"] = request.name

    # Update directory if provided
    if request.directory is not None:
        new_dir = os.path.abspath(request.directory)
        config["directory"] = new_dir

    config["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not save_workspace_config(workspace_uuid, config):
        raise HTTPException(status_code=500, detail="Failed to save config")

    return {"success": True, "workspace": config}


@app.post("/api/workspaces/{workspace_uuid}/reset")
async def reset_workspace(workspace_uuid: str):
    """Reset workspace config and sessions, but keep workspace files intact."""
    async with _agents_lock:
        _cleanup_agents_for_workspace(workspace_uuid)
    _remove_workspace_data(workspace_uuid)
    logger.info(f"Workspace reset: {workspace_uuid} (data removed, user files kept)")
    return {"success": True}


# ----- Sessions -----

@app.get("/api/workspaces/{workspace_uuid}/sessions")
async def list_sessions(workspace_uuid: str, ws_dir: Path = Depends(_require_workspace)):
    """List all sessions in a workspace."""
    sessions_dir = ws_dir / "sessions"
    if not sessions_dir.exists():
        return {"sessions": []}

    sessions = []
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        index_file = session_dir / "index.json"
        if not index_file.exists():
            continue
        try:
            mtime = index_file.stat().st_mtime
            with open(index_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Extract preview from last user message
            preview = ""
            for msg in reversed(data.get("messages", [])):
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for block in content:
                            if block.get("type") == "text":
                                preview = block.get("text", "")[:200]
                                break
                    elif isinstance(content, str):
                        preview = content[:200]
                    if preview:
                        break

            metadata = data.get("metadata", {})
            sessions.append({
                "session_id": data["session_id"],
                "name": data.get("name", "Unnamed"),
                "created_at": metadata.get("created_at", ""),
                "updated_at": metadata.get("updated_at", ""),
                "message_count": len(data.get("messages", [])),
                "subagent_count": metadata.get("subagent_count", 0),
                "preview": preview,
                "hidden": metadata.get("hidden", False),
                "_mtime": mtime,
            })
        except Exception as e:
            logger.error(f"Failed to read session {index_file}: {e}")

    sessions.sort(key=lambda s: s.pop("_mtime"), reverse=True)
    return {"sessions": sessions}


@app.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}")
async def get_session(workspace_uuid: str, session_id: str, ws_dir: Path = Depends(_require_workspace)):
    """Get a specific session with all messages."""
    session_dir = ws_dir / "sessions" / session_id
    index_file = session_dir / "index.json"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 从外部文件按需注入工具结果内容（前端渲染需要）
        messages = data.get("messages", [])
        _resolve_tool_results_for_session(messages, session_dir)

        return data
    except Exception as e:
        logger.error(f"Failed to read session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to read session")


def _resolve_tool_results_for_session(messages: list[dict], session_dir: Path) -> None:
    """从外部文件按需读取工具结果内容，注入到消息中。

    用于前端渲染历史会话。Session 中只存元信息，内容保存在外部文件。
    此函数从文件读取内容并注入到消息中，供前端渲染。
    同时给已回答的 ask_user tool_use 块添加 _answered 标记。
    """
    from core.tools.shared.base import Tool

    # 第一遍：收集已回答的 ask_user tool_use_id
    answered_ask_user_ids = set()
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            # 已回答 = _wait_for_user 且有实际 content（用户已提交答案）
            if block.get("type") == "tool_result" and block.get("_wait_for_user") and block.get("content"):
                # 同时检查两种字段名
                tool_use_id = block.get("tool_use_id") or block.get("tool_call_id")
                if tool_use_id:
                    answered_ask_user_ids.add(tool_use_id)

    # 第二遍：处理工具结果 + 标记已回答的 ask_user
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue

        # 标记已回答的 ask_user tool_use/tool_call
        if msg.get("role") == "assistant":
            for block in content:
                # 同时检查 tool_use 和 tool_call 两种类型
                if block.get("type") in ("tool_use", "tool_call") and block.get("id") in answered_ask_user_ids:
                    block["_answered"] = True

        # 处理工具结果
        if msg.get("role") == "user":
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                # 已有内容（如错误信息），跳过
                if block.get("content"):
                    continue

                # 处理压缩标记
                if block.get("_compacted"):
                    output_filename = block.get("_output_path", "")
                    if output_filename:
                        tool_use_id = output_filename.replace(".txt", "")
                        block["content"] = f"[Compacted: use `read_tool_result` tool with tool_use_id=\"{tool_use_id}\" to retrieve original content]"
                    else:
                        block["content"] = "[Compacted: tool_use_id unknown]"
                    continue

                # 从外部文件读取
                output_path = block.get("_output_path", "")
                if not output_path:
                    block["content"] = "[工具输出文件路径缺失]"
                    continue

                file_path = session_dir / output_path
                if not file_path.exists():
                    # 尝试在 SubAgent 执行目录中查找
                    exec_dirs = list(session_dir.glob("exec_*"))
                    found = False
                    for exec_dir in exec_dirs:
                        candidate = exec_dir / output_path
                        if candidate.exists():
                            file_path = candidate
                            found = True
                            break
                    if not found:
                        block["content"] = f"[工具输出文件不存在: {output_path}]"
                        continue

                try:
                    file_content = file_path.read_text(encoding='utf-8', errors='replace')
                except Exception as e:
                    block["content"] = f"[读取工具输出失败: {e}]"
                    continue

                # 截断显示（前端不需要完整内容）
                if block.get("_truncated"):
                    truncated = Tool.truncate_middle(file_content, 8000)
                    file_size = block.get('_file_size', len(file_content))
                    guide = (
                        f"\n\n---\n"
                        f"[提示] 工具输出过长（{file_size:,} 字符），已截断显示。"
                        f"完整输出保存在文件: {output_path}。"
                    )
                    block["content"] = truncated + guide
                    block["is_error"] = block.get("_is_error", False)
                else:
                    # 正常输出：限制到 100K 字符
                    block["content"] = Tool.truncate_result(file_content, 100_000)
                    block["is_error"] = block.get("_is_error", False)


@app.post("/api/workspaces/{workspace_uuid}/sessions")
async def create_session(workspace_uuid: str, request: CreateSessionRequest, ws_dir: Path = Depends(_require_workspace)):
    """Create a new session."""
    session_id = secrets.token_hex(4)  # 8 位十六进制短 ID
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    session_data = {
        "session_id": session_id,
        "name": request.name,
        "messages": [],
        "metadata": {
            "created_at": now,
            "updated_at": now,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "api_calls": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            },
            "subagent_count": 0,
        }
    }

    sessions_dir = ws_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # 创建 session 目录
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    # Atomic write: write to temp file then rename
    index_file = session_dir / "index.json"
    temp_file = session_dir / "index.json.tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, index_file)
    except Exception as e:
        # Clean up temp file on error
        if temp_file.exists():
            temp_file.unlink()
        raise HTTPException(status_code=500, detail=f"Failed to create session: {e}")

    return session_data


@app.delete("/api/workspaces/{workspace_uuid}/sessions/{session_id}")
async def delete_session(workspace_uuid: str, session_id: str, ws_dir: Path = Depends(_require_workspace)):
    """Delete a session."""
    session_dir = ws_dir / "sessions" / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    # 删除整个 session 目录
    shutil.rmtree(session_dir)

    # Remove from agents dict (under lock)
    key = f"{workspace_uuid}:{session_id}"
    async with _agents_lock:
        if key in agents:
            evicted = agents.pop(key)
            _agent_access.pop(key, None)
            try:
                evicted.cleanup()
            except Exception:
                pass

    return {"success": True}


class RenameSessionRequest(BaseModel):
    name: str


class SetHiddenRequest(BaseModel):
    hidden: bool


class BatchSessionRequest(BaseModel):
    session_ids: list[str]
    action: str  # "hide", "unhide", "delete"


class AnswerAskUserRequest(BaseModel):
    tool_use_id: str  # ask_user 工具调用的 ID
    answer: str  # 用户的答案


@app.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/rename")
async def rename_session(workspace_uuid: str, session_id: str, request: RenameSessionRequest, ws_dir: Path = Depends(_require_workspace)):
    """Rename a session (atomic write)."""
    index_file = ws_dir / "sessions" / session_id / "index.json"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        data["name"] = request.name

        temp_file = index_file.with_suffix(".json.tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_file.replace(index_file)

        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to rename session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to rename session")


@app.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/hidden")
async def set_session_hidden(workspace_uuid: str, session_id: str, request: SetHiddenRequest, ws_dir: Path = Depends(_require_workspace)):
    """Set session hidden status."""
    index_file = ws_dir / "sessions" / session_id / "index.json"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if "metadata" not in data:
            data["metadata"] = {}
        data["metadata"]["hidden"] = request.hidden

        temp_file = index_file.with_suffix(".json.tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_file.replace(index_file)

        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to set hidden status for session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to set hidden status")


@app.post("/api/workspaces/{workspace_uuid}/sessions/batch")
async def batch_session_operation(workspace_uuid: str, request: BatchSessionRequest, ws_dir: Path = Depends(_require_workspace)):
    """Batch operations on sessions: hide, unhide, or delete."""
    sessions_dir = ws_dir / "sessions"
    results = []

    for session_id in request.session_ids:
        index_file = sessions_dir / session_id / "index.json"
        if not index_file.exists():
            results.append({"session_id": session_id, "success": False, "error": "Not found"})
            continue

        try:
            if request.action == "delete":
                # Delete session
                import shutil
                session_dir = sessions_dir / session_id
                shutil.rmtree(session_dir, ignore_errors=True)
                results.append({"session_id": session_id, "success": True})
            elif request.action in ("hide", "unhide"):
                # Set hidden status
                with open(index_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if "metadata" not in data:
                    data["metadata"] = {}
                data["metadata"]["hidden"] = (request.action == "hide")

                temp_file = index_file.with_suffix(".json.tmp")
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                temp_file.replace(index_file)
                results.append({"session_id": session_id, "success": True})
            else:
                results.append({"session_id": session_id, "success": False, "error": f"Unknown action: {request.action}"})
        except Exception as e:
            logger.error(f"Batch operation failed for session {session_id}: {e}")
            results.append({"session_id": session_id, "success": False, "error": str(e)})

    return {"success": True, "results": results}


# ----- SubAgent Executions -----

@app.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/executions")
async def list_executions(workspace_uuid: str, session_id: str, ws_dir: Path = Depends(_require_workspace)):
    """List all sub-agent executions for a session."""
    sessions_dir = ws_dir / "sessions"
    sm = SessionManager.load_session(session_id, sessions_dir)
    if not sm:
        raise HTTPException(status_code=404, detail="Session not found")

    logs = sm.list_subagent_logs()
    return {"executions": logs}


@app.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/executions/{exec_id}")
async def get_execution(workspace_uuid: str, session_id: str, exec_id: str, ws_dir: Path = Depends(_require_workspace)):
    """Get a specific sub-agent execution log with full messages."""
    sessions_dir = ws_dir / "sessions"
    sm = SessionManager.load_session(session_id, sessions_dir)
    if not sm:
        raise HTTPException(status_code=404, detail="Session not found")

    log = sm.load_subagent_log(exec_id)
    if not log:
        raise HTTPException(status_code=404, detail="Execution not found")

    # 从外部文件按需注入工具结果内容（前端渲染需要）
    messages = log.get("messages", [])
    exec_dir = sessions_dir / session_id / exec_id
    _resolve_tool_results_for_session(messages, exec_dir)

    return log


@app.delete("/api/workspaces/{workspace_uuid}/sessions/{session_id}/executions/{exec_id}")
async def delete_execution(workspace_uuid: str, session_id: str, exec_id: str, ws_dir: Path = Depends(_require_workspace)):
    """Delete a specific sub-agent execution log."""
    sessions_dir = ws_dir / "sessions"
    sm = SessionManager.load_session(session_id, sessions_dir)
    if not sm:
        raise HTTPException(status_code=404, detail="Session not found")

    if sm.delete_subagent_log(exec_id):
        return {"success": True}
    raise HTTPException(status_code=404, detail="Execution not found")


# ----- Tool Output Streaming -----

import re as _re
# tool_use_id 只允许字母、数字、下划线、短横线（防止路径穿越）
_TOOL_USE_ID_RE = _re.compile(r'^[a-zA-Z0-9_-]+$')


@app.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/stream/{tool_use_id}")
async def stream_tool_output(
    workspace_uuid: str,
    session_id: str,
    tool_use_id: str,
    offset: int = 0,
    ws_dir: Path = Depends(_require_workspace),
):
    """读取工具输出文件的新增内容，供前端轮询实时显示。

    文件命名格式为 {tool_use_id}_{tool_name}.txt，此接口通过 glob 匹配查找。

    Args:
        tool_use_id: 工具调用 ID（文件名的前缀部分）
        offset: 从第几个字节开始读取（前端记录上次位置）

    Returns:
        {content: 新内容, offset: 新位置, exists: 文件是否存在}
    """
    # 安全检查：tool_use_id 只允许字母、数字、下划线、短横线
    if not _TOOL_USE_ID_RE.match(tool_use_id):
        raise HTTPException(status_code=400, detail="Invalid tool_use_id")

    sessions_dir = ws_dir / "sessions"
    session_dir = sessions_dir / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    # 查找匹配 {tool_use_id}.txt 或 {tool_use_id}_*.txt 的文件
    # 同时在 session 目录和 exec_* 子目录中搜索（SubAgent 的输出在 exec_* 目录）
    matches = list(session_dir.glob(f"{tool_use_id}.txt")) + list(session_dir.glob(f"{tool_use_id}_*.txt"))
    # 搜索 exec_* 子目录
    for exec_dir in session_dir.glob("exec_*"):
        if exec_dir.is_dir():
            matches.extend(exec_dir.glob(f"{tool_use_id}.txt"))
            matches.extend(exec_dir.glob(f"{tool_use_id}_*.txt"))

    if not matches:
        return {"content": "", "offset": 0, "exists": False}

    output_file = matches[0]  # 只取第一个匹配

    try:
        file_size = output_file.stat().st_size
        if offset >= file_size:
            # 没有新内容
            return {"content": "", "offset": file_size, "exists": True}

        with open(output_file, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            new_content = f.read()

        return {
            "content": new_content,
            "offset": file_size,
            "exists": True,
        }
    except Exception as e:
        logger.warning(f"Failed to read stream file for {tool_use_id}: {e}")
        return {"content": "", "offset": offset, "exists": True}


# ----- Chat -----

def _get_session_manager(workspace_uuid: str, session_id: str) -> SessionManager | None:
    """Load a SessionManager for the given session (lightweight, no RootAgent)."""
    sessions_dir = WORKSPACE_DATA_DIR / workspace_uuid / "sessions"
    if not sessions_dir.exists():
        return None
    return SessionManager.load_session(session_id, sessions_dir)


@app.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/messages")
async def send_message(workspace_uuid: str, session_id: str, request: SendMessageRequest):
    """Send a message and get a streaming SSE response."""
    content = request.content.strip()

    # Handle special commands
    if content == "/help":
        help_text = """**特殊命令：**

- `/help` - 显示本帮助信息
- `/status` - 显示当前会话状态（上下文长度、用量等）
- `/bash <command>` - 直接执行 bash 命令（例如：`/bash ls -la`）

**工具使用：**
直接描述你要完成的任务即可，AI 会自动选择合适的工具。
"""
        # Save to session via SessionManager
        sm = _get_session_manager(workspace_uuid, session_id)
        if sm:
            sm.add_message("user", content, flush=False)
            sm.add_message("assistant", [{"type": "text", "text": help_text}], flush=False)
            sm.save()
        return StreamingResponse(_sse_stream({"type": "text", "content": help_text}), media_type="text/event-stream")

    if content == "/status":
        agent = await _get_or_create_agent(workspace_uuid, session_id)
        usage = agent.get_usage()

        # 使用 agent 内部的 token 计数方法（更准确）
        messages = agent.session_manager.get_valid_messages()
        context_tokens = agent._count_messages_tokens(messages)
        body_size = agent._estimate_request_body_size(messages)

        # Format body size
        if body_size > 1_000_000:
            body_size_str = f"{body_size / 1_000_000:.2f} MB"
        else:
            body_size_str = f"{body_size / 1_000:.1f} KB"

        status_text = f"""**当前会话状态：**

- **模型：** {agent.config.model.name} ({agent.config.model.interface_type})
- **上下文长度：** ~{context_tokens} tokens
- **请求体大小：** {body_size_str}
- **API 调用次数：** {usage['api_calls']}
- **输入 tokens：** {usage['input_tokens']:,}
- **输出 tokens：** {usage['output_tokens']:,}
- **缓存读取：** {usage.get('cache_read_tokens', 0):,} tokens
- **缓存创建：** {usage.get('cache_creation_tokens', 0):,} tokens
"""
        # Save to session via SessionManager
        agent.session_manager.add_message("user", content, flush=False)
        agent.session_manager.add_message("assistant", [{"type": "text", "text": status_text}], flush=False)
        agent.session_manager.save()
        return StreamingResponse(_sse_stream({"type": "text", "content": status_text}), media_type="text/event-stream")

    if content.startswith("/bash "):
        # 执行 bash 命令
        command = content[6:].strip()
        if not command:
            return StreamingResponse(_sse_stream({"type": "text", "content": "请输入要执行的命令"}), media_type="text/event-stream")

        # 使用 agent 的 bash 工具执行命令
        agent = await _get_or_create_agent(workspace_uuid, session_id)

        # 提前生成 tool_use_id，用于设置输出文件和 SSE 事件
        tool_use_id = f"cmd_{secrets.token_hex(4)}"

        loop = asyncio.get_running_loop()
        try:
            from core.tools import get_tool_by_name
            bash_tool = get_tool_by_name(agent.tools, 'bash')
            if not bash_tool:
                error_text = 'bash 工具不可用'
                agent.session_manager.add_message("user", content, flush=False)
                agent.session_manager.add_message("assistant", [{"type": "text", "text": error_text}], flush=False)
                agent.session_manager.save()
                return StreamingResponse(_sse_stream({"type": "error", "content": error_text}), media_type="text/event-stream")

            # 设置输出文件路径（供前端轮询实时显示）
            session_dir = agent.session_manager.session_dir
            output_file_path = str(session_dir / f"{tool_use_id}.txt")
            bash_tool.output_file = output_file_path

            result = await loop.run_in_executor(None, lambda: bash_tool.execute(command=command))
            output = result.output if hasattr(result, 'output') else str(result)
            is_error = bool(result.error if hasattr(result, 'error') else False)
        except Exception as e:
            output = f'执行失败: {str(e)}'
            is_error = True
        finally:
            if bash_tool:
                bash_tool.output_file = None

        # Build tool_use and tool_result blocks
        tool_use_block = {
            "type": "tool_use",
            "id": tool_use_id,
            "name": "bash",
            "input": {"command": command},
        }
        tool_result_block = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": output,
            "is_error": is_error,
        }
        # Save to session via SessionManager (user + assistant tool_use + user tool_result)
        agent.session_manager.add_message("user", content, flush=False)
        agent.session_manager.add_message("assistant", [tool_use_block], flush=False)
        agent.session_manager.add_message("user", [tool_result_block], flush=False)
        agent.session_manager.save()

        return StreamingResponse(_sse_stream(
            {"type": "tool_use", "tool": "bash", "input": {"command": command}, "tool_use_id": tool_use_id},
            {"type": "tool_result", "tool": "bash", "content": output, "is_error": is_error, "tool_use_id": tool_use_id},
        ), media_type="text/event-stream")
    # Normal message - send to agent
    agent = await _get_or_create_agent(workspace_uuid, session_id)

    # Prevent concurrent execution on the same session
    if agent.is_running():
        error_text = "当前会话正在执行中，请等待完成后再发送消息"
        return StreamingResponse(_sse_stream({"type": "error", "content": error_text}), media_type="text/event-stream")

    # Use a queue to bridge sync agent callbacks → async SSE generator
    event_queue: queue.Queue[str | None] = queue.Queue()

    def on_text(text: str) -> None:
        # Sentinel: 413 retry needs frontend to clear already-streamed text
        if text == "\x00RETRY_CLEAR\x00":
            event = json.dumps({"type": "retry_clear"})
            event_queue.put(f"data: {event}\n\n")
            return
        event = json.dumps({"type": "text", "content": text})
        event_queue.put(f"data: {event}\n\n")

    def on_thinking(text: str) -> None:
        event = json.dumps({"type": "thinking", "content": text})
        event_queue.put(f"data: {event}\n\n")

    def on_tool_call(tool_name: str, tool_input: dict, tool_use_id: str) -> None:
        event = json.dumps({"type": "tool_use", "tool": tool_name, "input": tool_input, "tool_use_id": tool_use_id})
        event_queue.put(f"data: {event}\n\n")

    def on_tool_result(tool_name: str, output: str, is_error: bool, tool_use_id: str) -> None:
        event = json.dumps({"type": "tool_result", "tool": tool_name, "content": output, "is_error": is_error, "tool_use_id": tool_use_id})
        event_queue.put(f"data: {event}\n\n")

        # Check for todo_write tool and push todo update event
        if tool_name == "todo_write" and not is_error and agent.session_manager:
            todos = agent.session_manager.metadata.get("todos")
            if todos is not None:
                todo_event = json.dumps({"type": "todo_update", "todos": todos})
                event_queue.put(f"data: {todo_event}\n\n")

    def on_subagent_start(exec_id: str, task_summary: str) -> None:
        # Write _subagent_ref to main session immediately so it persists across page refreshes
        agent.session_manager.add_subagent_ref(
            exec_id=exec_id,
            task_summary=task_summary,
            status="running",
        )
        agent.session_manager.save()

        # Send SSE event for real-time UI update
        event = json.dumps({"type": "subagent_start", "exec_id": exec_id, "task_summary": task_summary})
        event_queue.put(f"data: {event}\n\n")

    async def generate():
        # Run the agent loop in a background thread
        loop = asyncio.get_running_loop()

        def run_agent():
            try:
                agent.run(
                    user_input=request.content,
                    on_text=on_text,
                    on_thinking=on_thinking,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                    on_subagent_start=on_subagent_start,
                )
            except Exception as e:
                logger.error(f"RootAgent error: {e}")
                err_event = json.dumps({"type": "error", "content": str(e)})
                event_queue.put(f"data: {err_event}\n\n")
            finally:
                event_queue.put(None)  # sentinel: done

        task = asyncio.ensure_future(loop.run_in_executor(None, run_agent))

        # Stream events from queue to client (use to_thread to avoid blocking the event loop)
        try:
            while True:
                try:
                    # Use to_thread so the blocking queue.get() doesn't block the async loop
                    event = await asyncio.to_thread(event_queue.get, True, 0.5)
                    if event is None:
                        break
                    yield event
                except queue.Empty:
                    continue
        except asyncio.CancelledError:
            # Client disconnected — let the agent keep running in the background.
            # The agent only stops when the user explicitly clicks the stop button
            # (which calls the /stop endpoint). This prevents browser refresh or
            # network glitches from aborting long-running tasks.
            logger.info("Client disconnected, agent continues running in background")
            return

        # Send done signal
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ----- Global Config -----

class ModelConfigRequest(BaseModel):
    """Request model for updating a model configuration."""
    name: str | None = None
    interface_type: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int | None = None
    max_context_tokens: int | None = None
    multimodal: bool | None = None
    temperature: float | None = None


class UpdateConfigRequest(BaseModel):
    """Request model for updating global configuration."""
    model: ModelConfigRequest | None = None          # RootAgent model (multi-turn)
    llm_model: ModelConfigRequest | None = None      # LLM model (single-turn, optional)
    system: dict | None = None                       # System parameters (pip_mirror, etc.)


@app.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/stop")
async def stop_agent(workspace_uuid: str, session_id: str):
    """Stop the currently running agent for a session."""
    key = f"{workspace_uuid}:{session_id}"
    if key not in agents:
        return {"success": False, "message": "没有正在运行的 RootAgent"}

    agent = agents[key]
    if not agent.is_running():
        return {"success": False, "message": "RootAgent 当前未在运行"}

    agent.stop()
    return {"success": True, "message": "已发送停止信号"}


@app.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/answer-ask-user")
async def answer_ask_user(workspace_uuid: str, session_id: str, request: AnswerAskUserRequest):
    """用户提交 ask_user 工具的答案，后端补 tool_result 并继续 agent 循环"""
    key = f"{workspace_uuid}:{session_id}"
    agent = agents.get(key)
    if not agent:
        raise HTTPException(404, "Agent not found")

    if agent.is_running():
        return StreamingResponse(_sse_stream({"type": "error", "content": "Agent is already running"}), media_type="text/event-stream")

    # 使用前端提供的 tool_use_id
    ask_user_tool_use_id = request.tool_use_id
    logger.info(f"[ask-user] 尝试为 tool_use_id={ask_user_tool_use_id} 提交答案")

    # 找到并替换占位符 tool_result
    found_placeholder = False
    for msg in reversed(agent.session_manager.messages):
        if msg["role"] != "user":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            # 检查两种字段名（tool_use_id 或 tool_call_id）
            block_tool_id = block.get("tool_use_id") or block.get("tool_call_id")
            if block.get("type") == "tool_result" and block_tool_id == ask_user_tool_use_id:
                # 替换占位符内容
                block["content"] = request.answer
                found_placeholder = True
                logger.info(f"[ask-user] 找到并替换 tool_result: tool_use_id={ask_user_tool_use_id}")
                # 同步更新外部文件，保持与其他工具一致
                output_path = block.get("_output_path", "")
                if output_path:
                    ext_file = agent.session_manager.session_dir / output_path
                    try:
                        ext_file.write_text(request.answer, encoding="utf-8")
                        block["_file_size"] = len(request.answer.encode("utf-8"))
                        logger.info(f"[ask-user] 已更新外部文件: {output_path}")
                    except Exception as e:
                        logger.warning(f"[ask-user] 更新外部文件失败: {e}")
                break
        if found_placeholder:
            break

    if not found_placeholder:
        logger.error(f"[ask-user] 未找到占位符 tool_result: tool_use_id={ask_user_tool_use_id}")
        raise HTTPException(400, f"Placeholder tool_result not found for tool_use_id: {ask_user_tool_use_id}")

    # 在对应的 tool_use/tool_call 块上添加 _answered 标记
    found_tool_use = False
    for msg in agent.session_manager.messages:
        if msg["role"] != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            # 同时检查 tool_use 和 tool_call 两种类型
            if block.get("type") in ("tool_use", "tool_call") and block.get("id") == ask_user_tool_use_id:
                block["_answered"] = True
                found_tool_use = True
                logger.info(f"[ask-user] 在 {block.get('type')} 上添加 _answered=true: id={ask_user_tool_use_id}")
                break

    if not found_tool_use:
        logger.warning(f"[ask-user] 未找到对应的 tool_use/tool_call: id={ask_user_tool_use_id}")

    agent.session_manager.save()

    # 继续 agent 循环
    event_queue: queue.Queue[str | None] = queue.Queue()

    def on_text(text: str) -> None:
        if text == "\x00RETRY_CLEAR\x00":
            event = json.dumps({"type": "retry_clear"})
            event_queue.put(f"data: {event}\n\n")
            return
        event = json.dumps({"type": "text", "content": text})
        event_queue.put(f"data: {event}\n\n")

    def on_thinking(text: str) -> None:
        event = json.dumps({"type": "thinking", "content": text})
        event_queue.put(f"data: {event}\n\n")

    def on_tool_call(tool_name: str, tool_input: dict, tool_use_id: str) -> None:
        event = json.dumps({"type": "tool_use", "tool": tool_name, "input": tool_input, "tool_use_id": tool_use_id})
        event_queue.put(f"data: {event}\n\n")

    def on_tool_result(tool_name: str, output: str, is_error: bool, tool_use_id: str) -> None:
        event = json.dumps({"type": "tool_result", "tool": tool_name, "content": output, "is_error": is_error, "tool_use_id": tool_use_id})
        event_queue.put(f"data: {event}\n\n")

        # Check for todo_write tool and push todo update event
        if tool_name == "todo_write" and not is_error and agent.session_manager:
            todos = agent.session_manager.metadata.get("todos")
            if todos is not None:
                todo_event = json.dumps({"type": "todo_update", "todos": todos})
                event_queue.put(f"data: {todo_event}\n\n")

    def on_subagent_start(exec_id: str, task_summary: str) -> None:
        agent.session_manager.add_subagent_ref(
            exec_id=exec_id,
            task_summary=task_summary,
            status="running",
        )
        agent.session_manager.save()
        event = json.dumps({"type": "subagent_start", "exec_id": exec_id, "task_summary": task_summary})
        event_queue.put(f"data: {event}\n\n")

    async def generate():
        loop = asyncio.get_running_loop()

        def run_agent():
            try:
                agent.resume_after_ask_user(
                    on_text=on_text,
                    on_thinking=on_thinking,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                    on_subagent_start=on_subagent_start,
                )
            except Exception as e:
                logger.error(f"RootAgent error: {e}")
                err_event = json.dumps({"type": "error", "content": str(e)})
                event_queue.put(f"data: {err_event}\n\n")
            finally:
                event_queue.put(None)  # sentinel: done

        task = asyncio.ensure_future(loop.run_in_executor(None, run_agent))

        try:
            while True:
                try:
                    event = await asyncio.to_thread(event_queue.get, timeout=0.1)
                except Exception:
                    continue
                if event is None:
                    done_event = json.dumps({"type": "done"})
                    yield f"data: {done_event}\n\n"
                    break
                yield event
        except asyncio.CancelledError:
            task.cancel()

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/status")
async def get_agent_status(workspace_uuid: str, session_id: str):
    """Check if an agent is running for a session."""
    key = f"{workspace_uuid}:{session_id}"
    if key not in agents:
        return {"running": False}

    agent = agents[key]
    return {"running": agent.is_running()}


def _mask_single_model(model: dict) -> dict:
    """Mask API key in a single model config dict."""
    m = model.copy()
    api_key = m.get("api_key", "")
    if api_key:
        m["api_key_masked"] = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "***"
    m.pop("api_key", None)
    return m


def _mask_api_key(config: dict) -> dict:
    """Mask API keys in config for safe display."""
    result = config.copy()
    for key in ("model", "llm_model"):
        if key in result and isinstance(result[key], dict):
            result[key] = _mask_single_model(result[key])
    return result


@app.get("/api/config")
async def get_config():
    """Get global model configuration."""
    config = load_global_config()
    # Mask API keys for security
    masked_config = _mask_api_key(config)
    return {
        "config": masked_config,
        "config_path": str(GLOBAL_CONFIG_PATH),
    }


def _update_model_config(existing: dict, update: ModelConfigRequest) -> dict:
    """Update a model config dict with values from request."""
    result = existing.copy() if existing else {}
    for key, value in update.model_dump(exclude_none=True).items():
        if key == "base_url" and value:
            result[key] = value.rstrip("/")
        else:
            result[key] = value
    return result


@app.put("/api/config")
async def update_config(request: UpdateConfigRequest):
    """Update global model configuration."""
    config = load_global_config()

    # Update agent model config (multi-turn)
    if request.model is not None:
        existing_model = config.get("model", {})
        config["model"] = _update_model_config(existing_model, request.model)

    # Update LLM model config (single-turn, optional)
    if request.llm_model is not None:
        # If all fields are empty/None, remove llm_model config
        llm_data = request.llm_model
        if not llm_data.name and not llm_data.api_key:
            config.pop("llm_model", None)
        else:
            existing_llm = config.get("llm_model", {})
            config["llm_model"] = _update_model_config(existing_llm, llm_data)

    # Update system config
    if request.system is not None:
        existing_system = config.get("system", {})
        existing_system.update(request.system)
        config["system"] = existing_system

    if not save_global_config(config):
        raise HTTPException(status_code=500, detail="Failed to save config")

    # 通知所有缓存的 RootAgent 重新加载配置（新的 API key / model 等）
    async with _agents_lock:
        for key, agent in list(agents.items()):
            agent.reload_config()
            logger.info(f"[Config] 已通知 RootAgent {key} 重新加载配置")

    return {"success": True, "config_path": str(GLOBAL_CONFIG_PATH)}


class TestConfigRequest(BaseModel):
    config: ModelConfigRequest | None = None


@app.post("/api/config/test")
async def test_config(request: TestConfigRequest = TestConfigRequest()):
    """Test model configuration by connecting to the API.

    如果 request.config 有值，则用传入的参数测试；否则用已保存的主模型配置。
    """
    try:
        if request.config is not None:
            # 用传入的配置临时测试
            from core.config import ModelConfig

            # 如果 api_key 为空，尝试从已保存的配置中获取
            api_key = request.config.api_key
            if not api_key:
                saved_config = load_config()
                if saved_config.model.api_key:
                    api_key = saved_config.model.api_key

            if not api_key:
                return {
                    "success": False,
                    "message": "API Key 不能为空",
                    "interface_type": request.config.interface_type or "anthropic",
                }

            model_cfg = ModelConfig(
                name=request.config.name or "claude-sonnet-4-6",
                interface_type=request.config.interface_type or "anthropic",
                api_key=api_key,
                base_url=(request.config.base_url or "https://api.anthropic.com").rstrip("/"),
                max_tokens=int(request.config.max_tokens or 16384),
                max_context_tokens=int(request.config.max_context_tokens or 256000),
                multimodal=bool(request.config.multimodal) if request.config.multimodal is not None else True,
                temperature=float(request.config.temperature) if request.config.temperature is not None else 0.2,
            )
        else:
            # 默认用已保存的主模型
            config = load_config()
            model_cfg = config.model

        from core.llm import create_llm_client
        client = create_llm_client(model_cfg)
        success, message = client.test_connection()
        interface_type = client.interface_type
        client.close()

        return {
            "success": success,
            "message": message,
            "interface_type": interface_type,
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e),
            "interface_type": "unknown",
        }


# ----- Workspace Files -----

def _find_workspace_for_file(file_path: str) -> tuple[str, Path] | None:
    """Scan all workspaces and find which one contains the given relative file path.

    Returns (workspace_uuid, full_file_path) or None if not found in any workspace.
    """
    if not WORKSPACE_DATA_DIR.exists():
        return None

    for item in WORKSPACE_DATA_DIR.iterdir():
        if not item.is_dir() or item.name.startswith('.'):
            continue
        info = _get_workspace_info(item.name)
        if not info:
            continue
        workspace_dir = info.get("directory", "")
        if not workspace_dir:
            continue
        workspace_path = Path(workspace_dir).resolve()
        file_full_path = (workspace_path / file_path).resolve()
        # Security: ensure file is within the workspace directory
        if not str(file_full_path).startswith(str(workspace_path)):
            continue
        if file_full_path.exists() and file_full_path.is_file():
            return (item.name, file_full_path)
    return None


def _serve_workspace_file(workspace_dir: str, file_path: str) -> FileResponse:
    """Serve a file from a workspace directory with path-traversal protection."""
    workspace_path = Path(workspace_dir).resolve()
    file_full_path = (workspace_path / file_path).resolve()
    if not str(file_full_path).startswith(str(workspace_path)):
        raise HTTPException(status_code=403, detail="Access denied: path traversal not allowed")
    if not file_full_path.exists() or not file_full_path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
    return FileResponse(str(file_full_path))


async def _sse_stream(*events: dict):
    """Yield SSE events followed by a done event."""
    for event in events:
        yield f"data: {json.dumps(event)}\n\n"
    yield f"data: {json.dumps({'type': 'done'})}\n\n"


@app.get("/api/workspace/files/{file_path:path}")
async def get_workspace_file_short(file_path: str, workspace_uuid: str = ""):
    """Serve a file from a workspace (no UUID in URL path).

    If workspace_uuid query parameter is provided, look in that workspace only.
    Otherwise, scan all workspaces to find the file.
    """
    if workspace_uuid:
        info = _get_workspace_info(workspace_uuid)
        if not info:
            raise HTTPException(status_code=404, detail="Workspace not found")
        workspace_dir = info.get("directory", "")
        if not workspace_dir:
            raise HTTPException(status_code=404, detail="Workspace directory not configured")
        return _serve_workspace_file(workspace_dir, file_path)

    # No UUID: scan all workspaces
    result = _find_workspace_for_file(file_path)
    if not result:
        raise HTTPException(status_code=404, detail=f"File not found in any workspace: {file_path}")

    _, file_full_path = result
    return FileResponse(str(file_full_path))


@app.get("/api/workspaces/{workspace_uuid}/files/{file_path:path}")
async def get_workspace_file(workspace_uuid: str, file_path: str):
    """Serve a file from the workspace directory (for images, etc.)."""
    info = _get_workspace_info(workspace_uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_dir = info.get("directory", "")
    if not workspace_dir:
        raise HTTPException(status_code=404, detail="Workspace directory not configured")

    return _serve_workspace_file(workspace_dir, file_path)


# ----- Directory Browser -----

@app.get("/api/browse")
async def browse_directory(path: str = ""):
    """Browse directories on the server filesystem.

    Args:
        path: Directory path to browse. Empty string returns system drives (Windows) or root (Unix).

    Returns:
        List of directories with their names and full paths.
    """
    if not path:
        if sys.platform == "win32":
            # Get available drives on Windows
            import string
            drives = []
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    drives.append({"name": drive, "path": drive})
            return {"path": "", "directories": drives, "parent": None}
        else:
            path = "/"

    # Validate path exists and is a directory
    # If path doesn't exist, try to find the nearest existing parent
    try:
        path_obj = Path(path)
        if not path_obj.exists():
            # Auto-fallback: find nearest existing parent directory
            original_path = path
            search_path = path_obj
            while search_path and str(search_path) != str(search_path.parent):
                search_path = search_path.parent
                if search_path.exists():
                    path_obj = search_path
                    break
            else:
                # Fallback to system root or drives
                if sys.platform == "win32":
                    return {"path": "", "directories": [], "parent": None,
                            "fallback": f"原路径不存在: {original_path}，请从磁盘列表选择"}
                else:
                    return {"path": "/", "directories": [], "parent": None,
                            "fallback": f"原路径不存在: {original_path}，已跳转到根目录"}
        if not path_obj.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {path}")
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {path}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # List directories
    directories = []
    try:
        for item in sorted(path_obj.iterdir(), key=lambda x: x.name.lower()):
            if item.is_dir() and not item.name.startswith('.'):
                directories.append({
                    "name": item.name,
                    "path": str(item.resolve())
                })
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {path}")

    # Get parent directory
    resolved_path = str(path_obj.resolve())

    # Check if we're at a drive root (e.g., C:\)
    # A drive root has the pattern X:\ where X is a letter
    is_drive_root = sys.platform == "win32" and re.match(r'^[A-Z]:\\$', resolved_path, re.IGNORECASE)

    if is_drive_root:
        # At drive root, parent is drive list (empty string)
        parent = ""
    elif resolved_path == str(Path(resolved_path).anchor):
        # At root directory (e.g., / on Unix), no parent
        parent = None
    else:
        parent = str(path_obj.parent.resolve())

    return {
        "path": resolved_path,
        "directories": directories,
        "parent": parent
    }


@app.get("/api/files")
async def list_files(workspace_uuid: str, path: str = ""):
    """List files in workspace directory for file selection.

    Args:
        workspace_uuid: Workspace UUID to restrict file listing
        path: Relative path within workspace. Empty string lists workspace root.

    Returns:
        List of files and directories with relative paths.
    """
    # Get workspace directory
    ws_config = load_workspace_config(workspace_uuid)
    if not ws_config:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_dir = Path(ws_config.get("directory", ""))
    if not workspace_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace directory not found")

    # Build target path
    if path:
        target_path = (workspace_dir / path).resolve()
        # Security: ensure path is within workspace
        try:
            target_path.relative_to(workspace_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied: path outside workspace")
    else:
        target_path = workspace_dir.resolve()

    if not target_path.exists() or not target_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {path}")

    # List files and directories
    items = []
    try:
        for item in sorted(target_path.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if item.name.startswith('.'):
                continue
            rel_path = str(item.relative_to(workspace_dir))
            items.append({
                "name": item.name,
                "path": rel_path,
                "is_file": item.is_file()
            })
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {path}")

    # Get parent path
    if target_path == workspace_dir.resolve():
        parent = None
    else:
        parent = str(target_path.parent.relative_to(workspace_dir))
        if parent == ".":
            parent = ""

    return {
        "path": path,
        "items": items,
        "parent": parent
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
