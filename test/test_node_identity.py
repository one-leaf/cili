"""节点身份：生成、持久化、改名、重新生成与环境变量。"""

from __future__ import annotations

import json
import re

import core.node_identity as ni


def test_ensure_generates_and_reuses(tmp_path, monkeypatch):
    path = tmp_path / "node.json"
    monkeypatch.setattr(ni, "node_path", lambda: path)

    first = ni.ensure_node_identity()
    assert re.fullmatch(r"node-[0-9a-f]{12}", first["id"])
    assert first["name"]
    assert path.is_file()
    assert ni.get_node_id() == first["id"]
    assert ni.get_node_name() == first["name"]

    second = ni.ensure_node_identity()
    assert second == first


def test_env_is_published(tmp_path, monkeypatch):
    path = tmp_path / "node.json"
    monkeypatch.setattr(ni, "node_path", lambda: path)
    monkeypatch.delenv(ni.ENV_NODE_ID, raising=False)
    monkeypatch.delenv(ni.ENV_NODE_NAME, raising=False)

    identity = ni.ensure_node_identity()

    assert ni.os.environ[ni.ENV_NODE_ID] == identity["id"]
    assert ni.os.environ[ni.ENV_NODE_NAME] == identity["name"]


def test_update_name_keeps_id(tmp_path, monkeypatch):
    path = tmp_path / "node.json"
    monkeypatch.setattr(ni, "node_path", lambda: path)
    identity = ni.ensure_node_identity()

    updated = ni.update_node_name(" 书房\n电脑 ")

    assert updated["id"] == identity["id"]
    assert updated["name"] == "书房 电脑"
    assert ni.get_node_name() == "书房 电脑"


def test_regenerate_changes_id_keeps_name(tmp_path, monkeypatch):
    path = tmp_path / "node.json"
    monkeypatch.setattr(ni, "node_path", lambda: path)
    first = ni.ensure_node_identity()

    second = ni.regenerate_node_identity()

    assert second["id"] != first["id"]
    assert second["name"] == first["name"]
    assert ni.get_node_id() == second["id"]


def test_corrupt_file_is_repaired(tmp_path, monkeypatch):
    path = tmp_path / "node.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(ni, "node_path", lambda: path)

    identity = ni.ensure_node_identity()

    assert re.fullmatch(r"node-[0-9a-f]{12}", identity["id"])
    assert json.loads(path.read_text(encoding="utf-8"))["id"] == identity["id"]
