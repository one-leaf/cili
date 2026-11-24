"""合并升级迁移框架：有序、幂等、可回滚提示的启动期数据迁移。

设计要点（refactor-scan §5）：
- ``data/cili/schema_version.json`` 记录已完成的步骤 id；已记录的步骤不再执行。
- 每个步骤返回 True 表示完成并记入版本文件；返回 False 表示「不适用」同样记入。
- 单个步骤失败：配置/凭证类失败中止启动（由调用方抛出），会话类步骤可跳过并告警。
- 步骤实现集中在 ``core.migrations.steps``；新增破坏性契约变更时在此追加。

本模块只做编排；具体数据搬运不在此处。
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

Step = Callable[[], bool]

_VERSION_FILE = "schema_version.json"


def _version_path(data_dir: Path) -> Path:
    return Path(data_dir) / _VERSION_FILE


def load_completed(data_dir: Path) -> list[str]:
    path = _version_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    steps = data.get("steps") if isinstance(data, dict) else None
    return [str(s) for s in steps] if isinstance(steps, list) else []


def _save_completed(data_dir: Path, steps: list[str]) -> None:
    from core.fs_utils import atomic_write_json

    atomic_write_json(_version_path(data_dir), {"steps": steps})


_BACKUP_FILES = ("setting.json", "workspaces.json")


def _backup_config_files(data_dir: Path) -> Path | None:
    """迁移前把小型配置文件复制到备份目录（按时间戳，不覆盖旧备份）。"""
    existing = [n for n in _BACKUP_FILES if (data_dir / n).is_file()]
    if not existing:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = data_dir / "backup" / "migrations" / stamp
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in existing:
            shutil.copy2(data_dir / name, target / name)
    except OSError as e:
        logger.warning("[migration] 备份配置失败: %s", e)
        return None
    logger.info("[migration] 配置备份: %s", target)
    return target


def run_pending(steps: list[tuple[str, Step]], data_dir: Path) -> list[str]:
    """按顺序执行未完成的步骤；返回本次执行的步骤 id。

    首次执行任一步骤前，把 setting.json / workspaces.json 复制到
    ``data/cili/backup/migrations/{时间戳}/``（失败只告警，不阻断）。
    """
    done = load_completed(data_dir)
    executed: list[str] = []
    if not all(step_id in done for step_id, _ in steps):
        _backup_config_files(Path(data_dir))
    for step_id, step in steps:
        if step_id in done:
            continue
        try:
            step()
        except Exception as e:  # noqa: BLE001 —— 记录后由调用方决定是否中止
            logger.error(f"[migration] step {step_id} failed: {e}")
            raise
        done.append(step_id)
        executed.append(step_id)
        _save_completed(data_dir, done)
        logger.info(f"[migration] step {step_id} completed")
    return executed