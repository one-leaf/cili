"""Cron v2 文件存储：每个任务一个目录。

布局：
    {workspace}/.cili/cron/
      .migrated.json
      {id}/job.json
      {id}/state.json
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

from core.config import get_workspace_data_dir
from core.cron.model import Job, State
from core.fs_utils import atomic_write_json, load_json_or_backup

logger = logging.getLogger(__name__)

_config_lock = threading.RLock()
_job_locks: dict[tuple[str, str], threading.RLock] = {}
_job_locks_guard = threading.Lock()


def config_lock() -> threading.RLock:
    """cron 配置/状态的进程级可重入锁（scheduler、工具、REST 共用）。"""
    return _config_lock


def job_lock(workspace_uuid: str, job_id: str) -> threading.RLock:
    with _job_locks_guard:
        key = (workspace_uuid or "", job_id)
        lock = _job_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _job_locks[key] = lock
        return lock


def cron_root(workspace_uuid: str = "") -> Path:
    return get_workspace_data_dir(workspace_uuid) / "cron"


def job_dir(workspace_uuid: str, job_id: str) -> Path:
    return cron_root(workspace_uuid) / job_id


def job_path(workspace_uuid: str, job_id: str) -> Path:
    return job_dir(workspace_uuid, job_id) / "job.json"


def state_path(workspace_uuid: str, job_id: str) -> Path:
    return job_dir(workspace_uuid, job_id) / "state.json"


def ensure_root(workspace_uuid: str = "") -> Path:
    root = cron_root(workspace_uuid)
    root.mkdir(parents=True, exist_ok=True)
    return root


def list_job_ids(workspace_uuid: str = "") -> list[str]:
    root = cron_root(workspace_uuid)
    if not root.exists():
        return []
    ids: list[str] = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / "job.json").exists():
            ids.append(child.name)
    return sorted(ids)


def load_job(workspace_uuid: str, job_id: str) -> Job | None:
    path = job_path(workspace_uuid, job_id)
    if not path.exists():
        return None
    data = load_json_or_backup(path, None)
    if not isinstance(data, dict):
        raise ValueError(f"invalid job file: {path}")
    return Job.from_dict(data, workspace_uuid=workspace_uuid)


def list_jobs(workspace_uuid: str = "") -> list[Job]:
    jobs: list[Job] = []
    for job_id in list_job_ids(workspace_uuid):
        try:
            job = load_job(workspace_uuid, job_id)
        except Exception as exc:  # noqa: BLE001 —— 单个坏任务不拖垮整表
            logger.error("[cron] failed to load job %s/%s: %s", workspace_uuid, job_id, exc)
            continue
        if job is not None:
            jobs.append(job)
    return jobs


def save_job(job: Job) -> Path:
    path = job_path(job.workspace_uuid, job.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, job.to_dict())
    return path


def delete_job(workspace_uuid: str, job_id: str) -> None:
    path = job_dir(workspace_uuid, job_id)
    if path.exists():
        shutil.rmtree(path)


def load_state(workspace_uuid: str, job_id: str) -> State:
    path = state_path(workspace_uuid, job_id)
    if not path.exists():
        return State()
    try:
        return State.from_dict(load_json_or_backup(path, {}))
    except Exception as exc:  # noqa: BLE001
        logger.error("[cron] invalid state %s/%s: %s", workspace_uuid, job_id, exc)
        return State()


def save_state(workspace_uuid: str, job_id: str, state: State) -> Path:
    path = state_path(workspace_uuid, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, state.to_dict())
    return path


def migrate_marker(workspace_uuid: str = "") -> Path:
    return cron_root(workspace_uuid) / ".migrated.json"
