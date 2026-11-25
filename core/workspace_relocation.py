"""单工作区数据迁移：把一个工作区目录搬到其他盘/目录，并更新索引。

设计要点（workspace/default/workspace-data-migration-design-2026-11-25.md）：

- 迁移单位是单个工作区；同卷 rename，跨卷 copy → verify → switch → cleanup。
- 迁移期间冻结该工作区：拒绝新回合、移除 runner；其他工作区不受影响。
- 进度与断点写在 ``data/cili/workspace-migration.json``；启动时由
  ``recover_pending_migration()`` 做一致性修复。
- 索引指向目标且校验通过之前，绝不删除源目录。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid as uuid_mod
from pathlib import Path
from typing import Any

from core import config as config_mod
from core.fs_utils import atomic_write_json, is_path_within
from core.paths import to_storage_path
from core.time_utils import now_str
from core.win_locks import describe_holders

logger = logging.getLogger(__name__)

JOURNAL_NAME = "workspace-migration.json"
LOCK_NAME = "workspace-migration.lock"

_PROCESS_LOCK = threading.Lock()
_JOURNAL_LOCK = threading.RLock()
_MIGRATING: set[str] = set()
_MIGRATING_LOCK = threading.Lock()
_CANCEL_EVENTS: dict[str, threading.Event] = {}

_SPACE_MARGIN = 1.1
_SPACE_WARN = 1.2
_PROGRESS_INTERVAL = 2.0


class MigrationError(Exception):
    """迁移前置/执行错误；status_code 供 Web 路由映射 HTTP 状态。"""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class MigrationCancelled(MigrationError):
    def __init__(self, message: str = "迁移已取消"):
        super().__init__(message, status_code=409)


# ── 运行时标记 ──────────────────────────────────────────────────

def is_workspace_migrating(workspace_uuid: str) -> bool:
    with _MIGRATING_LOCK:
        return workspace_uuid in _MIGRATING


def migrating_workspaces() -> set[str]:
    with _MIGRATING_LOCK:
        return set(_MIGRATING)


def _mark_migrating(workspace_uuid: str) -> None:
    with _MIGRATING_LOCK:
        _MIGRATING.add(workspace_uuid)


def _unmark_migrating(workspace_uuid: str) -> None:
    with _MIGRATING_LOCK:
        _MIGRATING.discard(workspace_uuid)


# ── journal ─────────────────────────────────────────────────────

def _journal_path() -> Path:
    return Path(config_mod.DATA_DIR) / JOURNAL_NAME


def _read_journal_unlocked() -> dict:
    path = _journal_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"[relocation] 读取迁移日志失败: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _read_journal() -> dict:
    with _JOURNAL_LOCK:
        return _read_journal_unlocked()


def _save_active(journal: dict) -> None:
    with _JOURNAL_LOCK:
        data = _read_journal_unlocked()
        data["version"] = 1
        data["active"] = journal
        data.setdefault("last", None)
        atomic_write_json(_journal_path(), data)


def _finish_active(journal: dict, status: str, error: str = "") -> None:
    with _JOURNAL_LOCK:
        data = _read_journal_unlocked()
        data["version"] = 1
        data["active"] = None
        data["last"] = {
            "id": journal.get("id", ""),
            "workspace_uuid": journal.get("workspace_uuid", ""),
            "workspace_name": journal.get("workspace_name", ""),
            "src": journal.get("src", ""),
            "dst": journal.get("dst", ""),
            "method": journal.get("method", ""),
            "status": status,
            "error": error,
            "files_total": journal.get("files_total", 0),
            "bytes_total": journal.get("bytes_total", 0),
            "finished_at": now_str(),
        }
        atomic_write_json(_journal_path(), data)


def get_migration_status(workspace_uuid: str) -> dict:
    """当前/最近一次迁移状态；没有该工作区的记录时返回 {}。"""
    data = _read_journal()
    for key in ("active", "last"):
        item = data.get(key)
        if isinstance(item, dict) and item.get("workspace_uuid") == workspace_uuid:
            result = dict(item)
            result["active"] = key == "active"
            result["migrating"] = is_workspace_migrating(workspace_uuid)
            return result
    return {}


def cancel_workspace_migration(workspace_uuid: str) -> bool:
    """请求取消（仅复制/校验阶段有效）；返回是否已发出取消信号。"""
    active = _read_journal().get("active")
    if not isinstance(active, dict) or active.get("workspace_uuid") != workspace_uuid:
        return False
    if active.get("phase") not in ("preparing", "copying", "verifying"):
        return False
    event = _CANCEL_EVENTS.get(str(active.get("id") or ""))
    if event is None:
        return False
    event.set()
    return True


# ── 路径与容量评估 ──────────────────────────────────────────────

def _existing_ancestor(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return current
        current = parent
    return current


def _same_volume(source: Path, target_parent: Path) -> bool:
    try:
        return os.stat(source).st_dev == os.stat(target_parent).st_dev
    except OSError:
        return os.path.splitdrive(str(source))[0].lower() == os.path.splitdrive(
            str(target_parent))[0].lower()


def _measure_tree(path: Path) -> dict:
    """统计文件数与字节数（不跟随目录符号链接；链接本身按 lstat 计数）。"""
    files = 0
    total = 0
    try:
        for root, dirs, names in os.walk(path, followlinks=False):
            for name in list(dirs):
                item = Path(root) / name
                if item.is_symlink():
                    dirs.remove(name)
                    files += 1
                    total += item.lstat().st_size
            for name in names:
                item = Path(root) / name
                files += 1
                total += item.lstat().st_size
    except OSError as e:
        raise MigrationError(f"无法读取目录 {path}: {e}") from e
    return {"files": files, "bytes": total}


def _validate_target(workspace_uuid: str, src: Path, dst: Path) -> list[str]:
    blockers: list[str] = []
    if dst == src:
        blockers.append("目标路径与当前目录相同")
    if is_path_within(str(dst), str(src)):
        blockers.append("目标路径不能位于当前工作区目录内部")
    if src == Path(config_mod.PROJECT_ROOT) or is_path_within(
            str(config_mod.PROJECT_ROOT), str(src)):
        blockers.append("工作区目录包含 cili 程序目录，禁止迁移")
    if dst == Path(config_mod.PROJECT_ROOT):
        blockers.append("目标路径不能是 cili 项目根目录")
    for protected in (Path(config_mod.DATA_DIR), Path(config_mod.SYSTEM_DATA_DIR)):
        if is_path_within(str(dst), str(protected)):
            blockers.append("目标路径不能位于系统数据目录内")
    if dst.exists():
        blockers.append("目标路径已存在，不能覆盖")

    for entry in config_mod.load_workspaces_index():
        if entry.get("uuid") == workspace_uuid:
            continue
        directory = entry.get("directory")
        if not directory:
            continue
        other = Path(directory)
        if is_path_within(str(dst), str(other)):
            blockers.append(f"目标路径位于工作区「{entry.get('workspace_name') or other.name}」内部")
        if is_path_within(str(other), str(dst)):
            blockers.append(f"目标路径包含了工作区「{entry.get('workspace_name') or other.name}」")
        cili = other / ".cili"
        if is_path_within(str(dst), str(cili)):
            blockers.append(f"目标路径位于工作区数据目录「{cili}」内")

    if len(str(dst)) > 240:
        blockers.append("目标路径过长（Windows 长路径限制）")
    return blockers


def _workspace_activity(workspace_uuid: str) -> list[str]:
    """该工作区当前的活动来源（空列表 = 空闲）。"""
    activities: set[str] = set()

    try:
        from core.session_registry import registry

        for key in registry.keys():
            if key.startswith(f"{workspace_uuid}:") and not registry.is_idle(key):
                activities.add("运行中的会话")
                break
    except Exception:  # noqa: BLE001
        pass

    try:
        from core.turn_queue import turn_queue

        if turn_queue.pending_for_workspace(workspace_uuid):
            activities.add("排队中的回合")
    except Exception:  # noqa: BLE001
        pass

    try:
        from core.tools.background import BackgroundTaskManager

        if BackgroundTaskManager.workspace_tasks(workspace_uuid, active_only=True):
            activities.add("后台任务")
    except Exception:  # noqa: BLE001
        pass

    try:
        from core.cron.executor import any_running_for_workspace

        if any_running_for_workspace(workspace_uuid):
            activities.add("定时任务")
    except Exception:  # noqa: BLE001
        pass

    return sorted(activities)


def preflight_workspace_migration(workspace_uuid: str, target_path: str) -> dict:
    """只读预检：目标合法性、体积/文件数、同/跨卷、可用空间、活动占用。"""
    entry = config_mod.find_workspace_entry(workspace_uuid)
    if not entry:
        raise MigrationError("工作区不存在", status_code=404)
    if workspace_uuid == config_mod.SYSTEM_WORKSPACE_UUID:
        raise MigrationError("系统工作区不能迁移", status_code=403)
    if is_workspace_migrating(workspace_uuid):
        raise MigrationError("该工作区正在迁移", status_code=409)

    raw_target = str(target_path or "").strip()
    if not raw_target:
        raise MigrationError("目标路径不能为空")
    src = Path(str(entry.get("directory") or ""))
    if not src.is_dir():
        raise MigrationError("当前工作区目录不存在，请先修复索引")

    dst = Path(os.path.abspath(os.path.expanduser(raw_target)))
    blockers = _validate_target(workspace_uuid, src, dst)
    warnings: list[str] = []

    stats = _measure_tree(src)
    parent = _existing_ancestor(dst.parent)
    if not parent.is_dir():
        blockers.append(f"目标父路径不是目录: {parent}")
    if not os.access(parent, os.W_OK):
        blockers.append(f"目标父目录不可写: {parent}")
    same_volume = _same_volume(src, parent)
    free = shutil.disk_usage(parent).free
    required = 0 if same_volume else int(stats["bytes"] * _SPACE_MARGIN)
    if not same_volume and free < required:
        blockers.append(
            f"目标盘可用空间不足：需要 {required} 字节，可用 {free} 字节"
        )
    elif not same_volume and free < required * _SPACE_WARN:
        warnings.append("目标盘剩余空间余量不足 20%")

    activities = _workspace_activity(workspace_uuid)
    if activities:
        blockers.append("工作区正在使用中：" + "、".join(activities))

    return {
        "uuid": workspace_uuid,
        "name": entry.get("workspace_name", ""),
        "src": str(src),
        "dst": str(dst),
        "target_parent": str(dst.parent),
        "same_volume": same_volume,
        "method": "rename" if same_volume else "copy",
        "file_count": stats["files"],
        "total_bytes": stats["bytes"],
        "dest_free_bytes": free,
        "required_bytes": required,
        "blockers": blockers,
        "warnings": warnings,
    }


# ── 复制/校验/清理 ──────────────────────────────────────────────

def _partial_path(dst: Path, migration_id: str) -> Path:
    return dst.with_name(f".{dst.name}.migrating-{migration_id}")


def _copy_symlink(src: Path, dst: Path) -> None:
    try:
        os.symlink(os.readlink(src), dst)
    except OSError as e:
        raise MigrationError(f"无法创建符号链接（需要权限）: {src}: {e}") from e


def _copy_tree_progress(src: Path, dst: Path, journal: dict,
                        cancel_event: threading.Event) -> None:
    dst.mkdir(parents=True, exist_ok=False)
    files_done = 0
    bytes_done = 0
    last_update = time.monotonic()

    for root, dirs, names in os.walk(src, followlinks=False):
        if cancel_event.is_set():
            raise MigrationCancelled()
        rel = Path(root).relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)

        for name in list(dirs):
            item = Path(root) / name
            if item.is_symlink():
                dirs.remove(name)
                _copy_symlink(item, target_dir / name)
                files_done += 1
                bytes_done += item.lstat().st_size

        for name in names:
            if cancel_event.is_set():
                raise MigrationCancelled()
            item = Path(root) / name
            target = target_dir / name
            if item.is_symlink():
                _copy_symlink(item, target)
            else:
                shutil.copy2(item, target)
            files_done += 1
            bytes_done += item.lstat().st_size

        now = time.monotonic()
        if now - last_update >= _PROGRESS_INTERVAL:
            journal["files_done"] = files_done
            journal["bytes_done"] = bytes_done
            journal["updated_at"] = now_str()
            _save_active(journal)
            last_update = now

    journal["files_done"] = files_done
    journal["bytes_done"] = bytes_done
    journal["updated_at"] = now_str()


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _log_holders(path: Path, exc: BaseException) -> None:
    """移动/删除因占用失败（WinError 5/32）时，把占用进程写进日志。"""
    if getattr(exc, "winerror", None) not in (5, 32):
        return
    holders = describe_holders(path)
    if holders:
        logger.warning(f"[relocation] {path} 被以下进程占用: {'；'.join(holders)}")
    else:
        logger.warning(f"[relocation] {path} 移动失败，未识别到占用进程（可能是权限或杀毒软件）")


# ── 执行 ────────────────────────────────────────────────────────

def start_workspace_migration(workspace_uuid: str, target_path: str) -> dict:
    """校验并启动后台迁移；立即返回迁移状态。"""
    if not _PROCESS_LOCK.acquire(blocking=False):
        raise MigrationError("另一个迁移正在进行", status_code=409)
    marked = False
    try:
        active = _read_journal().get("active")
        if isinstance(active, dict):
            raise MigrationError("存在未完成的迁移日志，请先重启服务恢复", status_code=409)

        plan = preflight_workspace_migration(workspace_uuid, target_path)
        if plan["blockers"]:
            raise MigrationError("；".join(plan["blockers"]), status_code=409)

        backup_migration_config("workspace-move")

        entry = config_mod.find_workspace_entry(workspace_uuid) or {}
        _mark_migrating(workspace_uuid)
        marked = True

        # 预检与标记之间可能有新回合进来；标记后再确认一次
        activities = _workspace_activity(workspace_uuid)
        if activities:
            raise MigrationError("工作区正在使用中：" + "、".join(activities), status_code=409)

        try:
            from core.session_registry import registry

            with registry.lock:
                registry.remove_workspace(workspace_uuid)
        except Exception as e:  # noqa: BLE001 —— 移除 runner 失败不应阻断迁移准备
            logger.warning(f"[relocation] 移除 runner 失败: {e}")

        migration_id = (
            time.strftime("%Y%m%d-%H%M%S") + "-" + uuid_mod.uuid4().hex[:6]
        )
        journal: dict[str, Any] = {
            "id": migration_id,
            "workspace_uuid": workspace_uuid,
            "workspace_name": entry.get("workspace_name", ""),
            "src": plan["src"],
            "dst": plan["dst"],
            "method": plan["method"],
            "phase": "preparing",
            "files_total": plan["file_count"],
            "bytes_total": plan["total_bytes"],
            "files_done": 0,
            "bytes_done": 0,
            "error": "",
            "started_at": now_str(),
            "updated_at": now_str(),
        }
        _save_active(journal)

        event = threading.Event()
        _CANCEL_EVENTS[migration_id] = event
        thread = threading.Thread(
            target=_run_worker, args=(journal, event),
            name=f"workspace-migration-{migration_id}", daemon=True,
        )
        thread.start()
        return {"id": migration_id, "status": "running", "plan": plan}
    except Exception:
        if marked:
            _unmark_migrating(workspace_uuid)
        _PROCESS_LOCK.release()
        raise


def _run_worker(journal: dict, cancel_event: threading.Event) -> None:
    workspace_uuid = journal["workspace_uuid"]
    src = Path(journal["src"])
    dst = Path(journal["dst"])
    method = journal["method"]
    migration_id = journal["id"]
    partial = _partial_path(dst, migration_id)
    keep_active = False

    try:
        if method == "copy":
            dst.parent.mkdir(parents=True, exist_ok=True)
            _remove_path(partial)
            journal["phase"] = "copying"
            journal["updated_at"] = now_str()
            _save_active(journal)

            _copy_tree_progress(src, partial, journal, cancel_event)
            journal["phase"] = "verifying"
            journal["updated_at"] = now_str()
            _save_active(journal)

            stats = _measure_tree(partial)
            if stats["files"] != journal["files_total"] or stats["bytes"] != journal["bytes_total"]:
                raise MigrationError(
                    f"复制校验失败：源 {journal['files_total']} 文件/{journal['bytes_total']} 字节，"
                    f"目标 {stats['files']} 文件/{stats['bytes']} 字节"
                )
            if cancel_event.is_set():
                raise MigrationCancelled()
            os.replace(partial, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            journal["phase"] = "switching"
            journal["updated_at"] = now_str()
            _save_active(journal)
            os.replace(src, dst)

        journal["phase"] = "switched"
        journal["updated_at"] = now_str()
        _save_active(journal)

        entry = config_mod.find_workspace_entry(workspace_uuid)
        if not entry:
            raise MigrationError("工作区索引条目不存在")
        entry["directory"] = str(dst)
        if not config_mod.save_workspace_meta(workspace_uuid, entry):
            if method == "rename":
                try:
                    os.replace(dst, src)
                except OSError:
                    journal["error"] = "索引写入失败，目标已就绪，待启动修复"
                    _save_active(journal)
                    keep_active = True
                    raise MigrationError(journal["error"])
            else:
                journal["error"] = "索引写入失败，目标已就绪，待启动修复"
                _save_active(journal)
                keep_active = True
                raise MigrationError(journal["error"])
            raise MigrationError("写入工作区索引失败")

        if method == "copy":
            journal["phase"] = "cleaning"
            journal["updated_at"] = now_str()
            _save_active(journal)
            try:
                shutil.rmtree(src)
            except OSError as e:
                journal["error"] = f"源目录清理失败，待下次启动继续: {e}"
                _save_active(journal)
                keep_active = True
                logger.warning(f"[relocation] {journal['error']}")
                _log_holders(src, e)
                return

        _finish_active(journal, "done")
        logger.info(f"[relocation] 工作区迁移完成: {workspace_uuid} -> {dst}")
    except MigrationCancelled as e:
        _remove_path(partial)
        _finish_active(journal, "cancelled", str(e))
        logger.info(f"[relocation] 工作区迁移已取消: {workspace_uuid}")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[relocation] 工作区迁移失败: {workspace_uuid}")
        _log_holders(src, e)
        if journal.get("phase") in ("copying", "verifying"):
            _remove_path(partial)
        if not keep_active:
            _finish_active(journal, "failed", str(e))
    finally:
        _unmark_migrating(workspace_uuid)
        _CANCEL_EVENTS.pop(migration_id, None)
        _PROCESS_LOCK.release()


# ── 启动恢复 ────────────────────────────────────────────────────

def backup_migration_config(reason: str) -> Path | None:
    """迁移前备份 setting.json / workspaces.json。

    备份到 ``data/cili/backup/migrations/{时间戳}-{reason}-{随机后缀}/``；
    两个文件都很小，失败时抛 MigrationError，调用方据此中止迁移。
    文件都不存在（全新安装）时返回 None。
    """
    sources = [Path(config_mod.GLOBAL_CONFIG_PATH), Path(config_mod.WORKSPACES_JSON)]
    existing = [path for path in sources if path.is_file()]
    if not existing:
        return None

    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = (
        Path(config_mod.DATA_DIR) / "backup" / "migrations"
        / f"{stamp}-{reason}-{uuid_mod.uuid4().hex[:4]}"
    )
    try:
        target.mkdir(parents=True, exist_ok=False)
        for path in existing:
            shutil.copy2(path, target / path.name)
    except OSError as e:
        raise MigrationError(f"迁移前备份失败: {e}", status_code=500) from e
    logger.info(f"[relocation] 迁移前配置备份: {target}")
    return target


def _read_raw_config() -> dict | None:
    """直接读 setting.json；文件不存在返回 {}，损坏返回 None（不覆盖）。"""
    path = Path(config_mod.GLOBAL_CONFIG_PATH)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"[relocation] setting.json 无法读取，跳过配置写入: {e}")
        return None
    return data if isinstance(data, dict) else None


def _save_config_section(section: dict) -> bool:
    """只更新 setting.json 的 workspaces 段；文件不存在/损坏时不写。"""
    cfg = _read_raw_config()
    if cfg is None:
        return False
    if not Path(config_mod.GLOBAL_CONFIG_PATH).is_file():
        return False
    cfg["workspaces"] = section
    ok = config_mod.save_global_config(cfg)
    if ok:
        config_mod.reset_workspaces_root_cache()
    return ok


def _is_empty_skeleton(path: Path) -> bool:
    """只含空 .cili/sessions 的自动骨架（可安全删除后重建）。"""
    if not path.is_dir() or path.is_symlink():
        return False
    for root, _dirs, files in os.walk(path):
        if files:
            return False
        rel = Path(root).relative_to(path)
        if len(rel.parts) >= 2 or (rel.parts and rel.parts[0] != "sessions"):
            return False
    return True


def _move_path(src: Path, dst: Path) -> None:
    """启动期批量搬运：同卷 rename，跨卷复制校验后删除源。"""
    if _same_volume(src.parent, dst.parent):
        os.replace(src, dst)
        return
    partial = dst.with_name(f".{dst.name}.layout-migrating")
    _remove_path(partial)
    if src.is_dir():
        shutil.copytree(src, partial, symlinks=True)
        if _measure_tree(partial) != _measure_tree(src):
            _remove_path(partial)
            raise MigrationError(f"复制校验失败: {src}")
        os.replace(partial, dst)
        shutil.rmtree(src)
    else:
        shutil.copy2(src, partial)
        os.replace(partial, dst)
        src.unlink(missing_ok=True)


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def migrate_layout_to_workspaces() -> dict:
    """把旧 ``workspace/`` 逐项迁到 ``workspaces/``（启动期幂等编排）。

    顺序：旧布局归一化 → 已登记工作区 → 非工作区 → 删除空旧根。
    返回 {"moved": [...], "skipped": [...], "leftovers": [...], "skipped_reason": ...}。
    """
    legacy_root = Path(config_mod.PROJECT_ROOT) / "workspace"
    target_root = Path(config_mod.PROJECT_ROOT) / "workspaces"
    cfg = _read_raw_config()
    section = (cfg or {}).get("workspaces") if isinstance((cfg or {}).get("workspaces"), dict) else {}

    if section.get("layout_migrated_at"):
        return {"skipped_reason": "already_migrated"}

    current_root = config_mod.workspaces_root()
    if _norm(current_root) not in (_norm(legacy_root), _norm(target_root)):
        return {"skipped_reason": "custom_root"}

    if not legacy_root.exists():
        if target_root.exists() and Path(config_mod.GLOBAL_CONFIG_PATH).is_file():
            section["root"] = to_storage_path(target_root)
            section["layout_migrated_at"] = now_str()
            _save_config_section(section)
        return {"skipped_reason": "no_legacy"}

    try:
        backup_migration_config("workspace-layout")
    except MigrationError as e:
        logger.error(f"[relocation] 布局迁移前备份失败，已跳过: {e}")
        return {"skipped_reason": "backup_failed", "error": str(e)}

    # 0. 旧版 Default 直接占用 workspace/ 根目录：先归一化到 workspace/default/
    entries = config_mod.load_workspaces_index()
    root_entries = [
        e for e in entries
        if e.get("directory") and _norm(Path(str(e["directory"]))) == _norm(legacy_root)
    ]
    if root_entries:
        try:
            from core.workspace_migration import migrate_default_workspace

            migrate_default_workspace(backup=False)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[relocation] 旧布局归一化失败，跳过批量迁移: {e}")
            return {"skipped_reason": "legacy_layout_failed"}
        entries = config_mod.load_workspaces_index()
        if any(
            e.get("directory") and _norm(Path(str(e["directory"]))) == _norm(legacy_root)
            for e in entries
        ):
            logger.error("[relocation] 旧布局仍占用 workspace/ 根目录，跳过批量迁移")
            return {"skipped_reason": "legacy_layout_failed"}

    if any(
        e.get("directory") and _norm(Path(str(e["directory"]))) == _norm(target_root)
        for e in entries
    ):
        logger.error("[relocation] workspaces/ 已被登记为工作区，跳过批量迁移")
        return {"skipped_reason": "target_is_workspace"}

    target_root.mkdir(parents=True, exist_ok=True)
    section["root"] = to_storage_path(target_root)
    _save_config_section(section)

    result: dict[str, Any] = {"moved": [], "skipped": [], "leftovers": []}
    handled: set[str] = set()
    entries = config_mod.load_workspaces_index()

    # 1. 已登记工作区（仅旧根的直接子目录）
    for entry in entries:
        uuid = str(entry.get("uuid") or "")
        if not uuid or uuid == config_mod.SYSTEM_WORKSPACE_UUID:
            continue
        directory = entry.get("directory")
        if not directory:
            continue
        src = Path(str(directory))
        if _norm(src.parent) != _norm(legacy_root):
            continue
        name = src.name
        handled.add(name)
        dst = target_root / name

        if not src.exists():
            if dst.exists():
                entry["directory"] = str(dst)
                config_mod.save_workspace_meta(uuid, entry)
                result["moved"].append(name)
            else:
                logger.warning(f"[relocation] 工作区目录缺失，跳过: {src}")
                result["skipped"].append(name)
            continue

        if dst.exists():
            if _is_empty_skeleton(dst):
                _remove_path(dst)
            else:
                logger.warning(f"[relocation] 目标已存在，跳过工作区: {dst}")
                result["skipped"].append(name)
                continue

        try:
            _move_path(src, dst)
            entry["directory"] = str(dst)
            if not config_mod.save_workspace_meta(uuid, entry):
                _move_path(dst, src)
                raise MigrationError("写入工作区索引失败")
            result["moved"].append(name)
            logger.info(f"[relocation] 工作区已迁移: {src} -> {dst}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[relocation] 工作区迁移失败，保留原位: {src}: {e}")
            _log_holders(src, e)
            result["skipped"].append(name)

    # 2. 非工作区目录/文件
    registered_dirs = [
        Path(str(e["directory"])) for e in config_mod.load_workspaces_index()
        if e.get("directory") and Path(str(e["directory"])).exists()
    ]
    for name in sorted(os.listdir(legacy_root)):
        if name in handled:
            continue
        src = legacy_root / name
        if any(_norm(d) == _norm(src) or is_path_within(str(d), str(src))
               for d in registered_dirs):
            logger.warning(f"[relocation] 目录内含已登记工作区，跳过: {src}")
            result["skipped"].append(name)
            continue
        dst = target_root / name
        if dst.exists():
            logger.warning(f"[relocation] 目标已存在，跳过: {dst}")
            result["skipped"].append(name)
            continue
        try:
            _move_path(src, dst)
            result["moved"].append(name)
            logger.info(f"[relocation] 非工作区已迁移: {src} -> {dst}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[relocation] 非工作区迁移失败，保留原位: {src}: {e}")
            _log_holders(src, e)
            result["skipped"].append(name)

    # 3. 删除空旧根；有残留则下次启动继续
    leftovers = sorted(os.listdir(legacy_root))
    result["leftovers"] = leftovers
    if not leftovers:
        try:
            os.rmdir(legacy_root)
        except OSError as e:
            logger.warning(f"[relocation] 删除旧根失败: {e}")
            result["leftovers"] = [legacy_root.name]
            return result
        if Path(config_mod.GLOBAL_CONFIG_PATH).is_file():
            section["root"] = to_storage_path(target_root)
            section["layout_migrated_at"] = now_str()
            _save_config_section(section)
    return result


def recover_pending_migration() -> None:
    """启动时修复上次中断的迁移（只做一致性收尾，不重新复制）。"""
    data = _read_journal()
    active = data.get("active")
    if not isinstance(active, dict):
        return

    workspace_uuid = str(active.get("workspace_uuid") or "")
    src = Path(str(active.get("src") or ""))
    dst = Path(str(active.get("dst") or ""))
    phase = str(active.get("phase") or "")
    migration_id = str(active.get("id") or "")
    partial = _partial_path(dst, migration_id)

    # 复制完成并已切换到正式目录、但日志还停在 verifying：按 switched 继续收尾
    if phase == "verifying" and dst.exists() and not partial.exists():
        phase = "switched"

    if phase in ("preparing", "copying", "verifying"):
        _remove_path(partial)
        _finish_active(active, "failed", "迁移中断，源目录未移动")
        logger.warning("[relocation] 上次迁移中断，已清理临时目录（源未移动）")
        return

    if phase in ("switching", "switched", "cleaning"):
        entry = config_mod.find_workspace_entry(workspace_uuid)
        if entry and dst.exists():
            current = Path(str(entry.get("directory") or ""))
            if current != dst:
                entry["directory"] = str(dst)
                if not config_mod.save_workspace_meta(workspace_uuid, entry):
                    logger.error("[relocation] 启动恢复：写入工作区索引失败")
                    return
            if src.exists():
                try:
                    _remove_path(src)
                except OSError as e:
                    logger.error(f"[relocation] 启动恢复：清理源目录失败: {e}")
                    return
            _finish_active(active, "done", "迁移已完成（启动恢复）")
            logger.info(f"[relocation] 启动恢复完成: {workspace_uuid} -> {dst}")
            return

    _finish_active(active, "failed", "无法恢复：源与目标状态不一致")
    logger.error("[relocation] 启动恢复失败：源与目标状态不一致")