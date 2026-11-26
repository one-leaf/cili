"""青少年模式：收件箱按工作区白名单过滤。"""

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
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def teen_mode(isolated_teen):
    isolated_teen.enable("secret123", policy={"allowed_workspaces": ["test-uuid"]})
    return isolated_teen


def test_list_and_count_are_filtered(inbox_store, teen_mode):
    visible = inbox_store.add("可见", workspace_uuid="test-uuid")
    inbox_store.add("受限", workspace_uuid="test")
    global_item = inbox_store.add("全局", workspace_uuid="")

    data = _run(list_inbox())
    assert {item["id"] for item in data["items"]} == {visible.id, global_item.id}
    assert data["total"] == 2
    assert data["unread_count"] == 2

    assert _run(inbox_count())["total"] == 2


def test_locked_item_is_404(inbox_store, teen_mode):
    locked = inbox_store.add("受限", workspace_uuid="test")
    with pytest.raises(HTTPException) as exc:
        _run(get_inbox_item(locked.id))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        _run(mark_inbox_read(locked.id))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        _run(delete_inbox_item(locked.id))
    assert exc.value.status_code == 404


def test_read_all_only_affects_visible(inbox_store, teen_mode):
    visible = inbox_store.add("可见", workspace_uuid="test-uuid")
    locked = inbox_store.add("受限", workspace_uuid="test")

    data = _run(mark_inbox_all_read(None))
    assert data["changed"] == 1
    assert inbox_store.get(visible.id).read_at
    assert inbox_store.get(locked.id).read_at == ""

    # 受限条目仍保留未读状态，只是当前不可见
    assert inbox_store.unread_count(
        allowed_workspace_uuids=frozenset({"test-uuid", ""})
    ) == 0
    assert inbox_store.unread_count(
        allowed_workspace_uuids=frozenset({"test"})
    ) == 1
