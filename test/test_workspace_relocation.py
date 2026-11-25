"""单工作区数据迁移测试：同卷/跨卷、预检、容量、索引与中断恢复。"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

import pytest

import core.config as config_mod
import core.workspace_relocation as relocation


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离项目根、索引、设置与迁移日志目录。"""
    root = tmp_path / "cili"
    root.mkdir()
    (root / "data").mkdir()
    (root / "data" / "setting.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(config_mod, "PROJECT_ROOT", root)
    monkeypatch.setattr(config_mod, "DATA_DIR", root / "data")
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", root / "data" / "setting.json")
    monkeypatch.setattr(config_mod, "WORKSPACES_JSON", root / "data" / "workspaces.json")
    monkeypatch.delenv("CILI_WORKSPACES_ROOT", raising=False)
    config_mod.reset_workspaces_root_cache()

    src = root / "workspaces" / "demo"
    src.mkdir(parents=True)
    (src / "a.txt").write_text("hello", encoding="utf-8")
    (src / ".cili").mkdir()
    (src / ".cili" / "state.json").write_text("{}", encoding="utf-8")

    config_mod.upsert_workspace_entry({
        "uuid": "w1",
        "workspace_name": "demo",
        "directory": str(src),
        "git_enabled": False,
        "memory_enabled": False,
    })

    yield root, src

    # 清理迁移模块的进程内状态，避免用例之间互相影响
    relocation._MIGRATING.clear()
    relocation._CANCEL_EVENTS.clear()
    if relocation._PROCESS_LOCK.locked():
        relocation._PROCESS_LOCK.release()


def _wait_status(workspace_uuid: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = relocation.get_migration_status(workspace_uuid)
        if last and not last.get("active"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"迁移未在 {timeout}s 内完成: {last}")


def test_same_volume_migration_updates_index(env):
    root, src = env
    dst = root / "workspaces" / "moved"

    plan = relocation.preflight_workspace_migration("w1", str(dst))
    assert plan["same_volume"] is True
    assert plan["method"] == "rename"
    assert plan["blockers"] == []

    result = relocation.start_workspace_migration("w1", str(dst))
    assert result["status"] == "running"

    backups = list((root / "data" / "backup" / "migrations").glob("*workspace-move*"))
    assert backups, "迁移前应自动备份配置"
    assert (backups[0] / "workspaces.json").is_file()
    assert (backups[0] / "setting.json").is_file()

    status = _wait_status("w1")
    assert status["status"] == "done"
    assert not src.exists()
    assert (dst / "a.txt").read_text(encoding="utf-8") == "hello"
    assert (dst / ".cili" / "state.json").is_file()

    entry = config_mod.find_workspace_entry("w1")
    assert entry is not None
    assert Path(entry["directory"]) == dst

    raw = json.loads(config_mod.WORKSPACES_JSON.read_text(encoding="utf-8"))
    assert raw[0]["directory"] == "workspaces/moved"


def test_cross_volume_copy_and_cleanup(env, monkeypatch):
    root, src = env
    dst = root / "workspaces" / "copied"
    monkeypatch.setattr(relocation, "_same_volume", lambda a, b: False)

    result = relocation.start_workspace_migration("w1", str(dst))
    assert result["plan"]["method"] == "copy"

    status = _wait_status("w1")
    assert status["status"] == "done"
    assert not src.exists()
    assert (dst / "a.txt").read_text(encoding="utf-8") == "hello"


def test_preflight_rejects_existing_target(env):
    root, _src = env
    dst = root / "workspaces" / "moved"
    dst.mkdir(parents=True)

    plan = relocation.preflight_workspace_migration("w1", str(dst))
    assert any("已存在" in item for item in plan["blockers"])


def test_preflight_rejects_target_inside_source(env):
    _root, src = env
    plan = relocation.preflight_workspace_migration("w1", str(src / "sub"))
    assert any("内部" in item for item in plan["blockers"])


def test_preflight_rejects_insufficient_space(env, monkeypatch):
    root, _src = env
    dst = root / "other" / "moved"
    monkeypatch.setattr(relocation, "_same_volume", lambda a, b: False)
    monkeypatch.setattr(
        relocation.shutil, "disk_usage",
        lambda path: type("Usage", (), {"total": 1, "used": 0, "free": 1})(),
    )

    plan = relocation.preflight_workspace_migration("w1", str(dst))
    assert any("空间不足" in item for item in plan["blockers"])


def test_backup_failure_blocks_migration(env, monkeypatch):
    root, src = env
    dst = root / "workspaces" / "moved"

    def _fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(relocation.shutil, "copy2", _fail)
    with pytest.raises(relocation.MigrationError) as exc:
        relocation.start_workspace_migration("w1", str(dst))
    assert exc.value.status_code == 500
    assert src.exists()
    assert not dst.exists()


def test_start_rejects_busy_workspace(env, monkeypatch):
    root, _src = env
    dst = root / "workspaces" / "moved"
    monkeypatch.setattr(relocation, "_workspace_activity", lambda uuid: ["运行中的会话"])

    with pytest.raises(relocation.MigrationError) as exc:
        relocation.start_workspace_migration("w1", str(dst))
    assert exc.value.status_code == 409


@pytest.fixture
def layout_env(tmp_path, monkeypatch):
    """旧布局 workspace/：两个已登记工作区 + 非工作区目录/文件。"""
    root = tmp_path / "cili"
    root.mkdir()
    (root / "data").mkdir()
    (root / "data" / "setting.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(config_mod, "PROJECT_ROOT", root)
    monkeypatch.setattr(config_mod, "DATA_DIR", root / "data")
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", root / "data" / "setting.json")
    monkeypatch.setattr(config_mod, "WORKSPACES_JSON", root / "data" / "workspaces.json")
    monkeypatch.delenv("CILI_WORKSPACES_ROOT", raising=False)
    config_mod.reset_workspaces_root_cache()

    legacy = root / "workspace"
    default_dir = legacy / "default"
    pytest_dir = legacy / "pytest"
    math_dir = legacy / "何同学-数学"
    plain_dir = legacy / "plain"
    chinese_plain = legacy / "中文测试"
    for path in (default_dir, pytest_dir, math_dir, plain_dir, chinese_plain):
        path.mkdir(parents=True)
        (path / "keep.txt").write_text(path.name, encoding="utf-8")
    (legacy / "stray.txt").write_text("stray", encoding="utf-8")

    config_mod.upsert_workspace_entry({
        "uuid": "w-default", "workspace_name": "Default",
        "directory": str(default_dir), "git_enabled": False, "memory_enabled": False,
    })
    config_mod.upsert_workspace_entry({
        "uuid": "w-pytest", "workspace_name": "pytest",
        "directory": str(pytest_dir), "git_enabled": False, "memory_enabled": False,
    })
    config_mod.upsert_workspace_entry({
        "uuid": "w-math", "workspace_name": "何同学-数学",
        "directory": str(math_dir), "git_enabled": False, "memory_enabled": False,
    })
    yield root, legacy

    relocation._MIGRATING.clear()
    relocation._CANCEL_EVENTS.clear()
    if relocation._PROCESS_LOCK.locked():
        relocation._PROCESS_LOCK.release()


def test_layout_migration_moves_registered_and_plain(layout_env):
    root, legacy = layout_env
    target = root / "workspaces"

    result = relocation.migrate_layout_to_workspaces()

    backups = list((root / "data" / "backup" / "migrations").glob("*workspace-layout*"))
    assert backups, "布局迁移前应自动备份配置"
    assert (backups[0] / "workspaces.json").is_file()
    assert (backups[0] / "setting.json").is_file()

    assert not legacy.exists()
    assert (target / "default" / "keep.txt").is_file()
    assert (target / "pytest" / "keep.txt").is_file()
    assert (target / "何同学-数学" / "keep.txt").is_file()
    assert (target / "plain" / "keep.txt").is_file()
    assert (target / "中文测试" / "keep.txt").is_file()
    assert (target / "stray.txt").is_file()
    assert set(result["moved"]) >= {
        "default", "pytest", "何同学-数学", "plain", "中文测试", "stray.txt",
    }

    default_entry = config_mod.find_workspace_entry("w-default")
    assert default_entry is not None
    assert Path(default_entry["directory"]) == target / "default"
    math_entry = config_mod.find_workspace_entry("w-math")
    assert math_entry is not None
    assert Path(math_entry["directory"]) == target / "何同学-数学"

    raw = {e["uuid"]: e for e in json.loads(
        config_mod.WORKSPACES_JSON.read_text(encoding="utf-8")
    )}
    assert raw["w-math"]["directory"] == "workspaces/何同学-数学"

    cfg = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert cfg["workspaces"]["root"] == "workspaces"
    assert cfg["workspaces"]["layout_migrated_at"]

    # 幂等：第二次直接跳过
    again = relocation.migrate_layout_to_workspaces()
    assert again.get("skipped_reason") == "already_migrated"


def test_layout_backup_failure_skips(layout_env, monkeypatch):
    _root, legacy = layout_env

    def _fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(relocation.shutil, "copy2", _fail)
    result = relocation.migrate_layout_to_workspaces()
    assert result["skipped_reason"] == "backup_failed"
    assert legacy.exists()


def test_layout_migration_conflict_keeps_legacy(layout_env):
    root, legacy = layout_env
    target = root / "workspaces"
    (target / "default").mkdir(parents=True)
    (target / "default" / "other.txt").write_text("other", encoding="utf-8")

    result = relocation.migrate_layout_to_workspaces()

    assert "default" in result["skipped"]
    assert (legacy / "default" / "keep.txt").is_file()
    assert (target / "pytest").is_dir()
    assert legacy.exists()
    cfg = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert not cfg["workspaces"].get("layout_migrated_at")
    assert cfg["workspaces"]["root"] == "workspaces"

    # 解决冲突后重跑：旧根剩余项继续迁移并完成
    shutil.rmtree(target / "default")
    result2 = relocation.migrate_layout_to_workspaces()
    assert "default" in result2["moved"]
    assert not legacy.exists()
    cfg2 = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert cfg2["workspaces"]["layout_migrated_at"]


def test_layout_migration_normalizes_root_workspace(layout_env):
    root, legacy = layout_env
    # 额外模拟旧版：根目录本身是 Default 工作区
    (legacy / ".cili").mkdir()
    (legacy / ".cili" / "marker").write_text("x", encoding="utf-8")
    config_mod.remove_workspace_entry("w-default")
    config_mod.upsert_workspace_entry({
        "uuid": "w-root", "workspace_name": "Default",
        "directory": str(legacy), "git_enabled": False, "memory_enabled": False,
    })

    result = relocation.migrate_layout_to_workspaces()

    assert not legacy.exists()
    target = root / "workspaces"
    assert (target / "default" / ".cili" / "marker").is_file()
    entry = config_mod.find_workspace_entry("w-root")
    assert entry is not None
    assert Path(entry["directory"]) == target / "default"
    assert result.get("skipped_reason") != "legacy_layout_failed"


def test_layout_migration_logs_holders_on_lock(layout_env, monkeypatch, caplog):
    """占用导致的 WinError 5 失败应把占用进程写进日志。"""
    root, legacy = layout_env
    monkeypatch.setattr(
        relocation, "describe_holders",
        lambda path: [f"WINWORD.EXE（PID 1）打开了 {path / 'x.docx'}"],
    )

    def _locked(src, dst):
        raise PermissionError(13, "拒绝访问", str(src), 5)

    monkeypatch.setattr(relocation, "_move_path", _locked)

    with caplog.at_level(logging.WARNING, logger="core.workspace_relocation"):
        result = relocation.migrate_layout_to_workspaces()

    assert result["skipped"], "占用时全部迁移项应跳过"
    assert any(
        "被以下进程占用" in r.getMessage() and "WINWORD.EXE" in r.getMessage()
        for r in caplog.records
    )


def test_recover_verifying_after_switch(env):
    """复制已切换、日志仍停在 verifying：启动恢复应补索引并清理源。"""
    root, src = env
    dst = root / "workspaces" / "recovered"
    shutil.copytree(src, dst)

    relocation._save_active({
        "id": "mig-2",
        "workspace_uuid": "w1",
        "workspace_name": "demo",
        "src": str(src),
        "dst": str(dst),
        "method": "copy",
        "phase": "verifying",
        "files_total": 2,
        "bytes_total": 10,
    })

    relocation.recover_pending_migration()

    entry = config_mod.find_workspace_entry("w1")
    assert entry is not None
    assert Path(entry["directory"]) == dst
    assert not src.exists()
    assert relocation.get_migration_status("w1")["status"] == "done"


def test_recover_switched_migration(env):
    root, src = env
    dst = root / "workspaces" / "recovered"
    dst.mkdir(parents=True)
    (dst / "a.txt").write_text("hello", encoding="utf-8")
    src.rename(dst.with_name("_gone"))  # 模拟源已被 rename 走
    src.unlink(missing_ok=True)
    shutil.rmtree(dst.with_name("_gone"), ignore_errors=True)

    relocation._save_active({
        "id": "mig-1",
        "workspace_uuid": "w1",
        "workspace_name": "demo",
        "src": str(src),
        "dst": str(dst),
        "method": "rename",
        "phase": "switched",
        "files_total": 1,
        "bytes_total": 5,
    })

    relocation.recover_pending_migration()

    entry = config_mod.find_workspace_entry("w1")
    assert entry is not None
    assert Path(entry["directory"]) == dst
    status = relocation.get_migration_status("w1")
    assert status["status"] == "done"