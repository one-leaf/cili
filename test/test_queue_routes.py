"""队列路由端点测试：直接调用异步端点函数（仓库无 TestClient 约定）。"""

import asyncio
import secrets
from pathlib import Path

import pytest
from fastapi import HTTPException

import web.routes_chat as routes_chat
from core.turn_queue import QueuedTurn
from core.turn_queue import queue as turn_queue


def _key():
    return f"ws-{secrets.token_hex(3)}:sess-{secrets.token_hex(3)}"


@pytest.fixture(autouse=True)
def _no_pump(monkeypatch):
    """端点内的后台泵会起线程跑回合；路由测试只验证入队语义，替换为空操作。"""
    monkeypatch.setattr(routes_chat, "_start_pump", lambda key: None)


def _enqueue(key, content="hi"):
    return turn_queue.enqueue(key, QueuedTurn(item_id="", content=content, user_input=content))


class TestQueueEndpoints:
    def test_get_queue_returns_snapshot(self):
        key = _key()
        ws, sid = key.split(":")
        _enqueue(key)
        try:
            data = asyncio.run(routes_chat.get_queue(ws, sid, ws_dir=Path(".")))
        finally:
            turn_queue.remove_session(key)
        assert data["success"] is True
        assert data["items"][0]["content"] == "hi"
        assert data["paused"] is False

    def test_delete_item(self):
        key = _key()
        ws, sid = key.split(":")
        item = _enqueue(key)
        try:
            data = asyncio.run(routes_chat.delete_queue_item(ws, sid, item.item_id, ws_dir=Path(".")))
        finally:
            turn_queue.remove_session(key)
        assert data["success"] is True

    def test_delete_rejects_bad_item_id(self):
        key = _key()
        ws, sid = key.split(":")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(routes_chat.delete_queue_item(ws, sid, "../evil", ws_dir=Path(".")))
        assert exc.value.status_code == 400
        turn_queue.remove_session(key)

    def test_clear_queue(self):
        key = _key()
        ws, sid = key.split(":")
        _enqueue(key, "a")
        try:
            data = asyncio.run(routes_chat.clear_queue(ws, sid, ws_dir=Path(".")))
        finally:
            turn_queue.remove_session(key)
        assert data["cleared"] == 1

    def test_resume_clears_pause(self):
        key = _key()
        ws, sid = key.split(":")
        turn_queue.pause(key, "stopped")
        try:
            asyncio.run(routes_chat.resume_queue(ws, sid, ws_dir=Path(".")))
            assert turn_queue.is_paused(key) is False
        finally:
            turn_queue.remove_session(key)

    def test_enqueue_rejects_empty(self, tmp_path, teen):
        ws, sid = _key().split(":")
        (tmp_path / "sessions" / sid).mkdir(parents=True)
        req = routes_chat.QueueRequest(content="   ")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(routes_chat.enqueue_message(ws, sid, req, ws_dir=tmp_path))
        assert exc.value.status_code == 400

    def test_enqueue_returns_item(self, tmp_path, teen):
        key = _key()
        ws, sid = key.split(":")
        (tmp_path / "sessions" / sid).mkdir(parents=True)
        req = routes_chat.QueueRequest(content="排队")
        try:
            data = asyncio.run(routes_chat.enqueue_message(ws, sid, req, ws_dir=tmp_path))
        finally:
            turn_queue.remove_session(key)
        assert data["success"] is True
        assert data["item"]["content"] == "排队"
