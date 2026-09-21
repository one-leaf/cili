"""Web API 共享依赖：全局状态 + 跨路由域 helper（deps 中间层防循环导入）。

各 routes_*.py 只从本模块与 core 导入；web_api.py 只 import router 与 deps。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from core.agent import Agent
from core.base_agent import RETRY_CLEAR_SENTINEL
from core.config import (
    load_config, PROJECT_ROOT, load_workspace_config, save_workspace_config,
    GLOBAL_CONFIG_PATH, get_workspace_data_dir, load_workspaces_index,
    find_workspace_entry,
)
from core.event_bus import get_event_bus
from core.message_bus import get_message_bus
from core.session import SessionManager
from core.tools.todo import get_todos_from_session

# Configure logging（root handler 由本模块首个配置，web_api 不再重复 basicConfig）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global agents dict: session_id -> Agent (master)
agents: dict[str, Agent] = {}
# LRU tracking: key -> last access timestamp
_agent_access: dict[str, float] = {}
_MAX_AGENTS = 20  # Maximum number of agents to keep in memory
# Lock for concurrent access to agents dict
_agents_lock = asyncio.Lock()

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
    if not _SAFE_ID_RE.match(workspace_uuid) or '..' in workspace_uuid:
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
    """Ensure default workspace exists, create if not. Returns workspace UUID or None on failure."""
    try:
        # Check if any workspace exists with name "Default" or "default" (legacy)
        for entry in load_workspaces_index():
            name = entry.get("workspace_name")
            uuid = entry.get("uuid")
            if name in ("Default", "default") and uuid:
                # Migrate legacy "default" to "Default"
                if name == "default":
                    entry["workspace_name"] = "Default"
                    save_workspace_config(uuid, entry)
                    logger.info(f"Migrated default workspace name: {uuid}")
                logger.info(f"Default workspace found: {uuid}")
                return uuid

        # Create default workspace
        workspace_uuid = _new_short_id()
        ws_data_dir = get_workspace_data_dir(workspace_uuid)
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
    logger.info(f"[master Agent LRU] 淘汰闲置 master Agent: {oldest}")
    evicted = agents.pop(oldest)
    _agent_access.pop(oldest, None)
    try:
        evicted.cleanup()
    except Exception as e:
        logger.warning(f"[master Agent LRU] 清理被淘汰的 master Agent 失败: {e}")


async def _get_or_create_agent(workspace_uuid: str, session_id: str) -> Agent:
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

            agent = Agent(config, role="master", cwd=workspace_dir, workspace_uuid=workspace_uuid)

            # Load the requested session if different from default
            if session_id != agent.current_session_id:
                session_dir = agent.sessions_dir / session_id
                index_file = session_dir / "index.json"
                if index_file.exists():
                    # Load existing session
                    agent.switch_session(session_id)
                    logger.info(f"Loaded existing session: {session_id}")
                else:
                    # 新会话：为请求的 id 直接新建 SessionManager 并立即落盘，
                    # 避免默认会话 index.json 不迁移、旧目录 rmdir 静默失败残留（W15）
                    old_session_dir = agent.session_manager.session_dir
                    new_sm = SessionManager(session_id, agent.sessions_dir)
                    new_sm.name = f"Session {session_id[:8]}"
                    new_sm.save(force=True)  # 新会话首次落盘：跳过脏标记短路
                    agent.session_manager = new_sm
                    agent.context.set_session_manager(new_sm)  # 同步 context 引用，保持一致
                    agent.current_session_id = session_id
                    agent._session_id = session_id
                    agent.session_dir = new_sm.session_dir
                    agent.messages = new_sm.messages  # Update reference
                    agent._usage = new_sm.get_usage()
                    # 同步工具 session_manager 引用（与 switch_session 一致）
                    for tool in agent.tools:
                        tool.session_manager = new_sm
                    # 删除空的旧默认会话目录，避免孤立目录
                    if old_session_dir.exists() and old_session_dir != new_sm.session_dir:
                        shutil.rmtree(old_session_dir, ignore_errors=True)
                    logger.info(f"Creating new session: {session_id}")

            agents[key] = agent

            # 全局事件流：master 工具实时输出 → 事件总线（无 exec_id 表示 master 工具）
            bus = get_event_bus()

            def _on_tool_output(tool_name: str, content: str, offset: int, tool_use_id: str) -> None:
                bus.publish({
                    "type": "tool_output",
                    "workspace_uuid": workspace_uuid,
                    "session_id": session_id,
                    "tool": tool_name,
                    "content": content,
                    "offset": offset,
                    "tool_use_id": tool_use_id,
                })

            agent._on_tool_output = _on_tool_output

            # Register session with MessageBus for cross-session messaging
            # （注意：不能复用变量名 bus——上方 _on_tool_output 闭包晚绑定捕获，
            #  改名 mbus 防止把事件总线遮蔽成 MessageBus，导致 publish 属性缺失）
            try:
                mbus = get_message_bus()
                mbus.register_session(session_id, agent.session_manager.name)
            except Exception as e:
                logger.warning(f"Failed to register session with MessageBus: {e}")

        return agents[key]


# ---------- 会话运行认领（防 send_message/answer_ask_user 双循环 TOCTOU） ----------

# 每个会话的执行中 claim（防 send_message 的 is_running 检查 TOCTOU）：
# 检查与 run 实际启动之间第二个请求可能并发通过检查，导致同一 agent 双循环
# 同时改写 messages。用 set 在请求入口原子认领，run 结束后释放。
_session_run_claims: set[str] = set()
_session_run_claims_lock = threading.Lock()


def _claim_session_run(key: str) -> bool:
    """原子认领会话执行权，返回是否认领成功。

    在锁内 check-and-set，关闭 is_running 检查到 run 启动之间的 TOCTOU 窗口；
    agent.is_running() 作为兜底（历史请求 claim 泄漏时仍能挡住）。
    """
    with _session_run_claims_lock:
        if key in _session_run_claims:
            return False
        agent = agents.get(key)
        if agent is not None and agent.is_running():
            return False
        _session_run_claims.add(key)
        return True


def _release_session_run(key: str) -> None:
    """释放会话执行权认领。"""
    with _session_run_claims_lock:
        _session_run_claims.discard(key)


# ---------- SSE 回调组 / 事件流（send_message 与 answer_ask_user 共用） ----------

@dataclass
class _SSECallbacks:
    """SSE 事件回调组：send_message 与 answer_ask_user 共用同一套实现。"""
    on_text: Callable[[str], None]
    on_thinking: Callable[[str], None]
    on_tool_call: Callable[[str, dict, str], None]
    on_tool_result: Callable[[str, str, bool, str], None]
    on_agent_start: Callable[[str, str], None]
    on_agent_complete: Callable[[str], None]


def _make_sse_callbacks(event_queue: queue.Queue[str | None], agent) -> _SSECallbacks:
    """构造统一的 SSE 回调组，同步 agent 回调 → 队列，供两个 Agent 运行入口复用。"""

    def on_text(text: str) -> None:
        # Sentinel: 413 retry needs frontend to clear already-streamed text
        if text == RETRY_CLEAR_SENTINEL:
            event = json.dumps({"type": "retry_clear"}, ensure_ascii=False)
            event_queue.put(f"data: {event}\n\n")
            return
        event = json.dumps({"type": "text", "content": text}, ensure_ascii=False)
        event_queue.put(f"data: {event}\n\n")

    def on_thinking(text: str) -> None:
        event = json.dumps({"type": "thinking", "content": text}, ensure_ascii=False)
        event_queue.put(f"data: {event}\n\n")

    def on_tool_call(tool_name: str, tool_input: dict, tool_use_id: str) -> None:
        event = json.dumps({"type": "tool_use", "tool": tool_name, "input": tool_input, "tool_use_id": tool_use_id}, ensure_ascii=False)
        event_queue.put(f"data: {event}\n\n")

    def on_tool_result(tool_name: str, output: str, is_error: bool, tool_use_id: str) -> None:
        # Skip tool_result SSE for placeholder tools (they have dedicated SSE events)
        if tool_name in ("ask_user", "agent"):
            return
        event = json.dumps({"type": "tool_result", "tool": tool_name, "content": output, "is_error": is_error, "tool_use_id": tool_use_id}, ensure_ascii=False)
        event_queue.put(f"data: {event}\n\n")

        # Check for todo_write tool and push todo update event
        if tool_name == "todo_write" and not is_error:
            todos = get_todos_from_session(agent.session_manager)
            if todos:
                todo_event = json.dumps({"type": "todo_update", "todos": todos}, ensure_ascii=False)
                event_queue.put(f"data: {todo_event}\n\n")

    def on_agent_start(exec_id: str, task_summary: str) -> None:
        # Send SSE event for real-time UI update
        event = json.dumps({"type": "agent_start", "exec_id": exec_id, "task_summary": task_summary}, ensure_ascii=False)
        event_queue.put(f"data: {event}\n\n")

    def on_agent_complete(exec_id: str) -> None:
        # Push SSE event for real-time UI update
        event = json.dumps({"type": "agent_complete", "exec_id": exec_id}, ensure_ascii=False)
        event_queue.put(f"data: {event}\n\n")

    return _SSECallbacks(
        on_text=on_text,
        on_thinking=on_thinking,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
        on_agent_start=on_agent_start,
        on_agent_complete=on_agent_complete,
    )


async def _sse_stream(*events: dict):
    """Yield SSE events followed by a done event."""
    for event in events:
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
