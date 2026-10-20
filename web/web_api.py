"""Web server for Cili - provides HTTP API and serves static files.

按资源域拆分后的装配入口：app 创建、中间件、静态文件、路由挂载。
路由实现分散在 web/routes_*.py，共享状态与 helper 在 web/deps.py。
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.app_context import app_context
from core.config import PROJECT_ROOT, load_config
from web.deps import _LOCALHOST_IPS, WEB_DIR, registry
from web.routes_ask_user import router as ask_user_router
from web.routes_chat import router as chat_router
from web.routes_config import router as config_router
from web.routes_files import router as files_router
from web.routes_memory import router as memory_router
from web.routes_qq import router as qq_router
from web.routes_update import router as update_router
from web.routes_wechat import router as wechat_router
from web.routes_workspace import router as workspace_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 - 启动/优雅关闭各全局单例（统一由 AppContext 持有）。"""
    # Web 接入端初始化（默认工作区 + 全局配置自检）。原先在 import web.deps 时
    # 隐式执行，改为在生命周期入口显式调用（幂等）。
    from web.deps import init_web_deps
    init_web_deps()

    app_context.startup()
    yield
    app_context.shutdown()


# Initialize FastAPI app with lifespan
app = FastAPI(title="Cili Web API", version="1.0.0", lifespan=lifespan)

# Add CORS middleware - restrict to localhost by default for security
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
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


# P1-32：?token= 首次鉴权成功后下发的 HttpOnly cookie 名（与前端 localStorage
# 键同名但用途不同：localStorage 供 JS 注入请求头，cookie 供服务端校验静态资源）
_ACCESS_TOKEN_COOKIE = "cili_access_token"


@app.middleware("http")
async def check_access_control(request: Request, call_next):
    """Middleware to enforce access control.

    Two modes:
    - No access_token configured: IP-based access control. Granted if client
      IP is localhost (always allowed) or in the allowed_ips whitelist.
    - access_token configured: ALL requests (including localhost) must carry a
      valid token via X-Access-Token header, ?token= query param, or the
      HttpOnly cookie issued after the first successful ?token= auth.

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

    # access_token 已配置：鉴权优先于 IP 白名单，任何来源都必须带有效令牌。
    # 令牌来源优先级：X-Access-Token 头 > ?token= 查询参数 > cookie
    if access_token:
        header_token = request.headers.get("X-Access-Token") or ""
        query_token = request.query_params.get("token") or ""
        cookie_token = request.cookies.get(_ACCESS_TOKEN_COOKIE) or ""
        provided = header_token or query_token or cookie_token
        if provided and hmac.compare_digest(provided, access_token):
            response = await call_next(request)
            # P1-32：?token= 首次鉴权成功即下发 HttpOnly cookie，此后静态资源、
            # 页面刷新不再依赖 URL 携带令牌，收敛 Referer/浏览器历史泄露面
            if query_token and hmac.compare_digest(query_token, access_token):
                response.set_cookie(
                    _ACCESS_TOKEN_COOKIE,
                    access_token,
                    max_age=7 * 24 * 3600,
                    httponly=True,
                    samesite="lax",
                    secure=request.url.scheme == "https",
                    path="/",
                )
            return response
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
        "active_sessions": len(registry),
        "timestamp": datetime.now().isoformat()
    }


# ----- Routes（按资源域挂载；files 域含 catch-all 路径，放在 config/memory 之后避免吞前缀） -----
app.include_router(workspace_router)
app.include_router(chat_router)
app.include_router(ask_user_router)
app.include_router(config_router)
app.include_router(update_router)
app.include_router(files_router)
app.include_router(memory_router)
app.include_router(qq_router)
app.include_router(wechat_router)


if __name__ == "__main__":
    # 直接运行本模块时的便捷入口：只绑 localhost。
    # 需要对外监听请走 `python main.py --host ...`——那条路径会先执行
    # _check_web_auth 强制要求 access_token，此处绕开它，故不得绑非 localhost。
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
