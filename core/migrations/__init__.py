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


def run_pending(steps: list[tuple[str, Step]], data_dir: Path) -> list[str]:
    """按顺序执行未完成的步骤；返回本次执行的步骤 id。"""
    done = load_completed(data_dir)
    executed: list[str] = []
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