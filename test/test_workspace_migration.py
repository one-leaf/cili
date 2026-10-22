"""默认工作区迁移测试：workspace/ 根 → workspace/default/。

覆盖触发条件（旧根条目存在才迁移）、隐藏项全部搬移、独立工作区保护、
目标位置占用（空骨架接管 / 真实数据中止）、同名冲突不覆盖、幂等与
索引待修复兜底。
"""

from pathlib import Path

import pytest

from core import config as config_mod
from core.workspace_migration import migrate_default_workspace

LEGACY_UUID = "d1b45267"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离的 PROJECT_ROOT / DATA_DIR / workspaces.json，workspace 根预建。"""
    root = tmp_path / "proj"
    ws = root / "workspace"
    ws.mkdir(parents=True)
    data_dir = root / "data" / "cili"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", root)
    monkeypatch.setattr(config_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(config_mod, "WORKSPACES_JSON", data_dir / "workspaces.json")
    return root, ws


def _write_index(entries: list[dict]) -> None:
    assert config_mod.save_workspaces_index(entries)


def _legacy(ws: Path) -> dict:
    return {
        "uuid": LEGACY_UUID,
        "workspace_name": "Default",
        "directory": str(ws),
        "git_enabled": False,
        "memory_enabled": False,
    }


def _populate_root(ws: Path) -> None:
    (ws / "notes.md").write_text("root", encoding="utf-8")
    (ws / ".hidden").write_text("h", encoding="utf-8")
    (ws / ".cili" / "sessions").mkdir(parents=True)
    (ws / ".cili" / "sessions" / "s1.json").write_text("{}", encoding="utf-8")
    (ws / ".git" / "objects").mkdir(parents=True)
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/master", encoding="utf-8")
    (ws / "node_modules" / "pkg").mkdir(parents=True)


def test_noop_without_legacy_entry(env):
    """旧根条目不存在（全新安装/已迁移）→ 什么都不做。"""
    root, ws = env
    _write_index([{"uuid": "aaaa1111", "workspace_name": "Other", "directory": str(root / "elsewhere")}])
    assert migrate_default_workspace() is None
    assert not (ws / "default").exists()
    assert list(config_mod.load_workspaces_index())[0]["uuid"] == "aaaa1111"


def test_moves_all_content_including_hidden(env):
    """基础迁移：根下所有内容（含 .git/.cili/隐藏文件）搬入 default/，索引改指新目录。"""
    _root, ws = env
    _populate_root(ws)
    _write_index([_legacy(ws)])

    uuid = migrate_default_workspace()
    assert uuid == LEGACY_UUID

    default_dir = ws / "default"
    assert (default_dir / "notes.md").read_text(encoding="utf-8") == "root"
    assert (default_dir / ".hidden").read_text(encoding="utf-8") == "h"
    assert (default_dir / ".cili" / "sessions" / "s1.json").exists()
    assert (default_dir / ".git" / "HEAD").exists()
    assert (default_dir / "node_modules" / "pkg").exists()
    # 根目录只剩 default/
    assert [p.name for p in ws.iterdir()] == ["default"]

    entry = config_mod.find_workspace_entry(LEGACY_UUID)
    assert Path(entry["directory"]) == default_dir


def test_merge_into_existing_target_without_clobber(env):
    """目标已存在（脚本先行创建过）：内容并入，同名文件改名让位，不覆盖。"""
    _root, ws = env
    _populate_root(ws)
    default_dir = ws / "default"
    default_dir.mkdir()
    (default_dir / "stray.md").write_text("stray", encoding="utf-8")
    (default_dir / "notes.md").write_text("target", encoding="utf-8")
    _write_index([_legacy(ws)])

    assert migrate_default_workspace() == LEGACY_UUID

    assert (default_dir / "stray.md").read_text(encoding="utf-8") == "stray"
    assert (default_dir / "notes.md").read_text(encoding="utf-8") == "target"
    renamed = list(default_dir.glob("notes.md.from-root-*"))
    assert len(renamed) == 1
    assert renamed[0].read_text(encoding="utf-8") == "root"
    assert [p.name for p in ws.iterdir()] == ["default"]


def test_registered_subworkspace_stays_in_place(env):
    """已注册的独立工作区子目录原地保留，不随迁移搬入 default/。"""
    _root, ws = env
    _populate_root(ws)
    myapp = ws / "myapp"
    myapp.mkdir()
    (myapp / "app.py").write_text("x", encoding="utf-8")
    _write_index([
        _legacy(ws),
        {"uuid": "bbbb2222", "workspace_name": "myapp", "directory": str(myapp)},
    ])

    assert migrate_default_workspace() == LEGACY_UUID

    assert (ws / "myapp" / "app.py").exists()
    assert not (ws / "default" / "myapp").exists()
    assert (ws / "default" / "notes.md").exists()
    assert Path(config_mod.find_workspace_entry("bbbb2222")["directory"]) == myapp


def test_duplicate_default_entry_skeleton_taken_over(env):
    """目标位置已有自动创建的空 Default 条目：删除该条目，旧条目接管目录。"""
    _root, ws = env
    _populate_root(ws)
    default_dir = ws / "default"
    (default_dir / ".cili" / "sessions").mkdir(parents=True)  # 空骨架
    _write_index([
        _legacy(ws),
        {"uuid": "cccc3333", "workspace_name": "Default", "directory": str(default_dir)},
    ])

    assert migrate_default_workspace() == LEGACY_UUID

    assert config_mod.find_workspace_entry("cccc3333") is None
    entry = config_mod.find_workspace_entry(LEGACY_UUID)
    assert Path(entry["directory"]) == default_dir
    # 旧根 .cili 完整接管（骨架被替换）
    assert (default_dir / ".cili" / "sessions" / "s1.json").exists()


def test_abort_when_default_entry_has_real_data(env):
    """目标位置条目含真实会话数据 → 放弃迁移，现场不变。"""
    _root, ws = env
    _populate_root(ws)
    default_dir = ws / "default"
    (default_dir / ".cili" / "sessions").mkdir(parents=True)
    (default_dir / ".cili" / "sessions" / "chat.json").write_text("{}", encoding="utf-8")
    _write_index([
        _legacy(ws),
        {"uuid": "dddd4444", "workspace_name": "Default", "directory": str(default_dir)},
    ])

    assert migrate_default_workspace() is None

    # 索引与磁盘都不动
    assert config_mod.find_workspace_entry("dddd4444") is not None
    assert Path(config_mod.find_workspace_entry(LEGACY_UUID)["directory"]) == ws
    assert (ws / "notes.md").exists()
    assert not (ws / "default" / "notes.md").exists()


def test_abort_on_git_history_conflict(env):
    """目标已存在 .git 且旧根也有 → 不能合并历史，中止。"""
    _root, ws = env
    _populate_root(ws)
    default_dir = ws / "default"
    (default_dir / ".git").mkdir(parents=True)
    (default_dir / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    _write_index([_legacy(ws)])

    assert migrate_default_workspace() is None
    assert (ws / "notes.md").exists()
    assert Path(config_mod.find_workspace_entry(LEGACY_UUID)["directory"]) == ws


def test_idempotent_second_run(env):
    """迁移完成后再次启动（索引已指向 default）→ 不再触发。"""
    _root, ws = env
    _populate_root(ws)
    _write_index([_legacy(ws)])

    assert migrate_default_workspace() == LEGACY_UUID
    assert migrate_default_workspace() is None

    entries = config_mod.load_workspaces_index()
    assert len(entries) == 1
    assert Path(entries[0]["directory"]) == ws / "default"


def test_stale_index_bootstrap_fix(env):
    """上次索引更新失败（文件已就位、索引仍指旧根）→ 直接修复索引。"""
    _root, ws = env
    default_dir = ws / "default"
    (default_dir / ".cili" / "sessions").mkdir(parents=True)
    (default_dir / ".cili" / "sessions" / "s1.json").write_text("{}", encoding="utf-8")
    _write_index([_legacy(ws)])

    assert migrate_default_workspace() == LEGACY_UUID
    assert Path(config_mod.find_workspace_entry(LEGACY_UUID)["directory"]) == default_dir


def test_ensure_default_workspace_triggers_migration(env, monkeypatch):
    """ensure_default_workspace 启动链路：先迁移旧根，再按名字命中 Default。"""
    _root, ws = env
    _populate_root(ws)
    _write_index([_legacy(ws)])

    uuid = config_mod.ensure_default_workspace()
    assert uuid == LEGACY_UUID
    assert Path(config_mod.find_workspace_entry(LEGACY_UUID)["directory"]) == ws / "default"
    assert (ws / "default" / ".cili" / "sessions" / "s1.json").exists()
