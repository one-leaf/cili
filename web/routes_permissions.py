"""权限配置与规则管理 API。

- 配置读写 ``setting.json`` 顶层 ``permissions`` 字段（不放进 system）；
- 规则 CRUD 读写全局/工作区 ``approval_rules.json``（decision=allow/deny/ask）；
- 变更后刷新缓存 runner，下一动作生效。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.config import (
    DATA_DIR,
    PermissionConfig,
    get_workspace_data_dir,
    load_global_config,
    load_workspaces_index,
    save_global_config,
)
from core.security.permission_rules import PermissionRules, new_rule_id
from web.deps import _csrf_protect, _require_workspace, registry, teen_guard

logger = logging.getLogger(__name__)
router = APIRouter()

_DEFAULT_GLOBAL_RULES = DATA_DIR / "cili" / "approval_rules.json"


class PermissionUpdateRequest(BaseModel):
    permissions: dict


class RuleCreateRequest(BaseModel):
    decision: str = "allow"
    tool: str = "*"
    kind: str = "command"
    match: dict
    note: str = ""
    enabled: bool = True


class RuleUpdateRequest(BaseModel):
    decision: str | None = None
    tool: str | None = None
    kind: str | None = None
    match: dict | None = None
    note: str | None = None
    enabled: bool | None = None


def _global_rules_path(cfg: PermissionConfig):
    return cfg.global_rules_path or str(_DEFAULT_GLOBAL_RULES)


def _rules_store(scope: str) -> PermissionRules:
    raw = load_global_config()
    cfg = PermissionConfig.from_dict(raw.get("permissions") or {})
    global_path = _global_rules_path(cfg)
    if scope == "global":
        return PermissionRules(None, global_path=global_path, hot_reload=False)
    if scope == "system":
        raise HTTPException(status_code=400, detail="system 工作区不开放权限规则管理")
    _require_workspace(scope)  # 存在性 + 青少年工作区守卫
    path = get_workspace_data_dir(scope) / "approval_rules.json"
    return PermissionRules(path, global_path=global_path, hot_reload=False)


def _reload_all_runners() -> None:
    with registry.lock:
        cached = list(registry.items())
    for key, runner in cached:
        try:
            runner.reload_config()
        except Exception as e:  # noqa: BLE001 —— 单个 runner 失败不影响其他
            logger.warning(f"[permissions] runner {key} 重载失败: {e}")


@router.get("/api/permissions")
async def get_permissions():
    raw = load_global_config()
    cfg = PermissionConfig.from_dict(raw.get("permissions") or {})
    workspaces = [
        {"uuid": entry.get("uuid", ""), "name": entry.get("workspace_name", "")}
        for entry in load_workspaces_index()
        if entry.get("uuid") and entry.get("uuid") != "system"
    ]
    return {
        "permissions": cfg.to_dict(),
        "builtin_rules": PermissionRules.builtin_rules(),
        "readonly_groups": {k: list(v) for k, v in PermissionRules.readonly_groups().items()},
        "workspaces": workspaces,
        "rules_scopes": ["global"] + [w["uuid"] for w in workspaces],
    }


@router.put("/api/permissions",
            dependencies=[Depends(teen_guard("config_update"))])
async def update_permissions(request: Request, body: PermissionUpdateRequest):
    _csrf_protect(request)
    cfg = PermissionConfig.from_dict(body.permissions or {})
    raw = load_global_config()
    raw["permissions"] = cfg.to_dict()
    if not save_global_config(raw):
        raise HTTPException(status_code=500, detail="保存权限配置失败")
    await asyncio.to_thread(_reload_all_runners)
    return {"success": True, "permissions": cfg.to_dict()}


@router.get("/api/permissions/rules")
async def list_permission_rules(scope: str = "global", decision: str = "all"):
    store = _rules_store(scope)
    rules = store.all_rules(scope)
    if decision in ("allow", "deny", "ask"):
        rules = [r for r in rules if r.get("decision") == decision]
    return {
        "scope": scope,
        "rules": rules,
        "builtin_rules": PermissionRules.builtin_rules() if decision in ("all", "deny") else [],
    }


@router.post("/api/permissions/rules",
             dependencies=[Depends(teen_guard("config_update"))])
async def create_permission_rule(request: Request, scope: str = "global",
                                 body: RuleCreateRequest | None = None):
    _csrf_protect(request)
    if body is None:
        raise HTTPException(status_code=400, detail="缺少规则内容")
    store = _rules_store(scope)
    rule = {
        "id": new_rule_id(),
        "enabled": bool(body.enabled),
        "decision": body.decision,
        "source": "user",
        "tool": body.tool or "*",
        "kind": body.kind,
        "match": body.match,
        "note": body.note,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hits": 0,
        "last_used_at": "",
    }
    saved = store.add_rule(rule, scope)
    if saved is None:
        raise HTTPException(status_code=400, detail="规则不合法（检查 decision/kind/match）")
    await asyncio.to_thread(_reload_all_runners)
    return {"success": True, "rule": saved}


@router.patch("/api/permissions/rules/{rule_id}",
              dependencies=[Depends(teen_guard("config_update"))])
async def update_permission_rule(rule_id: str, request: Request,
                                 scope: str = "global",
                                 body: RuleUpdateRequest | None = None):
    _csrf_protect(request)
    if body is None:
        raise HTTPException(status_code=400, detail="缺少规则内容")
    updates = body.model_dump(exclude_none=True)
    store = _rules_store(scope)
    saved = store.update_rule(rule_id, updates, scope)
    if saved is None:
        raise HTTPException(status_code=404, detail="规则不存在或不可修改")
    await asyncio.to_thread(_reload_all_runners)
    return {"success": True, "rule": saved}


@router.delete("/api/permissions/rules/{rule_id}",
               dependencies=[Depends(teen_guard("config_update"))])
async def delete_permission_rule(rule_id: str, request: Request, scope: str = "global"):
    _csrf_protect(request)
    store = _rules_store(scope)
    if not store.delete_rule(rule_id, scope):
        raise HTTPException(status_code=404, detail="规则不存在或不可删除")
    await asyncio.to_thread(_reload_all_runners)
    return {"success": True}


@router.post("/api/permissions/rules/revoke-ai",
             dependencies=[Depends(teen_guard("config_update"))])
async def revoke_ai_rules(request: Request, scope: str = "global"):
    _csrf_protect(request)
    store = _rules_store(scope)
    removed = store.revoke_judge_rules(scope)
    await asyncio.to_thread(_reload_all_runners)
    return {"success": True, "removed": removed}
