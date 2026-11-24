"""本机访问判定：localhost 白名单与依赖式守卫。

只信任 ``request.client.host``（真实 TCP 连接 IP），不看可伪造的 Host 头。
反代部署下该值是代理 IP——依赖本机判定的端点需在反代层单独封禁。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

LOCALHOST_IPS = frozenset({"127.0.0.1", "::1", "localhost"})


def client_ip(request: Request | None) -> str:
    return request.client.host if request is not None and request.client else ""


def is_localhost(request: Request | None) -> bool:
    return client_ip(request) in LOCALHOST_IPS


def require_localhost(request: Request | None, detail: str) -> str:
    """非本机访问抛 403；返回客户端 IP 供审计。"""
    ip = client_ip(request)
    if ip not in LOCALHOST_IPS:
        raise HTTPException(status_code=403, detail=detail)
    return ip