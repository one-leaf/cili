"""邮件接入端路由：状态与拦截统计（按账号）。

与 ``routes_wechat`` 的差别：邮件没有扫码/登录流程（凭证在 ``setting.json``，
配置读写走 ``routes_config``），所以这里只有一个只读状态端点。

**拦截统计是产品的一部分**（设计 §三 3.9）：第一次配置后用户就能看到「30 多封
被拦了」——既是安全感，也是白名单配置是否正确的反馈。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from interfaces.email.config import load_email_accounts
from interfaces.email.service import email_status_snapshot
from interfaces.email.cursor import ignored_summary

router = APIRouter()


@router.get("/api/email/status")
async def email_status() -> dict[str, Any]:
    """接入端状态（账号列表）：运行态、最近轮询、错误与 24h 拦截统计。**不含凭证**。"""
    accounts: list[dict[str, Any]] = []
    for cfg in load_email_accounts():
        snapshot = email_status_snapshot(cfg.id)
        summary = ignored_summary(cfg.id)
        accounts.append({
            "id": cfg.id,
            "label": cfg.label,
            "enabled": cfg.enabled,
            "email_address": cfg.email_address,
            "configured": cfg.is_ready(),
            "allowed_senders": list(cfg.allowed_senders),
            "running": snapshot["running"],
            "last_poll_at": snapshot["last_poll_at"],
            "last_error": snapshot["last_error"],
            "ignored_24h": {
                "total": summary["total"],
                "by_reason": summary["by_reason"],
            },
            "recent_ignored": summary["recent"],
        })
    return {"accounts": accounts}
