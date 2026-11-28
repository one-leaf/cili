"""会话归属：创建时写入、判定、列表/删除校验。"""

from __future__ import annotations

from pathlib import Path

import core.session_owner as so
from core.session import SessionStore


def _node(node_id="node-aaaaaaaaaaaa", name="本机"):
    return {"id": node_id, "name": name}


def test_owner_metadata_uses_node_and_user(monkeypatch):
    monkeypatch.setattr(so, "get_node_identity", lambda: _node())
    monkeypatch.setattr(so, "_org_user", lambda: ("u1", "张三"))
    meta = so.owner_metadata()
    assert meta["owner_node_id"] == "node-aaaaaaaaaaaa"
    assert meta["owner_user_id"] == "u1"
    assert meta["owner_user_label"] == "张三"


def test_is_mine_user_or_node_or_unknown(monkeypatch):
    monkeypatch.setattr(so, "get_node_identity", lambda: _node())
    monkeypatch.setattr(so, "_org_user", lambda: ("u1", "张三"))
    assert so.is_mine({"owner_node_id": "node-aaaaaaaaaaaa"}) is True
    assert so.is_mine({"owner_user_id": "u1", "owner_node_id": "node-other"}) is True
    assert so.is_mine({"owner_node_id": "node-other"}) is False
    assert so.is_mine({}) is None


def test_create_session_writes_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(so, "get_node_identity", lambda: _node())
    monkeypatch.setattr(so, "_org_user", lambda: ("u1", "张三"))
    store = SessionStore.create_new_session(tmp_path, "S")
    assert store.metadata["owner_node_id"] == "node-aaaaaaaaaaaa"
    assert store.metadata["owner_user_id"] == "u1"


def test_owner_fields_for_list(monkeypatch):
    monkeypatch.setattr(so, "get_node_identity", lambda: _node())
    monkeypatch.setattr(so, "_org_user", lambda: ("u1", "张三"))
    fields = so.owner_fields({"owner_node_id": "node-other", "owner_node_name": "别人"})
    assert fields["owner_mine"] is False
    assert fields["owner_node_name"] == "别人"
