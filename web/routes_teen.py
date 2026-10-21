"""青少年模式 HTTP API：状态、开关、解锁、策略、审计、报表。

权限模型（设计 §3.4）：
- ``/status`` 公开——前端灰显需要；不含密码哈希等敏感信息
- ``/enable`` 仅未开启时公开；``/unlock`` ``/lock`` 公开（解锁本身要密码）
- 其余端点需全局解锁窗口（``teen_guard("teen_admin")``）

密码错误 → 401；连续错误触发限流（TeenLockedOut → 429）；硬禁/受限
工作区 → TeenModeDenied（web_api 全局异常处理器转 403）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.config import find_workspace_entry, load_workspaces_index
from core.teen_mode import (
    SCOPE_GLOBAL,
    TeenLockedOut,
    TeenModeDenied,
    get_teen_mode,
)
from web.deps import _csrf_protect, teen_guard

logger = logging.getLogger(__name__)

router = APIRouter()


class TeenEnableRequest(BaseModel):
    password: str
    policy: dict | None = None
    limits: dict | None = None
    window_minutes: int | None = None


class TeenUnlockRequest(BaseModel):
    password: str
    scope: str = SCOPE_GLOBAL
    minutes: int | None = None


class TeenLockRequest(BaseModel):
    scope: str | None = None


class TeenDisableRequest(BaseModel):
    password: str


class TeenPolicyRequest(BaseModel):
    policy: dict | None = None
    limits: dict | None = None
    window_minutes: int | None = None


class TeenPasswordRequest(BaseModel):
    old_password: str
    new_password: str


def _validate_allowed_workspaces(uuids: list) -> list[str]:
    """白名单校验：uuid 必须存在、排除 system、目录不得互相嵌套（§3.13）。"""
    if not isinstance(uuids, list):
        raise HTTPException(status_code=400, detail="allowed_workspaces 必须为列表")
    cleaned: list[str] = []
    for uuid in uuids:
        if not isinstance(uuid, str) or not uuid:
            raise HTTPException(status_code=400, detail="allowed_workspaces 含非法条目")
        if uuid == "system":
            raise HTTPException(status_code=400, detail="system 工作区永远受限，不能加入白名单")
        if not find_workspace_entry(uuid):
            raise HTTPException(status_code=400, detail=f"工作区不存在: {uuid}")
        if uuid not in cleaned:
            cleaned.append(uuid)

    def _dir(u: str) -> str:
        entry = find_workspace_entry(u) or {}
        return str(entry.get("directory") or "").rstrip("\\/")

    import os

    for a in cleaned:
        dir_a = os.path.normcase(os.path.abspath(_dir(a)))
        for b in load_workspaces_index():
            uuid_b = b.get("uuid", "")
            if uuid_b == a or not b.get("directory"):
                continue
            dir_b = os.path.normcase(os.path.abspath(str(b["directory"]).rstrip("\\/")))
            if dir_a and dir_b and dir_a != dir_b:
                # 允许 A 的目录包含 B（无论 B 是否在白名单）→ A 内可用相对路径进入 B。
                # 跨盘符（D: vs E:）不存在嵌套关系，commonpath 会抛 ValueError，跳过即可
                try:
                    common = os.path.commonpath([dir_a, dir_b])
                except ValueError:
                    continue
                if os.path.normcase(common) == dir_a:
                    raise HTTPException(
                        status_code=400,
                        detail=(f"白名单工作区目录互相嵌套：{_dir(a)} 包含 "
                                f"{b['directory']}，前者内可用相对路径进入后者，拒绝保存"),
                    )
    return cleaned


@router.get("/api/teen/status")
async def teen_status():
    """公开状态：前端灰显、徽章与解锁倒计时。"""
    tm = get_teen_mode()
    data = tm.status()
    data["unlocked"] = tm.is_unlocked()
    return data


@router.post("/api/teen/enable")
async def teen_enable(request: Request, body: TeenEnableRequest):
    """开启青少年模式并设置家长密码（仅未开启时）。"""
    _csrf_protect(request)
    tm = get_teen_mode()
    if tm.is_enabled():
        raise HTTPException(status_code=409, detail="青少年模式已开启")
    if body.policy and "allowed_workspaces" in (body.policy or {}):
        body.policy["allowed_workspaces"] = _validate_allowed_workspaces(
            body.policy.get("allowed_workspaces") or [])
    try:
        tm.enable(body.password, policy=body.policy, limits=body.limits,
                  window_minutes=body.window_minutes or 0)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "status": tm.status()}


@router.post("/api/teen/unlock")
async def teen_unlock(request: Request, body: TeenUnlockRequest):
    """验证家长密码，开启解锁窗口（scope: `*` 或 `ws:<uuid>`）。"""
    _csrf_protect(request)
    tm = get_teen_mode()
    if not tm.is_enabled():
        raise HTTPException(status_code=409, detail="青少年模式未开启")
    try:
        until = tm.unlock(body.password, scope=body.scope or SCOPE_GLOBAL,
                          minutes=body.minutes,
                          ip=request.client.host if request.client else "")
    except TeenLockedOut as e:
        raise HTTPException(status_code=429, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "unlock_until": until, "scope": body.scope or SCOPE_GLOBAL}


@router.post("/api/teen/lock")
async def teen_lock(request: Request, body: TeenLockRequest):
    """立即结束解锁窗口（scope=None 清空全部作用域）。"""
    _csrf_protect(request)
    tm = get_teen_mode()
    tm.lock(scope=body.scope, ip=request.client.host if request.client else "")
    return {"success": True}


@router.post("/api/teen/disable",
             dependencies=[Depends(teen_guard("teen_admin"))])
async def teen_disable(request: Request, body: TeenDisableRequest):
    """关闭青少年模式（需全局解锁 + 密码双确认）。"""
    _csrf_protect(request)
    tm = get_teen_mode()
    try:
        tm.disable(body.password, source="web")
    except TeenModeDenied:
        raise  # 需解锁窗口：交全局处理器转 403（带「请先解锁」文案）
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return {"success": True}


@router.put("/api/teen/policy",
            dependencies=[Depends(teen_guard("teen_admin"))])
async def teen_update_policy(request: Request, body: TeenPolicyRequest):
    """更新策略 / 时长限制（需全局解锁）。"""
    _csrf_protect(request)
    tm = get_teen_mode()
    if not tm.is_enabled():
        raise HTTPException(status_code=409, detail="青少年模式未开启")
    if body.policy and "allowed_workspaces" in body.policy:
        body.policy["allowed_workspaces"] = _validate_allowed_workspaces(
            body.policy.get("allowed_workspaces") or [])
    try:
        tm.update_policy(policy=body.policy, limits=body.limits,
                         window_minutes=body.window_minutes)
    except TeenModeDenied:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "status": tm.status()}


@router.post("/api/teen/password",
             dependencies=[Depends(teen_guard("teen_admin"))])
async def teen_change_password(request: Request, body: TeenPasswordRequest):
    """修改家长密码（需全局解锁 + 原密码）。"""
    _csrf_protect(request)
    tm = get_teen_mode()
    try:
        tm.change_password(body.old_password, body.new_password)
    except TeenModeDenied:
        raise
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True}


@router.get("/api/teen/audit",
            dependencies=[Depends(teen_guard("teen_admin"))])
async def teen_audit(limit: int = 200):
    """审计记录（需全局解锁）。"""
    tm = get_teen_mode()
    return {"audit": tm.read_audit(limit=min(max(1, limit), 1000))}


@router.get("/api/teen/report",
            dependencies=[Depends(teen_guard("teen_admin"))])
async def teen_report(days: int = 7):
    """用量报表（需全局解锁）：今日时长/回合/拦截 + 近 N 天拦截分布。"""
    tm = get_teen_mode()
    return tm.usage_report(days=min(max(1, days), 30))
