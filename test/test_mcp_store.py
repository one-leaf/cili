"""工作区 MCP 配置存储与全局配置迁移测试。"""

import json
import secrets
from pathlib import Path

import pytest

import core.config as config_mod
from core.config import MCPConfig
from core.mcp_store import (
    load_mcp_servers,
    mcp_dir,
    save_mcp_servers,
    validate_server_name,
)
from core.migrations.steps import migrate_mcp_servers_to_workspace


def _register_workspace(tmp_path: Path) -> str:
    """注册一个指向临时目录的工作区，返回 uuid。"""
    ws_uuid = f"mcp-{secrets.token_hex(4)}"
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    config_mod.upsert_workspace_entry({
        "uuid": ws_uuid,
        "workspace_name": ws_uuid,
        "directory": str(ws_dir),
    })
    return ws_uuid


def _cfg(url: str = "http://localhost:3000/mcp") -> MCPConfig:
    return MCPConfig(url=url, headers={"X-API-Key": "secret"}, tool_timeout=5)


def test_save_load_roundtrip(tmp_path):
    ws_uuid = _register_workspace(tmp_path)
    save_mcp_servers(ws_uuid, {"alpha": _cfg(), "beta": _cfg("http://localhost:4000/mcp")})

    directory = mcp_dir(ws_uuid)
    assert (directory / "alpha.json").is_file()
    assert (directory / "beta.json").is_file()

    servers = load_mcp_servers(ws_uuid)
    assert set(servers) == {"alpha", "beta"}
    assert servers["alpha"].url == "http://localhost:3000/mcp"
    assert servers["alpha"].headers == {"X-API-Key": "secret"}


def test_save_removes_deleted_servers(tmp_path):
    ws_uuid = _register_workspace(tmp_path)
    save_mcp_servers(ws_uuid, {"alpha": _cfg(), "beta": _cfg()})
    save_mcp_servers(ws_uuid, {"beta": _cfg()})

    assert not (mcp_dir(ws_uuid) / "alpha.json").exists()
    assert set(load_mcp_servers(ws_uuid)) == {"beta"}


def test_load_skips_invalid_files(tmp_path):
    ws_uuid = _register_workspace(tmp_path)
    directory = mcp_dir(ws_uuid)
    directory.mkdir(parents=True)
    (directory / "good.json").write_text('{"url": "http://x/mcp"}', encoding="utf-8")
    (directory / "broken.json").write_text("{not json", encoding="utf-8")
    (directory / "list.json").write_text("[]", encoding="utf-8")

    assert set(load_mcp_servers(ws_uuid)) == {"good"}


def test_missing_dir_returns_empty(tmp_path):
    ws_uuid = _register_workspace(tmp_path)
    assert load_mcp_servers(ws_uuid) == {}


def test_validate_and_save_reject_bad_names(tmp_path):
    assert validate_server_name("filesystem") is None
    assert validate_server_name("my.server_2") is None
    for bad in ("", ".hidden", "../escape", "a/b", "a b", "-lead", "x" * 65):
        assert validate_server_name(bad) is not None

    ws_uuid = _register_workspace(tmp_path)
    with pytest.raises(ValueError):
        save_mcp_servers(ws_uuid, {"../escape": _cfg()})


def test_migrate_global_mcp_to_default_workspace(tmp_path):
    config_path = tmp_path / "setting.json"
    config_path.write_text(json.dumps({
        "system": {"pip_mirror": ""},
        "mcp_servers": {"kfb": {"url": "http://kfb/mcp", "headers": {"X-API-Key": "k"}}},
    }), encoding="utf-8")
    workspaces_json = tmp_path / "workspaces.json"
    workspaces_json.write_text(json.dumps([
        {"uuid": "ws-default", "workspace_name": "Default", "directory": str(tmp_path / "default")},
    ]), encoding="utf-8")

    data_dir = tmp_path / "default" / ".cili"
    get_data_dir = lambda uuid: data_dir  # noqa: E731

    assert migrate_mcp_servers_to_workspace(config_path, workspaces_json, get_data_dir) is True

    moved = json.loads((data_dir / "mcp" / "kfb.json").read_text(encoding="utf-8"))
    assert moved["url"] == "http://kfb/mcp"
    assert "mcp_servers" not in json.loads(config_path.read_text(encoding="utf-8"))
    # 幂等：第二次不再执行
    assert migrate_mcp_servers_to_workspace(config_path, workspaces_json, get_data_dir) is False


def test_migration_keeps_workspace_version(tmp_path):
    config_path = tmp_path / "setting.json"
    config_path.write_text(json.dumps({
        "mcp_servers": {"kfb": {"url": "http://global/mcp"}},
    }), encoding="utf-8")
    workspaces_json = tmp_path / "workspaces.json"
    workspaces_json.write_text(json.dumps([
        {"uuid": "ws-default", "workspace_name": "Default", "directory": str(tmp_path / "default")},
    ]), encoding="utf-8")

    data_dir = tmp_path / "default" / ".cili"
    (data_dir / "mcp").mkdir(parents=True)
    (data_dir / "mcp" / "kfb.json").write_text('{"url": "http://workspace/mcp"}', encoding="utf-8")

    migrate_mcp_servers_to_workspace(config_path, workspaces_json, lambda uuid: data_dir)

    kept = json.loads((data_dir / "mcp" / "kfb.json").read_text(encoding="utf-8"))
    assert kept["url"] == "http://workspace/mcp"
    assert "mcp_servers" not in json.loads(config_path.read_text(encoding="utf-8"))
