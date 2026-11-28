"""共享工作区：创建、remote_wins 拉取与退出清理。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from core.config import load_workspaces_index
from core.org import workspaces as org_ws

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI not available"
)


def _git(cwd, *args):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _remote_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "test")
    (seed / "hello.txt").write_text("hello from remote\n", encoding="utf-8")
    (seed / ".gitignore").write_text(
        ".cili/tmp/\n.cili/cache/\n.cili/runtime/\n", encoding="utf-8"
    )
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "init")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    return remote


def test_sync_reasserts_git_settings(tmp_path, monkeypatch):
    """本地误关 Git/清空 remote 后，组织同步恢复。"""
    from core.config import get_workspace_meta, save_workspace_meta

    monkeypatch.setattr(org_ws, "workspaces_root", lambda: tmp_path / "workspaces")
    remote = _remote_repo(tmp_path)
    results = org_ws.apply_workspaces(
        [{
            "key": "rd-shared", "name": "研发共享", "group_id": "g", "group_name": "研发",
            "git": {"remote_url": str(remote), "branch": "main", "username": "org",
                    "password": "dev-token", "sync_mode": "remote_wins"},
        }],
        org={"id": "acme"}, token="dev-token",
    )
    item = results[0]
    assert item["status"] == "ok", item
    uuid = item["uuid"]
    meta = get_workspace_meta(uuid)
    meta["git_enabled"] = False
    meta["git_remote_url"] = ""
    save_workspace_meta(uuid, meta)

    synced = org_ws.sync_managed_workspaces([item], token="dev-token")
    assert synced[0]["status"] == "ok", synced[0]
    meta = get_workspace_meta(uuid)
    assert meta["git_enabled"] is True
    assert meta["git_sync_mode"] == "remote_wins"
    assert meta["git_remote_url"] == str(remote)


def test_apply_and_disconnect_shared_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        org_ws, "workspaces_root", lambda: tmp_path / "workspaces"
    )
    remote = _remote_repo(tmp_path)
    results = org_ws.apply_workspaces(
        [
            {
                "key": "rd-shared",
                "name": "研发部共享",
                "group_id": "g_rd",
                "group_name": "研发部",
                "git": {
                    "remote_url": str(remote),
                    "branch": "main",
                    "username": "org",
                    "password": "dev-token",
                    "sync_mode": "remote_wins",
                },
            }
        ],
        org={"id": "acme"},
        token="dev-token",
    )
    assert len(results) == 1
    item = results[0]
    assert item["status"] == "ok", item
    directory = Path(item["directory"])
    assert (directory / "hello.txt").read_text(encoding="utf-8") == "hello from remote\n"
    entry = next(
        e for e in load_workspaces_index() if e.get("uuid") == item["uuid"]
    )
    assert entry["org_managed"] is True
    assert entry["org_workspace_key"] == "rd-shared"
    assert entry["git_token"] == ""
    exclude = (directory / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".cili/" not in exclude
    gitignore = (directory / ".gitignore").read_text(encoding="utf-8")
    assert ".cili/tmp/" in gitignore
    assert ".cili/cache/" in gitignore
    assert ".cili/runtime/" in gitignore

    from core.workspace_git import sync_remote_wins

    (directory / "local.txt").write_text("local change\n", encoding="utf-8")
    ok, message = sync_remote_wins(item["uuid"], "main")
    assert ok, message
    shown = subprocess.run(
        ["git", "--git-dir", str(remote), "show", "main:local.txt"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout == "local change\n"

    org_ws.disconnect_workspaces("acme")
    entry = next(
        (e for e in load_workspaces_index() if e.get("uuid") == item["uuid"]),
        None,
    )
    assert entry is not None
    assert entry["org_managed"] is False
    assert entry["org_active"] is False
    assert entry["git_token"] == ""
    assert directory.is_dir()

    rejoined = org_ws.apply_workspaces(
        [
            {
                "key": "rd-shared",
                "name": "研发部共享",
                "group_id": "g_rd",
                "group_name": "研发部",
                "git": {
                    "remote_url": str(remote),
                    "branch": "main",
                    "username": "org",
                    "password": "dev-token-2",
                },
            }
        ],
        org={"id": "acme"},
        token="dev-token-2",
    )[0]
    assert rejoined["uuid"] == item["uuid"]
    assert rejoined["directory"] == item["directory"]
    entry = next(
        e for e in load_workspaces_index() if e.get("uuid") == item["uuid"]
    )
    assert entry["org_managed"] is True
    assert entry["org_active"] is True
    assert entry["git_token"] == ""
