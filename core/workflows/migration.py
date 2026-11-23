"""工作流数据迁移：旧 `.cili/tasks` → workflows（一次性，幂等）。"""

from __future__ import annotations

import logging
import re
import shutil

from core.config import get_workspace_data_dir
from core.fs_utils import atomic_write_json, load_json_or_backup

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[0-9a-f]{8}$")
_migrated_workspaces: set[str] = set()


def migrate_legacy(workspace_uuid: str) -> None:
    """把旧版 `.cili/tasks`（task.json / subtasks）迁移到 workflows。"""
    key = workspace_uuid or ""
    if key in _migrated_workspaces:
        return
    data_dir = get_workspace_data_dir(workspace_uuid)
    old_root = data_dir / "tasks"
    new_root = data_dir / "workflows"
    if old_root.is_dir() and not new_root.exists():
        try:
            old_root.rename(new_root)
        except OSError:
            try:
                shutil.move(str(old_root), str(new_root))
            except OSError as exc:
                logger.warning("[workflows] 旧目录迁移失败: %s", exc)
    if new_root.is_dir() and not (new_root / ".legacy-migrated").exists():
        for directory in new_root.iterdir():
            if not directory.is_dir() or not _ID_RE.match(directory.name):
                continue
            legacy = directory / "task.json"
            current = directory / "workflow.json"
            if legacy.is_file() and not current.exists():
                data = load_json_or_backup(legacy, {})
                if isinstance(data, dict):
                    if "subtasks" in data and "steps" not in data:
                        data["steps"] = data.pop("subtasks")
                    try:
                        atomic_write_json(current, data)
                        legacy.unlink()
                    except OSError as exc:
                        logger.warning("[workflows] 迁移 %s 失败: %s", legacy, exc)
            state_file = directory / "state.json"
            if state_file.is_file():
                state = load_json_or_backup(state_file, {})
                if isinstance(state, dict) and (
                        state.get("panel_session_id") or state.get("results_session_id")):
                    state["panel_session_id"] = ""
                    state["results_session_id"] = ""
                    try:
                        atomic_write_json(state_file, state)
                    except OSError as exc:
                        logger.warning("[workflows] 迁移 %s 失败: %s", state_file, exc)
        try:
            (new_root / ".legacy-migrated").write_text("1", encoding="utf-8")
        except OSError:
            pass
    _migrated_workspaces.add(key)
