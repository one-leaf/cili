"""TurnQueue 单元测试：FIFO、幂等、暂停/恢复、上限、合并批次与预算。

设计文档：workspace/default/cili-input-queue-plan-2026-10-29.md §5.2。
"""

import threading

import pytest

import core.turn_queue as tq_mod
from core.turn_queue import (
    MAX_ITEMS,
    MAX_TEXT,
    QueuedTurn,
    QueueError,
    TurnQueue,
    merge_queued_turns,
    new_item_id,
)

KEY = "ws:sess"


def _item(content="x", client_id=None, image_data=None, interface="web"):
    user_input = content
    if image_data:
        user_input = [
            {"type": "text", "text": content, "_valid": True},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": image_data}, "_valid": True},
        ]
    return QueuedTurn(
        item_id=new_item_id(),
        content=content,
        user_input=user_input,
        interface=interface,
        client_id=client_id,
        has_images=bool(image_data),
    )


@pytest.fixture
def q():
    return TurnQueue()


class TestFifoAndSnapshot:
    def test_drain_preserves_fifo_order(self, q):
        for text in ("a", "b", "c"):
            q.enqueue(KEY, _item(text))
        assert [it.content for it in q.drain_batch(KEY)] == ["a", "b", "c"]

    def test_drain_empty_returns_empty(self, q):
        assert q.drain_batch(KEY) == []

    def test_snapshot_preview_omits_payload(self, q):
        q.enqueue(KEY, _item("hello", image_data="QUJD"))
        snap = q.snapshot(KEY)
        item = snap["items"][0]
        assert item["content"] == "hello"
        assert item["has_images"] is True
        assert "user_input" not in item

    def test_snapshot_truncates_long_preview(self, q):
        q.enqueue(KEY, _item("字" * 500))
        assert q.snapshot(KEY)["items"][0]["truncated"] is True


class TestIdempotencyAndCrud:
    def test_same_client_id_returns_original(self, q):
        first = q.enqueue(KEY, _item("a", client_id="c1"))
        second = q.enqueue(KEY, _item("b", client_id="c1"))
        assert second is first
        assert [it["content"] for it in q.snapshot(KEY)["items"]] == ["a"]

    def test_remove_item(self, q):
        item = q.enqueue(KEY, _item("a"))
        assert q.remove(KEY, item.item_id) is True
        assert q.snapshot(KEY)["items"] == []
        assert q.remove(KEY, "q_missing") is False

    def test_clear(self, q):
        for text in ("a", "b"):
            q.enqueue(KEY, _item(text))
        assert q.clear(KEY) == 2
        assert q.snapshot(KEY)["items"] == []

    def test_remove_session(self, q):
        q.enqueue(KEY, _item("a"))
        q.remove_session(KEY)
        assert q.snapshot(KEY)["items"] == []

    def test_sessions_are_isolated(self, q):
        q.enqueue("ws:A", _item("a"))
        q.enqueue("ws:B", _item("b"))
        assert [it.content for it in q.drain_batch("ws:A")] == ["a"]
        assert [it.content for it in q.drain_batch("ws:B")] == ["b"]


class TestPauseSemantics:
    def test_paused_drain_returns_empty_and_keeps_items(self, q):
        q.enqueue(KEY, _item("a"))
        q.pause(KEY, "stopped")
        assert q.drain_batch(KEY) == []
        assert len(q.snapshot(KEY)["items"]) == 1
        assert q.snapshot(KEY)["paused"] is True
        assert q.snapshot(KEY)["reason"] == "stopped"

    def test_resume_allows_drain(self, q):
        q.enqueue(KEY, _item("a"))
        q.pause(KEY, "stopped")
        q.resume(KEY)
        assert q.snapshot(KEY)["paused"] is False
        assert [it.content for it in q.drain_batch(KEY)] == ["a"]

    def test_enqueue_while_paused_resumes(self, q):
        """设计 §3.4：暂停状态直接发新消息 = 恢复。"""
        q.pause(KEY, "stopped")
        q.enqueue(KEY, _item("a"))
        assert q.snapshot(KEY)["paused"] is False
        assert [it.content for it in q.drain_batch(KEY)] == ["a"]


class TestLimits:
    def test_item_count_cap(self, q):
        for i in range(MAX_ITEMS):
            q.enqueue(KEY, _item(str(i)))
        with pytest.raises(QueueError):
            q.enqueue(KEY, _item("overflow"))

    def test_single_text_cap(self, q):
        with pytest.raises(QueueError):
            q.enqueue(KEY, _item("字" * (MAX_TEXT + 1)))

    def test_image_bytes_cap(self, q, monkeypatch):
        monkeypatch.setattr(tq_mod, "MAX_IMAGE_BYTES", 64)
        with pytest.raises(QueueError):
            q.enqueue(KEY, _item("图", image_data="A" * 128))


class TestDrainBudget:
    def test_takes_oldest_batch_within_budget(self, q):
        for text in ("a" * 100, "b" * 100, "c" * 100):
            q.enqueue(KEY, _item(text))
        batch = q.drain_batch(KEY, max_text=250)
        assert [it.content[:1] for it in batch] == ["a", "b"]
        rest = q.drain_batch(KEY)
        assert [it.content[:1] for it in rest] == ["c"]

    def test_single_over_budget_item_still_delivered(self, q):
        q.enqueue(KEY, _item("x" * 400))
        assert len(q.drain_batch(KEY, max_text=100)) == 1

    def test_concurrent_enqueue_no_loss(self, q):
        threads = 8
        per_thread = 2

        def worker(idx):
            for j in range(per_thread):
                q.enqueue(KEY, _item(f"{idx}-{j}"))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        drain = q.drain_batch(KEY)
        assert len(drain) == threads * per_thread
        assert len({it.item_id for it in drain}) == len(drain)


class TestMergeQueuedTurns:
    def test_text_items_joined_with_blank_line(self):
        items = [_item("a"), _item("b")]
        user_input, meta = merge_queued_turns(items)
        assert user_input == "a\n\nb"
        assert meta["merged_count"] == 2
        assert meta["queued_ids"] == [it.item_id for it in items]
        assert meta["queued_interfaces"] == ["web", "web"]

    def test_images_keep_item_order(self):
        items = [
            _item("first", image_data="AAA"),
            _item("", image_data="BBB"),
            _item("third"),
        ]
        user_input, meta = merge_queued_turns(items)
        assert isinstance(user_input, list)
        types = [b["type"] for b in user_input]
        assert types == ["text", "image", "image", "text"]
        assert user_input[0]["text"] == "first"
        data = [b["source"]["data"] for b in user_input if b["type"] == "image"]
        assert data == ["AAA", "BBB"]
        assert user_input[-1]["text"] == "third"
        assert meta["merged_count"] == 3
