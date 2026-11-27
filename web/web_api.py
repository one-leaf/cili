"""Web server for Cili - provides HTTP API and serves static files.

按资源域拆分后的装配入口：app 创建、中间件、静态文件、路由挂载。
路由实现分散在 web/routes_*.py，共享状态与 helper 在 web/deps.py。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.app_context import app_context
from core.config import PROJECT_ROOT, load_config
from core.teen_mode import TeenLockedOut, TeenModeDenied, get_teen_mode
from web.access_control import check_access_control as _check_access_control
from web.access_control import install_access_control
from web.deps import WEB_DIR, registry
from web.routes_ask_user import router as ask_user_router
from web.routes_chat import router as chat_router
from web.routes_chatgpt_oauth import router as siwc_router
from web.routes_config import router as config_router
from web.routes_courseware import router as courseware_router
from web.routes_cron import router as cron_router
from web.routes_email import router as email_router
from web.routes_files import router as files_router
from web.routes_inbox import router as inbox_router
from web.routes_memory import router as memory_router
from web.routes_org import router as org_router
from web.routes_permissions import router as permissions_router
from web.routes_qq import router as qq_router
from web.routes_session_about import router as session_about_router
from web.routes_suggest import router as suggest_router
from web.routes_teen import router as teen_router
from web.routes_textbooks import router as textbooks_router
from web.routes_update import router as update_router
from web.routes_wechat import router as wechat_router
from web.routes_workflows import router as workflows_router
from web.routes_workspace import router as workspace_router
from web.routes_workspace_wizard import router as workspace_wizard_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 - 启动/优雅关闭各全局单例（统一由 AppContext 持有）。"""
    # Web 接入端初始化（默认工作区 + 全局配置自检）。原先在 import web.deps 时
    # 隐式执行，改为在生命周期入口显式调用（幂等）。
    import web.profile  # noqa: F401 —— 显式注册 Web 能力档案
    from web.deps import init_web_deps
    init_web_deps()

    from web.routes_chatgpt_oauth import register_hooks
    register_hooks()

    app_context.startup()
    yield
    from core.textbooks import textbook_tasks
    await textbook_tasks.shutdown_all()
    app_context.shutdown()


# Initialize FastAPI app with lifespan
app = FastAPI(title="Cili Web API", version="1.0.0", lifespan=lifespan)

# 访问控制（令牌 / localhost / IP 白名单）统一装配


async def check_access_control(request, call_next):
    """模块级入口（测试直接调用/打补丁）；读取本模块的 load_config。"""
    return await _check_access_control(request, call_next, load_config)


# 青少年模式：核心层抛出的拒绝统一转 HTTP 响应（数据层断言 / runner 池兜底 /
# 时长限制都没有路由上下文，在此收敛）
@app.exception_handler(TeenModeDenied)
async def _teen_denied_handler(request: Request, exc: TeenModeDenied):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(TeenLockedOut)
async def _teen_locked_handler(request: Request, exc: TeenLockedOut):
    return JSONResponse(status_code=429, content={"detail": str(exc)})

# Add CORS middleware - restrict to localhost by default for security
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# 访问控制必须在 CORS 之后注册：Starlette 里「最后添加的中间件在最外层」，
# 否则预检 OPTIONS 会先被 CORS 放行、绕过令牌校验。
install_access_control(app, load_config)


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


_SHARE_BLOCKED_HTML = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<title>链接不可用</title></head>"
    "<body style='font-family:sans-serif;display:flex;align-items:center;"
    "justify-content:center;height:100vh;margin:0;'>"
    "<p>该链接在青少年模式下不可用。</p></body></html>"
)


def _teen_share_blocked() -> bool:
    """deny_share 策略生效且当前未解锁（分享页是把内容带出系统的出口）。"""
    tm = get_teen_mode()
    return tm.is_enabled() and bool(tm.policy().get("deny_share")) and not tm.is_unlocked()


@app.get("/s/{workspace_uuid}/{session_id}")
async def session_view(workspace_uuid: str, session_id: str):
    """独立会话查看页（完整会话模式）。"""
    if _teen_share_blocked():
        return HTMLResponse(_SHARE_BLOCKED_HTML, status_code=403)
    response = FileResponse(str(WEB_DIR / "static" / "session.html"))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/s/{workspace_uuid}/{session_id}/{msg_ids}")
async def session_message_view(workspace_uuid: str, session_id: str, msg_ids: str):
    """独立会话查看页（指定消息模式，msg_ids 为逗号分隔的消息 ID）。"""
    if _teen_share_blocked():
        return HTMLResponse(_SHARE_BLOCKED_HTML, status_code=403)
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
app.include_router(workspace_wizard_router)
app.include_router(chat_router)
app.include_router(session_about_router)
app.include_router(ask_user_router)
app.include_router(suggest_router)
app.include_router(workflows_router)
app.include_router(config_router)
app.include_router(siwc_router)
app.include_router(permissions_router)
app.include_router(org_router)
app.include_router(update_router)
app.include_router(teen_router)
app.include_router(textbooks_router)
app.include_router(cron_router)
app.include_router(inbox_router)
app.include_router(files_router)
app.include_router(courseware_router)
app.include_router(memory_router)
app.include_router(qq_router)
app.include_router(wechat_router)
app.include_router(email_router)


if __name__ == "__main__":
    # 直接运行本模块时的便捷入口：只绑 localhost。
    # 需要对外监听请走 `python main.py --host ...`——那条路径会先执行
    # _check_web_auth 强制要求 access_token，此处绕开它，故不得绑非 localhost。
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=1618)
