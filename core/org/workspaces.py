"""组织共享工作区：创建/更新、Git 远程物化与退出清理。"""

from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

from core.config import (
    find_workspace_entry,
    get_workspace_meta,
    load_workspaces_index,
    save_workspace_meta,
    workspaces_root,
)
from core.time_utils import now_str
from core.workspace_git import (
    ensure_git_exclude,
    init_workspace_git,
    set_git_remote,
    sync_remote_wins,
)

logger = logging.getLogger(__name__)


def _safe_key(value: str) -> str:
    """目录/工作区 slug：以服务端下发值为准，仅去掉文件系统不安全字符。"""
    raw = str(value or "").strip()
    cleaned = re.sub(r'[\/:*?"<>|\s]+', "-", raw).strip(".-")[:64]
    return cleaned or "workspace"


def _find_managed(org_id: str, key: str) -> dict | None:
    for entry in load_workspaces_index():
        if (
            str(entry.get("org_id") or "") == str(org_id)
            and str(entry.get("org_workspace_key") or "") == str(key)
        ):
            return entry
    return None


def _directory_for(entry: dict | None, slug: str) -> Path:
    if entry and entry.get("directory"):
        return Path(entry["directory"])
    name = _safe_key(slug)
    base = workspaces_root() / name
    if base.exists():
        base = workspaces_root() / f"{name}-{secrets.token_hex(3)}"
    return base


def apply_workspaces(
    payload_workspaces: list[dict],
    *,
    org: dict,
    token: str,
) -> list[dict]:
    """创建/更新共享工作区，返回可写入 state 的摘要列表。"""
    org_id = str(org.get("id") or "")
    results: list[dict] = []
    for raw in payload_workspaces or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        if not key:
            continue
        # 工作区目录/slug 以服务端下发的 slug 为准（缺省回退 key）
        slug = str(raw.get("slug") or key).strip()
        name = str(raw.get("name") or key)
        git = raw.get("git") if isinstance(raw.get("git"), dict) else {}
        remote = str(git.get("remote_url") or "")
        remote_error = ""
        if remote:
            parsed_remote = urlparse(remote)
            if parsed_remote.username or parsed_remote.password:
                remote_error = "共享工作区远程地址不能包含用户名/密码"
                remote = ""
        branch = str(git.get("branch") or "main")
        if (
            not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
            or branch.startswith("-")
            or ".." in branch
        ):
            branch = "main"
        username = str(git.get("username") or "org")
        password = str(git.get("password") or token)
        group_id = str(raw.get("group_id") or "")
        group_name = str(raw.get("group_name") or "")
        memory_enabled = bool(raw.get("memory_enabled", True))

        entry = _find_managed(org_id, key) or {}
        uuid = str(entry.get("uuid") or secrets.token_hex(4))
        directory = _directory_for(entry or None, slug)
        now = now_str()
        config = dict(entry or {})
        config.update(
            {
                "workspace_name": name,
                "directory": str(directory),
                "slug": _safe_key(slug),
                "location": "cili",
                "category": "org",
                "created_at": config.get("created_at") or now,
                "updated_at": now,
                "git_enabled": True,
                "memory_enabled": memory_enabled,
                "org_id": org_id,
                "org_workspace_key": key,
                "org_group_id": group_id,
                "org_group_name": group_name,
                "org_managed": True,
                "org_active": True,
                "git_sync_mode": str(git.get("sync_mode") or "remote_wins"),
                "git_remote_url": remote,
                "git_branch": branch,
                "git_username": username,
                "git_token": "",
            }
        )
        result = {
            "uuid": uuid,
            "key": key,
            "name": name,
            "group_id": group_id,
            "group_name": group_name,
            "directory": str(directory),
            "branch": branch,
            "remote_url": remote,
            "git_username": username,
            "sync_mode": str(git.get("sync_mode") or "remote_wins"),
            "memory_enabled": memory_enabled,
            "status": "ok",
            "error": "",
        }
        try:
            if remote_error:
                raise RuntimeError(remote_error)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / ".cili" / "sessions").mkdir(parents=True, exist_ok=True)
            from core.layout_migration import write_layout_marker
            write_layout_marker(directory / ".cili")
            (directory / ".cili" / "config" / "approval_rules").mkdir(parents=True, exist_ok=True)
            (directory / ".cili" / "config" / "sensitive_words" / "rules").mkdir(parents=True, exist_ok=True)
            if not save_workspace_meta(uuid, config):
                raise RuntimeError("工作区索引写入失败")
            if remote:
                ok, msg = init_workspace_git(uuid)
                if not ok:
                    raise RuntimeError(msg)
                ensure_git_exclude(uuid)
                ok, msg = set_git_remote(uuid, remote, username, password)
                # 令牌只留在 .git/cili-credentials，不进入 workspaces.json / 工作区 API。
                meta = get_workspace_meta(uuid) or {}
                if meta.get("git_token"):
                    meta["git_token"] = ""
                    save_workspace_meta(uuid, meta)
                if not ok:
                    raise RuntimeError(msg)
                sync_ok, sync_msg = sync_remote_wins(uuid, branch)
                if not sync_ok:
                    result["status"] = "warning"
                    result["error"] = sync_msg
            else:
                result["status"] = "warning"
                result["error"] = "组织未下发 Git 远程地址"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[org] 共享工作区 %s 应用失败", key, exc_info=True)
            result["status"] = "error"
            result["error"] = str(exc)[:500]
        results.append(result)
    return results


def sync_managed_workspaces(entries: list[dict], token: str = "") -> list[dict]:
    """304/同步时强制恢复组织工作区 Git 配置并同步。"""
    results: list[dict] = []
    for item in entries or []:
        uuid = str(item.get("uuid") or "")
        if not uuid or not find_workspace_entry(uuid):
            continue
        meta = get_workspace_meta(uuid) or {}
        remote = str(item.get("remote_url") or meta.get("git_remote_url") or "")
        branch = str(item.get("branch") or meta.get("git_branch") or "main")
        username = str(item.get("git_username") or meta.get("git_username") or "org")
        if not remote:
            updated = dict(item)
            updated["status"] = "warning"
            updated["error"] = "共享工作区缺少远程地址，需全量配置"
            results.append(updated)
            continue
        config = dict(meta)
        config.update({
            "git_enabled": True,
            "git_sync_mode": "remote_wins",
            "git_remote_url": remote,
            "git_branch": branch,
            "git_username": username,
            "org_managed": True,
        })
        save_workspace_meta(uuid, config)
        ok, msg = init_workspace_git(uuid)
        if not ok:
            updated = dict(item)
            updated["status"] = "warning"
            updated["error"] = msg
            results.append(updated)
            continue
        ensure_git_exclude(uuid)
        set_ok, set_msg = set_git_remote(uuid, remote, username, token)
        if not set_ok:
            updated = dict(item)
            updated["status"] = "warning"
            updated["error"] = set_msg
            results.append(updated)
            continue
        meta = get_workspace_meta(uuid) or {}
        if meta.get("git_token"):
            meta["git_token"] = ""
            save_workspace_meta(uuid, meta)
        ok, msg = sync_remote_wins(uuid, branch)
        updated = dict(item)
        updated.update({"remote_url": remote, "git_username": username, "branch": branch})
        updated["status"] = "ok" if ok else "warning"
        updated["error"] = "" if ok else msg
        results.append(updated)
    return results


def prune_managed_workspaces(org_id: str, keep_keys: set[str]) -> None:
    """服务端已移除的共享工作区：清凭证并从索引移除，文件保留。"""
    for entry in load_workspaces_index():
        if str(entry.get("org_id") or "") != str(org_id):
            continue
        if not entry.get("org_managed"):
            continue
        key = str(entry.get("org_workspace_key") or "")
        if key and key in keep_keys:
            continue
        uuid = str(entry.get("uuid") or "")
        directory = Path(entry.get("directory") or "")
        try:
            (directory / ".git" / "cili-credentials").unlink(missing_ok=True)
        except OSError:
            pass
        if uuid:
            config = dict(entry)
            config["git_username"] = ""
            config["git_token"] = ""
            config["org_managed"] = False
            config["org_active"] = False
            config["updated_at"] = now_str()
            save_workspace_meta(uuid, config)


def disconnect_workspaces(org_id: str) -> None:
    """退出/失效时清理组织工作区；文件与索引条目保留。"""
    for entry in load_workspaces_index():
        if str(entry.get("org_id") or "") != str(org_id):
            continue
        if not entry.get("org_managed"):
            continue
        uuid = str(entry.get("uuid") or "")
        directory = Path(entry.get("directory") or "")
        try:
            (directory / ".git" / "cili-credentials").unlink(missing_ok=True)
        except OSError:
            pass
        if uuid:
            config = dict(entry)
            config["git_username"] = ""
            config["git_token"] = ""
            config["org_managed"] = False
            config["org_active"] = False
            config["updated_at"] = now_str()
            save_workspace_meta(uuid, config)
