"""Web server for Cili - provides HTTP API and serves static files.

按资源域拆分后的装配入口：app 创建、中间件、静态文件、路由挂载。
路由实现分散在 web/routes_*.py，共享状态与 helper 在 web/deps.py。
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.config import load_config, PROJECT_ROOT

from web.deps import agents, _LOCALHOST_IPS, WEB_DIR
from web.routes_workspace import router as workspace_router
from web.routes_chat import router as chat_router
from web.routes_ask_user import router as ask_user_router
from web.routes_config import router as config_router
from web.routes_files import router as files_router
from web.routes_memory import router as memory_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 - 优雅关闭时清理资源"""
    # 确保浏览器服务实例已创建（Playwright 延迟到首次操作时启动）
    from core.browser_service import get_service
    get_service()
    # 初始化 MessageBus
    from core.message_bus import start_message_bus
    start_message_bus()

    # 启动 MCP provider 并在后台连接已配置的服务器（不阻塞启动）
    def _connect_mcp_servers() -> None:
        try:
            cfg = load_config()
            if cfg.mcp_servers:
                # 局部导入，与 shutdown 路径 stop_mcp_provider 风格一致
                from core.tools.mcp import get_provider
                get_provider().ensure_connected(cfg.mcp_servers)
        except Exception as e:
            logger.warning(f"[Server] 连接 MCP 服务器失败: {e}")

    threading.Thread(target=_connect_mcp_servers, daemon=True).start()
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
    # 关闭时停止 MCP provider（断开所有服务器连接）
    try:
        from core.tools.mcp import stop_mcp_provider
        stop_mcp_provider()
    except Exception as e:
        logger.warning(f"[Server] 停止 MCP provider 失败: {e}")
    # 关闭时停止 cron 调度器
    try:
        from core.cron import stop_scheduler
        stop_scheduler()
    except Exception as e:
        logger.warning(f"[Server] 停止 cron 调度器失败: {e}")
    # 关闭时清理所有 master Agent 资源
    logger.info(f"[Server] 正在关闭，清理 {len(agents)} 个 master Agent...")
    for key, agent in list(agents.items()):
        try:
            agent.stop()
            agent.cleanup()
        except Exception as e:
            logger.warning(f"[Server] 清理 master Agent {key} 失败: {e}")
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


def _ip_matches(client_ip: str, allowed: list[str]) -> bool:
    """精确 IP 或 CIDR 前缀匹配（W18）。非 IP 字符串退化为精确匹配。"""
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return client_ip in allowed
    for entry in allowed:
        if not entry:
            continue
        if "/" in entry:
            try:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        elif entry == client_ip:
            return True
    return False


@app.middleware("http")
async def check_access_control(request: Request, call_next):
    """Middleware to enforce access control.

    Two modes:
    - No access_token configured: IP-based access control. Granted if client
      IP is localhost (always allowed) or in the allowed_ips whitelist.
    - access_token configured: ALL requests (including localhost) must carry a
      valid token via X-Access-Token header or ?token= query param (the latter
      lets the browser load static assets on first page load).

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

    access_token = config.system.access_token or ""

    # access_token 已配置：鉴权优先于 IP 白名单，任何来源都必须带有效令牌
    if access_token:
        provided = request.headers.get("X-Access-Token") or request.query_params.get("token") or ""
        if provided and hmac.compare_digest(provided, access_token):
            return await call_next(request)
        logger.warning(f"Blocked access from {client_ip}: missing/invalid access_token")
        return JSONResponse(
            status_code=403,
            content={"detail": "Access denied: invalid or missing token"}
        )

    # Always allow localhost
    if client_ip in _LOCALHOST_IPS:
        return await call_next(request)

    # Check whitelist（支持 CIDR 前缀，如 "192.168.1.0/24"）
    allowed_ips = config.system.allowed_ips or []
    if _ip_matches(client_ip, allowed_ips):
        return await call_next(request)

    # Blocked
    logger.warning(f"Blocked access from {client_ip}")
    return JSONResponse(
        status_code=403,
        content={"detail": "Access denied: IP not allowed"}
    )


# Static files (no-store: 前端页面和 js/css 每次打开强制更新，不做缓存)
class NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/static", NoCacheStaticFiles(directory=str(WEB_DIR / "static")), name="static")
app.mount("/docs", NoCacheStaticFiles(directory=str(PROJECT_ROOT / "docs")), name="docs")


@app.get("/favicon.ico")
async def favicon():
    """Serve favicon (browsers request this at root)."""
    response = FileResponse(str(WEB_DIR / "static" / "favicon.ico"))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def root():
    """Serve the main page."""
    response = FileResponse(str(WEB_DIR / "static" / "index.html"))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/s/{workspace_uuid}/{session_id}")
async def session_view(workspace_uuid: str, session_id: str):
    """独立会话查看页（完整会话模式）。"""
    response = FileResponse(str(WEB_DIR / "static" / "session.html"))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/s/{workspace_uuid}/{session_id}/{msg_ids}")
async def session_message_view(workspace_uuid: str, session_id: str, msg_ids: str):
    """独立会话查看页（指定消息模式，msg_ids 为逗号分隔的消息 ID）。"""
    response = FileResponse(str(WEB_DIR / "static" / "session.html"))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
async def health_check():
    """健康检查端点"""
    return {
        "status": "ok",
        "active_agents": len(agents),
        "timestamp": datetime.now().isoformat()
    }


# ----- Routes（按资源域挂载；files 域含 catch-all 路径，放在 config/memory 之后避免吞前缀） -----
app.include_router(workspace_router)
app.include_router(chat_router)
app.include_router(ask_user_router)
app.include_router(config_router)
app.include_router(files_router)
app.include_router(memory_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
