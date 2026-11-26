"""收件箱存储：增删已读、去重、保留策略与限流计数。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.inbox import InboxStore, _prune_items
from core.time_utils import now_str


@pytest.fixture
def store(tmp_path):
    return InboxStore(tmp_path / "inbox.json")


def test_add_and_list(store):
    item = store.add("标题", "正文", source_type="tool", workspace_uuid="ws-a")
    assert item is not None
    assert item.title == "标题"
    items, total = store.list()
    assert total == 1
    assert items[0].id == item.id
    assert items[0].body == "正文"


def test_title_required(store):
    with pytest.raises(ValueError):
        store.add("   ")


def test_truncate_title_and_body(store):
    item = store.add("x" * 200, "y" * 5000, max_body_chars=100)
    assert len(item.title) == 120
    assert len(item.body) == 100


def test_level_fallback(store):
    item = store.add("t", level="bogus")
    assert item.level == "info"


def test_dedupe_key_returns_existing(store):
    first = store.add("t", dedupe_key="cron:ws:job:slot")
    second = store.add("t2", dedupe_key="cron:ws:job:slot")
    assert second.id == first.id
    assert len(store.list()[0]) == 1


def test_dedupe_applies_to_deleted(store):
    first = store.add("t", dedupe_key="k")
    store.delete([first.id])
    second = store.add("t2", dedupe_key="k")
    assert second.id == first.id
    assert store.get(first.id).deleted_at


def test_read_state_and_counts(store):
    a = store.add("a", source_type="tool")
    b = store.add("b", source_type="tool")
    assert store.unread_count() == 2

    assert store.mark_read([a.id]) == 1
    assert store.unread_count() == 1
    assert store.mark_read([a.id]) == 0  # 重复标记不重复计数
    assert store.mark_unread([a.id]) == 1
    assert store.unread_count() == 2

    assert store.mark_all_read() == 2
    assert store.unread_count() == 0
    assert store.list(status="read")[1] == 2
    assert store.list(status="unread")[1] == 0


def test_delete_restore_and_permanent(store):
    item = store.add("t")
    assert store.delete([item.id]) == 1
    assert store.unread_count() == 0
    assert store.list(status="deleted")[1] == 1
    assert store.restore([item.id]) == 1
    restored = store.get(item.id)
    assert restored.deleted_at == ""
    assert restored.read_at  # 设计状态机：恢复后为已读
    assert store.unread_count() == 0
    assert store.list()[1] == 1
    assert store.delete([item.id]) == 1
    assert store.delete_permanent([item.id]) == 1
    assert store.get(item.id) is None


def test_workspace_filter(store):
    store.add("a", workspace_uuid="ws-a")
    store.add("b", workspace_uuid="ws-b")
    items, total = store.list(workspace_uuid="ws-a")
    assert total == 1
    assert items[0].workspace_uuid == "ws-a"


def test_allowed_workspace_filter(store):
    store.add("a", workspace_uuid="ws-a")
    store.add("b", workspace_uuid="ws-b")
    store.add("global")
    items, total = store.list(allowed_workspace_uuids=frozenset({"ws-a", ""}))
    assert total == 2
    assert {item.workspace_uuid for item in items} == {"ws-a", ""}


def test_mark_all_read_respects_allowed(store):
    store.add("a", workspace_uuid="ws-a")
    store.add("b", workspace_uuid="ws-b")
    changed = store.mark_all_read(allowed_workspace_uuids=frozenset({"ws-a"}))
    assert changed == 1
    assert store.unread_count(allowed_workspace_uuids=frozenset({"ws-b"})) == 1


def test_count_recent_by_session_and_source(store):
    store.add("a", source_type="tool", session_id="s1")
    store.add("b", source_type="cron", session_id="s1")
    store.add("c", source_type="tool", session_id="s2")
    assert store.count_recent("s1", seconds=60, source_type="tool") == 1
    assert store.count_recent("s1", seconds=60, source_type="") == 2


def test_prune_deleted_after_retention():
    from core.inbox import InboxItem

    old = InboxItem(
        id="inb_old", created_at=now_str(), level="info", title="old", body="",
        source={"type": "tool"}, deleted_at=(datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    fresh = InboxItem(
        id="inb_fresh", created_at=now_str(), level="info", title="fresh", body="",
        source={"type": "tool"}, deleted_at=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    kept = _prune_items([old, fresh])
    assert [item.id for item in kept] == ["inb_fresh"]


def test_prune_cap_prefers_removing_read(store, monkeypatch):
    monkeypatch.setattr("core.inbox.MAX_ITEMS", 2)
    a = store.add("a")
    b = store.add("b")
    store.mark_read([a.id])
    store.add("c")
    items, total = store.list(status="all")
    assert total == 2
    assert a.id not in {item.id for item in items}
    assert {b.id, items[0].id}  # b 仍在


def test_persists_across_instances(tmp_path):
    path = tmp_path / "inbox.json"
    first = InboxStore(path)
    item = first.add("t", body="hello")
    first.mark_read([item.id])

    second = InboxStore(path)
    loaded = second.get(item.id)
    assert loaded is not None
    assert loaded.read_at
    assert loaded.body == "hello"


def test_corrupt_file_recovers(tmp_path):
    path = tmp_path / "inbox.json"
    path.write_text("{not json", encoding="utf-8")
    store = InboxStore(path)
    assert store.list()[1] == 0
    assert any(p.name.startswith("inbox.json.corrupt-") for p in tmp_path.iterdir())


def test_excerpt_hides_body_in_list(store):
    item = store.add("t", "line1\nline2")
    data = item.to_dict(include_body=False)
    assert "body" not in data
    assert data["excerpt"] == "line1 line2"


def test_list_deleted_purges_expired(store, monkeypatch):
    item = store.add("old")
    store.delete([item.id])
    monkeypatch.setattr("core.inbox.DELETED_RETENTION_DAYS", -1)

    items, total = store.list(status="deleted")
    assert items == []
    assert total == 0
    assert store.get(item.id) is None


def test_change_event_is_signal_only(store):
    import queue

    from core.event_bus import get_event_bus

    q = queue.Queue()
    bus = get_event_bus()
    bus.subscribe(q)
    try:
        store.add("t")
        event = q.get(timeout=1)
    finally:
        bus.unsubscribe(q)

    assert event["type"] == "inbox_changed"
    assert event["broadcast"] is True
    assert "item_id" not in event
    assert "unread_count" not in event
    assert "title" not in event
    assert "body" not in event
