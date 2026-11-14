"""v1 → v2 单向迁移。

原则（见设计文档第十节）：
- 成功后删除 v1 数据；不做兼容读取、不回滚；
- 单 workspace 全有或全无：任一任务转换失败则保留旧数据并重试；
- 用 staging 目录构建后整目录 rename，避免半途状态。
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import get_all_workspace_uuids, get_workspace_data_dir
from core.cron import store, v1
from core.cron.model import now_str
from core.fs_utils import atomic_write_json, load_json_or_backup

logger = logging.getLogger(__name__)


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _cleanup_legacy(workspace_uuid: str) -> None:
    legacy = v1.legacy_dir(workspace_uuid)
    tasks_file = legacy / "user_tasks.json"
    try:
        if tasks_file.exists():
            tasks_file.unlink()
    except OSError as exc:
        logger.warning("[cron-migrate] failed to delete %s: %s", tasks_file, exc)
    state_dir = legacy / "state"
    if state_dir.exists():
        try:
            shutil.rmtree(state_dir)
        except OSError as exc:
            logger.warning("[cron-migrate] failed to delete %s: %s", state_dir, exc)
    _rmdir_if_empty(legacy)


def migrate_workspace(workspace_uuid: str = "") -> dict[str, Any]:
    """迁移单个 workspace 的 v1 用户任务。"""
    marker = store.migrate_marker(workspace_uuid)
    if marker.exists():
        _cleanup_legacy(workspace_uuid)
        return {"workspace_uuid": workspace_uuid, "status": "already"}

    tasks = v1.load_v1_tasks(workspace_uuid)
    if not tasks:
        root = store.ensure_root(workspace_uuid)
        atomic_write_json(
            marker,
            {
                "version": 2,
                "migrated_at": now_str(),
                "source": None,
                "jobs": 0,
                "deleted_legacy": False,
            },
        )
        logger.info("[cron-migrate] %s: no v1 user tasks", workspace_uuid or "default")
        return {"workspace_uuid": workspace_uuid, "status": "empty", "jobs": 0}

    converted: list[tuple[Any, Any]] = []
    for config in tasks:
        name = config.get("name") or config.get("task_id") or "<unnamed>"
        try:
            job = v1.convert_job(config, workspace_uuid)
            state = v1.convert_state(workspace_uuid, job)
        except Exception as exc:  # noqa: BLE001 —— 全有或全无
            logger.error(
                "[cron-migrate] %s: task %r conversion failed, aborting: %s",
                workspace_uuid or "default",
                name,
                exc,
            )
            raise v1.MigrationError(f"task {name!r}: {exc}") from exc
        converted.append((job, state))

    data_dir = get_workspace_data_dir(workspace_uuid)
    staging = data_dir / f".cron-staging-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        for job, state in converted:
            job_dir = staging / job.id
            job_dir.mkdir(parents=True, exist_ok=True)
            if isinstance(job.content, str) and job.content.strip().endswith(".py"):
                source = v1.legacy_dir(workspace_uuid) / job.content.strip()
                if source.exists():
                    target_name = source.name
                    shutil.copy2(source, job_dir / target_name)
                    job.content = target_name
            atomic_write_json(job_dir / "job.json", job.to_dict())
            atomic_write_json(job_dir / "state.json", state.to_dict())
        atomic_write_json(
            staging / ".migrated.json",
            {
                "version": 2,
                "migrated_at": now_str(),
                "source": ".cili/cron.d/user_tasks.json",
                "jobs": len(converted),
                "deleted_legacy": True,
            },
        )

        root = store.cron_root(workspace_uuid)
        if root.exists() and not store.migrate_marker(workspace_uuid).exists():
            shutil.rmtree(root)
        staging.rename(root)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    _cleanup_legacy(workspace_uuid)
    logger.info(
        "[cron-migrate] %s: migrated %d task(s), legacy deleted",
        workspace_uuid or "default",
        len(converted),
    )
    return {
        "workspace_uuid": workspace_uuid,
        "status": "migrated",
        "jobs": len(converted),
    }


def _legacy_state_to_dict(data: dict[str, Any]) -> dict[str, Any]:
    """v1 state → v2 state 字段（system 任务专用）。"""
    return {
        "version": 2,
        "next_run": None,
        "last_run": data.get("last_run"),
        "last_status": data.get("last_status"),
        "last_error": data.get("last_error"),
        "last_duration_ms": data.get("last_duration_ms"),
        "run_count": int(data.get("run_count", 0) or 0),
        "remaining": data.get("remaining"),
        "last_slot": data.get("last_slot"),
        "running": None,
        "missed_count": int(data.get("missed_count", 0) or 0),
        "cron_session_id": str(
            data.get("cron_session_id") or data.get("session_id") or ""
        ),
        "last_delivery": None,
        "history": [],
    }


def _last_run_key(data: dict[str, Any], path: Path) -> str:
    value = data.get("last_run")
    if value:
        return str(value)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return ""


def migrate_system_states(seed_ids: list[str]) -> None:
    """把散落在 System / 默认工作区的 v1 系统任务 state 收拢到 data/.cili/cron/。"""
    if not seed_ids:
        return
    candidates = [
        get_workspace_data_dir("system") / "cron.d" / "state",
        get_workspace_data_dir("") / "cron.d" / "state",
    ]
    target_root = store.cron_root("system")

    for job_id in seed_ids:
        best: tuple[str, Path, dict[str, Any]] | None = None
        for state_dir in candidates:
            path = state_dir / f"{job_id}.json"
            data = load_json_or_backup(path, None) if path.exists() else None
            if not isinstance(data, dict):
                continue
            key = _last_run_key(data, path)
            if best is None or key > best[0]:
                best = (key, path, data)

        if best is not None:
            target = target_root / job_id / "state.json"
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(target, _legacy_state_to_dict(best[2]))
                logger.info("[cron-migrate] system state %s migrated from %s", job_id, best[1])

        for state_dir in candidates:
            path = state_dir / f"{job_id}.json"
            if path.exists():
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning("[cron-migrate] failed to delete %s: %s", path, exc)

    for state_dir in candidates:
        _rmdir_if_empty(state_dir)


def migrate_all() -> list[dict[str, Any]]:
    """启动时执行全部迁移（幂等）。"""
    from core.cron import seeds

    reports: list[dict[str, Any]] = []
    seed_ids = seeds.seed_ids()
    try:
        migrate_system_states(seed_ids)
    except Exception as exc:  # noqa: BLE001
        logger.error("[cron-migrate] system state migration failed: %s", exc)

    for workspace_uuid in get_all_workspace_uuids():
        try:
            reports.append(migrate_workspace(workspace_uuid))
        except Exception as exc:  # noqa: BLE001 —— 单 workspace 失败不阻塞其它
            logger.error(
                "[cron-migrate] workspace %s migration failed: %s",
                workspace_uuid or "default",
                exc,
            )
            reports.append(
                {"workspace_uuid": workspace_uuid, "status": "failed", "error": str(exc)}
            )
    return reports
