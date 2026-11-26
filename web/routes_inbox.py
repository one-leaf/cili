"""收件箱路由：全局通知的查看、已读、删除与恢复。

收件箱是全局数据；青少年模式按工作区白名单做读侧过滤（见
``_allowed_workspace_uuids``）。所有端点在过滤后返回，受限条目一律 404，
不通过错误码暴露存在性。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from core.inbox import (
    ITEM_ID_RE,
    STATUSES,
    InboxItem,
    get_inbox_store,
)
from web.deps import csrf_protect

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 200


def _allowed_workspace_uuids() -> frozenset[str] | None:
    """青少年模式下的可见工作区集合；未开启返回 None（不过滤）。"""
    from core.teen_mode import get_teen_mode

    teen = get_teen_mode()
    if not teen.is_enabled():
        return None

    from web.deps import _list_all_workspaces

    allowed = {
        str(ws.get("uuid") or "")
        for ws in _list_all_workspaces()
        if teen.workspace_access(str(ws.get("uuid") or "")) == "allow"
    }
    # 空 workspace_uuid 的全局条目沿用 teen_mode 的「空工作区 = allow」规则
    allowed.add("")
    return frozenset(allowed)


def _visible(item: InboxItem, allowed: frozenset[str] | None) -> bool:
    return allowed is None or item.workspace_uuid in allowed


def _get_visible(item_id: str, allowed: frozenset[str] | None) -> InboxItem:
    if not ITEM_ID_RE.match(item_id or ""):
        raise HTTPException(status_code=404, detail="通知不存在")
    item = get_inbox_store().get(item_id)
    if item is None or not _visible(item, allowed):
        raise HTTPException(status_code=404, detail="通知不存在")
    return item


def _counts(allowed: frozenset[str] | None) -> tuple[int, int]:
    store = get_inbox_store()
    unread = store.unread_count(allowed_workspace_uuids=allowed)
    total = store.list(
        status="active", allowed_workspace_uuids=allowed, limit=1
    )[1]
    return unread, total


@router.get("/api/inbox")
async def list_inbox(
    status: str = "active",
    workspace_uuid: str = "",
    limit: int = 50,
    offset: int = 0,
):
    if status not in STATUSES:
        raise HTTPException(status_code=400, detail="无效的通知状态")
    size = min(max(int(limit or 50), 1), MAX_PAGE_SIZE)
    start = max(int(offset or 0), 0)
    allowed = _allowed_workspace_uuids()
    items, total = get_inbox_store().list(
        status=status,
        workspace_uuid=workspace_uuid,
        allowed_workspace_uuids=allowed,
        limit=size,
        offset=start,
    )
    unread, _ = _counts(allowed)
    return {
        "items": [item.to_dict(include_body=False) for item in items],
        "total": total,
        "unread_count": unread,
        "limit": size,
        "offset": start,
    }


@router.get("/api/inbox/count")
async def inbox_count():
    unread, total = _counts(_allowed_workspace_uuids())
    return {"unread_count": unread, "total": total}


@router.get("/api/inbox/{item_id}")
async def get_inbox_item(item_id: str):
    allowed = _allowed_workspace_uuids()
    item = _get_visible(item_id, allowed)
    return {"item": item.to_dict(include_body=True)}


@router.post("/api/inbox/{item_id}/read")
async def mark_inbox_read(item_id: str, _: None = Depends(csrf_protect)):
    allowed = _allowed_workspace_uuids()
    item = _get_visible(item_id, allowed)
    get_inbox_store().mark_read([item.id])
    unread, _ = _counts(allowed)
    return {
        "success": True,
        "item": _get_visible(item_id, allowed).to_dict(include_body=True),
        "unread_count": unread,
    }


@router.post("/api/inbox/{item_id}/unread")
async def mark_inbox_unread(item_id: str, _: None = Depends(csrf_protect)):
    allowed = _allowed_workspace_uuids()
    item = _get_visible(item_id, allowed)
    get_inbox_store().mark_unread([item.id])
    unread, _ = _counts(allowed)
    return {
        "success": True,
        "item": _get_visible(item_id, allowed).to_dict(include_body=True),
        "unread_count": unread,
    }


@router.post("/api/inbox/read-all")
async def mark_inbox_all_read(
    payload: dict[str, Any] | None = Body(default=None),
    _: None = Depends(csrf_protect),
):
    body = payload if isinstance(payload, dict) else {}
    workspace_uuid = str(body.get("workspace_uuid") or "")
    allowed = _allowed_workspace_uuids()
    changed = get_inbox_store().mark_all_read(
        workspace_uuid=workspace_uuid,
        allowed_workspace_uuids=allowed,
    )
    unread, _ = _counts(allowed)
    return {"success": True, "changed": changed, "unread_count": unread}


@router.delete("/api/inbox/{item_id}")
async def delete_inbox_item(
    item_id: str,
    permanent: bool = False,
    _: None = Depends(csrf_protect),
):
    allowed = _allowed_workspace_uuids()
    item = _get_visible(item_id, allowed)
    if permanent:
        if not item.deleted_at:
            raise HTTPException(status_code=400, detail="只能彻底删除回收站中的通知")
        get_inbox_store().delete_permanent([item.id])
    else:
        get_inbox_store().delete([item.id])
    unread, _ = _counts(allowed)
    return {"success": True, "permanent": bool(permanent), "unread_count": unread}


@router.post("/api/inbox/{item_id}/restore")
async def restore_inbox_item(item_id: str, _: None = Depends(csrf_protect)):
    allowed = _allowed_workspace_uuids()
    item = _get_visible(item_id, allowed)
    get_inbox_store().restore([item.id])
    unread, _ = _counts(allowed)
    return {
        "success": True,
        "item": _get_visible(item_id, allowed).to_dict(include_body=True),
        "unread_count": unread,
    }
