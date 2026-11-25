"""工作区路径存储测试：项目内相对、项目外绝对、默认根解析与一次性迁移。"""

from __future__ import annotations

import json
import os

import pytest

import core.config as config_mod
import core.workspaces as workspaces_mod
from core.paths import resolve_storage_path, to_storage_path


@pytest.fixture
def project(tmp_path, monkeypatch):
    """隔离项目根 / setting.json / workspaces.json / 根目录缓存。"""
    root = tmp_path / "cili"
    root.mkdir()
    (root / "data").mkdir()
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", root)
    monkeypatch.setattr(config_mod, "DATA_DIR", root / "data")
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", root / "data" / "setting.json")
    monkeypatch.setattr(config_mod, "WORKSPACES_JSON", root / "data" / "workspaces.json")
    monkeypatch.delenv("CILI_WORKSPACES_ROOT", raising=False)
    config_mod.reset_workspaces_root_cache()
    yield root
    config_mod.reset_workspaces_root_cache()


def test_to_storage_path_inside_and_outside(project, tmp_path):
    inside = project / "workspaces" / "default"
    assert to_storage_path(inside, root=project) == "workspaces/default"

    outside = tmp_path / "external"
    assert to_storage_path(outside, root=project) == str(outside)


def test_to_storage_path_keeps_relative(project):
    assert to_storage_path("workspaces/default", root=project) == "workspaces/default"


def test_resolve_storage_path(project, tmp_path):
    assert resolve_storage_path("workspaces/default", root=project) == (
        project / "workspaces" / "default"
    )
    assert resolve_storage_path(str(tmp_path / "x"), root=project) == tmp_path / "x"

    with pytest.raises(ValueError):
        resolve_storage_path("../escape", root=project)


def test_index_saves_relative_and_loads_absolute(project, tmp_path):
    index = project / "data" / "workspaces.json"
    inside = project / "workspaces" / "default"
    outside = tmp_path / "external"
    entries = [
        {"uuid": "a", "workspace_name": "Default", "directory": str(inside)},
        {"uuid": "b", "workspace_name": "ext", "directory": str(outside)},
    ]

    assert workspaces_mod.save_index(index, entries, root=project)

    raw = json.loads(index.read_text(encoding="utf-8"))
    assert raw[0]["directory"] == "workspaces/default"
    assert raw[1]["directory"] == str(outside)

    loaded = workspaces_mod.load_index(index, root=project)
    assert loaded[0]["directory"] == str(inside)
    assert loaded[1]["directory"] == str(outside)


def test_upsert_entry_roundtrip(project):
    index = project / "data" / "workspaces.json"
    entry = {
        "uuid": "a",
        "workspace_name": "Default",
        "directory": str(project / "workspaces" / "default"),
    }
    assert workspaces_mod.upsert_entry(index, entry, root=project)

    raw = json.loads(index.read_text(encoding="utf-8"))
    assert raw[0]["directory"] == "workspaces/default"

    found = workspaces_mod.find_entry(index, "a", root=project)
    assert found is not None
    assert found["directory"] == str(project / "workspaces" / "default")


def test_workspaces_root_env_override(project, tmp_path, monkeypatch):
    target = tmp_path / "override"
    monkeypatch.setenv("CILI_WORKSPACES_ROOT", str(target))
    config_mod.reset_workspaces_root_cache()
    assert config_mod.workspaces_root() == target


def test_workspaces_root_from_relative_config(project):
    config_mod.GLOBAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_mod.GLOBAL_CONFIG_PATH.write_text(
        json.dumps({"workspaces": {"root": "workspaces"}}), encoding="utf-8"
    )
    config_mod.reset_workspaces_root_cache()
    assert config_mod.workspaces_root() == project / "workspaces"


def test_workspaces_root_legacy_fallback(project):
    (project / "workspace").mkdir()
    config_mod.reset_workspaces_root_cache()
    assert config_mod.workspaces_root() == project / "workspace"


def test_workspaces_root_default_without_legacy(project):
    config_mod.reset_workspaces_root_cache()
    assert config_mod.workspaces_root() == project / "workspaces"


def test_migration_rewrites_project_paths(project):
    from core.migrations.steps import migrate_workspace_paths_relative

    config_mod.WORKSPACES_JSON.parent.mkdir(parents=True, exist_ok=True)
    inside = project / "workspaces" / "default"
    outside = project.parent / "external"
    config_mod.WORKSPACES_JSON.write_text(
        json.dumps([
            {"uuid": "a", "directory": str(inside)},
            {"uuid": "b", "directory": str(outside)},
        ]),
        encoding="utf-8",
    )
    config_mod.GLOBAL_CONFIG_PATH.write_text(
        json.dumps({"workspaces": {"root": str(project / "workspaces")}}),
        encoding="utf-8",
    )

    assert migrate_workspace_paths_relative(
        config_mod.WORKSPACES_JSON, config_mod.GLOBAL_CONFIG_PATH
    )

    raw = json.loads(config_mod.WORKSPACES_JSON.read_text(encoding="utf-8"))
    assert raw[0]["directory"] == "workspaces/default"
    assert raw[1]["directory"] == str(outside)
    config = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["workspaces"]["root"] == "workspaces"

    # 幂等：再跑一次不改变任何值
    assert migrate_workspace_paths_relative(
        config_mod.WORKSPACES_JSON, config_mod.GLOBAL_CONFIG_PATH
    )
    raw2 = json.loads(config_mod.WORKSPACES_JSON.read_text(encoding="utf-8"))
    assert raw2 == raw


def test_ensure_default_workspace_uses_root(project, monkeypatch):
    monkeypatch.setattr(config_mod, "_init_workspace_git_quietly", lambda uuid: None)
    config_mod.reset_workspaces_root_cache()

    uuid = config_mod.ensure_default_workspace()
    assert uuid

    raw = json.loads(config_mod.WORKSPACES_JSON.read_text(encoding="utf-8"))
    entry = next(item for item in raw if item.get("uuid") == uuid)
    assert entry["directory"] == "workspaces/default"

    expected = project / "workspaces" / "default" / ".cili"
    assert config_mod.get_workspace_data_dir(uuid) == expected
    assert expected.is_dir()


def test_updater_excludes_configured_root(project, monkeypatch):
    from core import updater

    config_mod.GLOBAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_mod.GLOBAL_CONFIG_PATH.write_text(
        json.dumps({"workspaces": {"root": "my-workspaces"}}), encoding="utf-8"
    )
    config_mod.reset_workspaces_root_cache()
    config_mod.upsert_workspace_entry({
        "uuid": "custom-ws",
        "workspace_name": "custom",
        "directory": str(project / "custom-ws"),
    })

    dirs = updater._current_exclude_dirs()
    assert "my-workspaces" in dirs
    assert "custom-ws" in dirs
    assert "workspace" in dirs and "workspaces" in dirs and "data" in dirs


def test_drive_relative_rejected_on_windows_only(project):
    if os.name != "nt":
        pytest.skip("Windows 盘符相对路径")
    with pytest.raises(ValueError):
        resolve_storage_path("D:foo", root=project)
    with pytest.raises(ValueError):
        to_storage_path("D:foo", root=project)