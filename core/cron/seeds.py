"""全局任务种子：安装到 System 工作区，并按 revision/checksum 轻量对账。

种子文件：core/cron_seeds/{id}.json（v2 格式，managed=true）。
设计见 workspace/default/cron-redesign-plan-2026-11-14.md 第 4.5 节。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT
from core.cron import rules, store
from core.cron.model import Job, State, now_str
from core.fs_utils import load_json_or_backup

logger = logging.getLogger(__name__)

SYSTEM_WORKSPACE = "system"
SEED_DIR = Path(PROJECT_ROOT) / "core" / "cron_seeds"


def seed_files() -> list[Path]:
    if not SEED_DIR.exists():
        return []
    return sorted(SEED_DIR.glob("*.json"))


def seed_ids() -> list[str]:
    ids: list[str] = []
    for path in seed_files():
        data = load_json_or_backup(path, None)
        if isinstance(data, dict) and data.get("id"):
            ids.append(str(data["id"]))
        else:
            ids.append(path.stem)
    return ids


def managed_checksum(job: Job) -> str:
    payload = json.dumps(
        job.managed_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_seed(path: Path) -> Job:
    data = load_json_or_backup(path, None)
    if not isinstance(data, dict):
        raise ValueError(f"invalid seed file: {path}")
    job = Job.from_dict(data, workspace_uuid=SYSTEM_WORKSPACE)
    job.managed = True
    job.scope = "global"
    job.workspace_uuid = SYSTEM_WORKSPACE
    job.seed_revision = int(data.get("seed_revision", 1) or 1)
    job.seed_checksum = managed_checksum(job)
    job.version = 2
    rules.validate_schedule(job)
    return job


def _default_state(job: Job) -> State:
    return State(
        remaining=int(job.execution.get("max_executions", 9999)),
    )


def _install(job: Job, reason: str) -> None:
    store.save_job(job)
    state_file = store.state_path(job.workspace_uuid, job.id)
    if not state_file.exists():
        store.save_state(job.workspace_uuid, job.id, _default_state(job))
    logger.info("[cron-seed] installed %s (%s, rev=%s)", job.id, reason, job.seed_revision)


def install_seeds() -> list[dict[str, Any]]:
    """启动时安装/升级全局任务；无变化不写文件。"""
    results: list[dict[str, Any]] = []
    if not SEED_DIR.exists():
        logger.warning("[cron-seed] seed directory not found: %s", SEED_DIR)
        return results

    store.ensure_root(SYSTEM_WORKSPACE)
    for path in seed_files():
        try:
            seed = _load_seed(path)
        except Exception as exc:  # noqa: BLE001
            logger.error("[cron-seed] invalid seed %s: %s", path, exc)
            results.append({"id": path.stem, "status": "invalid", "error": str(exc)})
            continue

        job_file = store.job_path(SYSTEM_WORKSPACE, seed.id)
        if not job_file.exists():
            _install(seed, "missing")
            results.append({"id": seed.id, "status": "installed"})
            continue

        try:
            existing = Job.from_dict(
                load_json_or_backup(job_file, {}), workspace_uuid=SYSTEM_WORKSPACE
            )
        except Exception as exc:  # noqa: BLE001 —— 损坏：备份后重装
            backup = job_file.with_name(
                f"job.json.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            )
            try:
                job_file.rename(backup)
            except OSError:
                pass
            logger.error("[cron-seed] %s corrupt (%s), reinstalling", seed.id, exc)
            _install(seed, "corrupt")
            results.append({"id": seed.id, "status": "reinstalled"})
            continue

        if seed.seed_revision > existing.seed_revision:
            seed.created_at = existing.created_at
            seed.updated_at = now_str()
            store.save_job(seed)
            logger.info(
                "[cron-seed] upgraded %s rev %s -> %s",
                seed.id,
                existing.seed_revision,
                seed.seed_revision,
            )
            results.append({"id": seed.id, "status": "upgraded"})
            continue

        if seed.seed_revision < existing.seed_revision:
            logger.warning(
                "[cron-seed] %s seed revision %s < installed %s, keeping installed",
                seed.id,
                seed.seed_revision,
                existing.seed_revision,
            )
            results.append({"id": seed.id, "status": "downgrade_ignored"})
            continue

        if seed.seed_checksum != managed_checksum(existing):
            seed.created_at = existing.created_at
            seed.updated_at = now_str()
            store.save_job(seed)
            logger.error(
                "[cron-seed] %s managed definition drift detected, restored from seed",
                seed.id,
            )
            results.append({"id": seed.id, "status": "restored"})
            continue

        results.append({"id": seed.id, "status": "unchanged"})

    return results
