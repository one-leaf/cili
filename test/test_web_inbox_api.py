"""收件箱 Web API：列表、已读、删除/恢复与路由参数。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from web.routes_inbox import (
    delete_inbox_item,
    get_inbox_item,
    inbox_count,
    list_inbox,
    mark_inbox_all_read,
    mark_inbox_read,
    mark_inbox_unread,
    restore_inbox_item,
)


def _run(coro):
    return asyncio.run(coro)


def test_list_and_count(inbox_store):
    inbox_store.add("标题", "正文内容", source_type="tool", workspace_uuid="ws-a")
    data = _run(list_inbox())
    assert data["total"] == 1
    assert data["unread_count"] == 1
    assert "body" not in data["items"][0]
    assert data["items"][0]["excerpt"] == "正文内容"

    count = _run(inbox_count())
    assert count == {"unread_count": 1, "total": 1}


def test_detail_returns_body(inbox_store):
    item = inbox_store.add("标题", "正文内容")
    data = _run(get_inbox_item(item.id))
    assert data["item"]["body"] == "正文内容"


def test_read_and_unread(inbox_store):
    item = inbox_store.add("标题")
    data = _run(mark_inbox_read(item.id))
    assert data["success"] is True
    assert data["unread_count"] == 0
    assert inbox_store.get(item.id).read_at

    data = _run(mark_inbox_unread(item.id))
    assert data["unread_count"] == 1
    assert inbox_store.get(item.id).read_at == ""


def test_read_all_with_workspace_filter(inbox_store):
    inbox_store.add("a", workspace_uuid="ws-a")
    inbox_store.add("b", workspace_uuid="ws-b")
    data = _run(mark_inbox_all_read({"workspace_uuid": "ws-a"}))
    assert data["changed"] == 1
    assert inbox_store.unread_count() == 1


def test_delete_restore_and_permanent(inbox_store):
    item = inbox_store.add("标题")
    data = _run(delete_inbox_item(item.id))
    assert data["success"] is True
    assert inbox_store.list(status="deleted")[1] == 1

    data = _run(restore_inbox_item(item.id))
    assert data["success"] is True
    assert inbox_store.list()[1] == 1

    _run(delete_inbox_item(item.id))
    data = _run(delete_inbox_item(item.id, permanent=True))
    assert data["permanent"] is True
    assert inbox_store.get(item.id) is None


def test_permanent_requires_deleted(inbox_store):
    item = inbox_store.add("标题")
    with pytest.raises(HTTPException) as exc:
        _run(delete_inbox_item(item.id, permanent=True))
    assert exc.value.status_code == 400


def test_invalid_status_and_missing_item(inbox_store):
    with pytest.raises(HTTPException) as exc:
        _run(list_inbox(status="bogus"))
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _run(get_inbox_item("inb_missing"))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        _run(get_inbox_item("not-an-id"))
    assert exc.value.status_code == 404
