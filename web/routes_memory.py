"""Memory 管理（v3 记忆）路由域。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.config import get_workspace_data_dir, load_workspace_config, save_workspace_config
from core.memory_pipeline import memory_enabled, run_consolidation
from core.memory_store import MemoryStore, Journal

from web.deps import _csrf_protect, _SAFE_ID_RE, _validate_workspace_uuid

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------- Models ----------

class UpdateMemoryEntryRequest(BaseModel):
    """更新记忆条目（title/description/tags/content 均可选）。"""
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    content: str | None = None


class MemorySettingsRequest(BaseModel):
    """记忆功能开关。"""
    memory_enabled: bool


def _memory_dir(workspace_uuid: str) -> Path:
    _validate_workspace_uuid(workspace_uuid)
    md = get_workspace_data_dir(workspace_uuid) / "memory"
    if not md.is_dir():
        raise HTTPException(status_code=404, detail="该工作区尚未启用记忆")
    return md


def _validate_memory_name(name: str) -> None:
    if not _SAFE_ID_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid memory entry name")


@router.get("/api/workspaces/{workspace_uuid}/memory")
async def list_memory(workspace_uuid: str, type: str = "", status: str = "", q: str = ""):
    """记忆总览：统计 + 条目列表（可按 type/status/关键词过滤）+ 待整合数 + 开关。"""
    _validate_workspace_uuid(workspace_uuid)
    md = get_workspace_data_dir(workspace_uuid) / "memory"
    if not md.is_dir():
        return {"enabled": memory_enabled(workspace_uuid),
                "stats": {"total": 0, "archived": 0, "stale": 0,
                          "by_type": {"fact": 0, "preference": 0, "skill": 0, "reference": 0},
                          "index_lines": 0, "index_bytes": 0},
                "entries": [], "pending": 0}
    store = MemoryStore(md)
    try:
        entries = store.list(type_=type or None, status=status or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if q:
        ql = q.lower()
        entries = [
            e for e in entries
            if ql in str(e.get("name", "")).lower()
            or ql in str(e.get("title", "")).lower()
            or ql in str(e.get("description", "")).lower()
            or ql in str(e.get("tags", [])).lower()
        ]
    journal = Journal(md)
    return {
        "enabled": memory_enabled(workspace_uuid),
        "stats": store.stat(),
        "entries": entries[:500],
        "pending": journal.pending_count(),
    }


@router.get("/api/workspaces/{workspace_uuid}/memory/entries/{name}")
async def get_memory_entry(workspace_uuid: str, name: str):
    """查看条目全文（只读，不递增 usage_count）。"""
    _validate_memory_name(name)
    md = _memory_dir(workspace_uuid)
    try:
        fm, body = MemoryStore(md).peek(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"entry": fm, "body": body}


@router.put("/api/workspaces/{workspace_uuid}/memory/entries/{name}")
async def update_memory_entry(workspace_uuid: str, name: str, request: UpdateMemoryEntryRequest, request_raw: Request = None):
    """编辑条目字段。"""
    _csrf_protect(request_raw)
    _validate_memory_name(name)
    md = _memory_dir(workspace_uuid)
    store = MemoryStore(md)
    try:
        store.update(
            name,
            title=request.title,
            description=request.description,
            tags=request.tags,
            content=request.content,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.post("/api/workspaces/{workspace_uuid}/memory/entries/{name}/archive")
async def archive_memory_entry(workspace_uuid: str, name: str, request_raw: Request = None):
    """归档条目（移出索引，不再参与检索）。"""
    _csrf_protect(request_raw)
    _validate_memory_name(name)
    md = _memory_dir(workspace_uuid)
    try:
        MemoryStore(md).archive(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.post("/api/workspaces/{workspace_uuid}/memory/entries/{name}/restore")
async def restore_memory_entry(workspace_uuid: str, name: str, request_raw: Request = None):
    """从归档恢复条目。"""
    _csrf_protect(request_raw)
    _validate_memory_name(name)
    md = _memory_dir(workspace_uuid)
    try:
        MemoryStore(md).restore(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.post("/api/workspaces/{workspace_uuid}/memory/entries/{name}/delete")
async def delete_memory_entry(workspace_uuid: str, name: str, request_raw: Request = None):
    """永久删除条目（不可恢复）。"""
    _csrf_protect(request_raw)
    _validate_memory_name(name)
    md = _memory_dir(workspace_uuid)
    try:
        MemoryStore(md).delete(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@router.post("/api/workspaces/{workspace_uuid}/memory/consolidate")
async def consolidate_memory(workspace_uuid: str, request_raw: Request = None):
    """手动触发记忆整合（journal → 条目）。"""
    _csrf_protect(request_raw)
    _memory_dir(workspace_uuid)
    # 手动整合一次清空待整合队列（每批 limit=20，最多 4 批）
    result = await asyncio.to_thread(run_consolidation, workspace_uuid, max_batches=4)
    return result


@router.put("/api/workspaces/{workspace_uuid}/memory/settings")
async def update_memory_settings(workspace_uuid: str, request: MemorySettingsRequest, request_raw: Request = None):
    """开/关工作区记忆功能（提取钩子 + cron 整合都受此开关控制）。"""
    _csrf_protect(request_raw)
    _validate_workspace_uuid(workspace_uuid)
    cfg = load_workspace_config(workspace_uuid)
    if not cfg:
        raise HTTPException(status_code=404, detail="Workspace not found")
    cfg["memory_enabled"] = request.memory_enabled
    save_workspace_config(workspace_uuid, cfg)
    return {"ok": True, "memory_enabled": request.memory_enabled}
