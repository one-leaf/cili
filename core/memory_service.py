"""记忆服务编排：回合后提取调度、开关读取、全工作区整合。"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from core.config import (
    SYSTEM_WORKSPACE_UUID,
    get_workspace_data_dir,
    get_workspace_meta,
    load_workspaces_index,
)
from core.memory_pipeline import run_consolidation, run_extraction

logger = logging.getLogger(__name__)

def schedule_extraction(workspace_uuid: str, session_id: str, messages: list[dict]) -> threading.Thread:
    """在后台守护线程中触发提取（web 回合结束后钩子）。不阻塞 SSE 流。"""
    def _run() -> None:
        try:
            run_extraction(workspace_uuid, session_id, messages)
        except Exception:
            logger.exception("memory extraction failed for session %s", session_id)

    thread = threading.Thread(target=_run, name="memory-extract", daemon=True)
    thread.start()
    return thread


def _iter_workspace_uuids() -> list[str]:
    """扫描 workspaces.json 中带 .cili/memory/ 的工作区 uuid（排除 system）。"""
    uuids: list[str] = []
    try:
        for entry in load_workspaces_index():
            uuid = entry.get("uuid", "")
            if uuid == SYSTEM_WORKSPACE_UUID:
                continue
            if (get_workspace_data_dir(uuid) / "memory").is_dir():
                uuids.append(uuid)
    except OSError:
        pass
    return uuids


def memory_enabled(workspace_uuid: str) -> bool:
    """记忆功能开关：workspaces.json 中该工作区条目的 memory_enabled 字段。

    缺省为 False——纯工作区隔离，除非用户在记忆管理页显式开启。
    """
    cfg = get_workspace_meta(workspace_uuid)
    return bool(cfg.get("memory_enabled", False))


def consolidate_all(consolidator: Callable | None = None, limit: int = 20) -> list[dict]:
    """整合所有开启记忆的工作区（cron / 手动触发）。返回每工作区结果。"""
    results: list[dict] = []
    for uuid in _iter_workspace_uuids():
        if not memory_enabled(uuid):
            continue
        try:
            results.append({"workspace_uuid": uuid, **run_consolidation(uuid, consolidator=consolidator, limit=limit)})
        except Exception as e:
            logger.warning("consolidate workspace %s failed: %s", uuid, e)
            results.append({"workspace_uuid": uuid, "error": str(e)})
    return results
