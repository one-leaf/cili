"""本机访问判定：localhost 白名单与依赖式守卫。

只信任 ``request.client.host``（真实 TCP 连接 IP），不看可伪造的 Host 头。
反代部署下该值是代理 IP——依赖本机判定的端点需在反代层单独封禁。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

LOCALHOST_IPS = frozenset({"127.0.0.1", "::1", "localhost"})


def client_ip_of(request: Request | None) -> str:
    return request.client.host if request is not None and request.client else ""


def require_localhost(request: Request | None, detail: str) -> str:
    """非本机访问抛 403；返回客户端 IP 供审计。"""
    ip = client_ip_of(request)
    if ip not in LOCALHOST_IPS:
        raise HTTPException(status_code=403, detail=detail)
    return ip


ACCESS_TOKEN_COOKIE = "cili_access_token"


def ip_matches(client_ip: str, allowed: list[str]) -> bool:
    """精确 IP 或 CIDR 前缀匹配（W18）。非 IP 字符串退化为精确匹配。"""
    import ipaddress

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


async def check_access_control(request, call_next, load_config):
    """访问控制判定：令牌优先；未配置令牌时按 localhost / IP 白名单放行。

    只信任 ``request.client.host``（真实 TCP 连接 IP），不校验可伪造的 Host 头。
    ``load_config`` 由调用方注入（便于测试替换）。
    """
    import hmac
    import logging

    from fastapi.responses import JSONResponse

    logger = logging.getLogger("web.access")
    client_ip = client_ip_of(request)

    try:
        config = load_config()
    except Exception:
        # 配置未就绪（例如还没有 API key）：只放行本机，便于打开 UI 配置
        if client_ip in LOCALHOST_IPS:
            return await call_next(request)
        logger.warning("Config not ready, blocked access from %s", client_ip)
        return JSONResponse(status_code=403, content={"detail": "Server not configured yet"})

    access_token = config.system.access_token or ""
    if access_token:
        header_token = request.headers.get("X-Access-Token") or ""
        query_token = request.query_params.get("token") or ""
        cookie_token = request.cookies.get(ACCESS_TOKEN_COOKIE) or ""
        provided = header_token or query_token or cookie_token
        if provided and hmac.compare_digest(provided, access_token):
            response = await call_next(request)
            # ?token= 首次鉴权成功即下发 HttpOnly cookie，页面刷新/静态资源
            # 不再依赖 URL 携带令牌，收敛 Referer/浏览器历史泄露面
            if query_token and hmac.compare_digest(query_token, access_token):
                response.set_cookie(
                    ACCESS_TOKEN_COOKIE,
                    access_token,
                    max_age=7 * 24 * 3600,
                    httponly=True,
                    samesite="lax",
                    secure=request.url.scheme == "https",
                    path="/",
                )
            return response
        logger.warning("Blocked access from %s: missing/invalid access_token", client_ip)
        return JSONResponse(
            status_code=403, content={"detail": "Access denied: invalid or missing token"}
        )

    if client_ip in LOCALHOST_IPS:
        return await call_next(request)

    if ip_matches(client_ip, config.system.allowed_ips or []):
        return await call_next(request)

    logger.warning("Blocked access from %s", client_ip)
    return JSONResponse(status_code=403, content={"detail": "Access denied: IP not allowed"})


def install_access_control(app, load_config) -> None:
    """注册 Web 访问控制中间件（绑定配置加载器）。"""

    @app.middleware("http")
    async def _middleware(request, call_next):
        return await check_access_control(request, call_next, load_config)
