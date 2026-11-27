"""Loop tool - progress tracking for iterative tasks.

Tracks progress of items through multiple execution cycles.
Each item has a status: "pending", "done", or "failed:{reason}".
The task is identified by its source file path.

Usage:
    loop(action="next", source_file="file_list.txt")  # Get next pending item (auto-loads from file)
    loop(action="done", source_file="file_list.txt", item="file1.md")  # Mark as completed
    loop(action="fail", source_file="file_list.txt", item="file2.md", error="encoding error")
    loop(action="status", source_file="file_list.txt")  # Get progress statistics
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path

from core.config import get_workspace_tools_dir
from core.fs_utils import atomic_write_json, load_json_or_backup
from core.paths import to_storage_path
from core.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

# Reserved key prefix for metadata (excluded from item iteration)
_META_PREFIX = "_"


def _get_loop_state_dir(workspace_uuid: str = "") -> Path:
    """Get the loop state directory for a workspace."""
    return get_workspace_tools_dir(workspace_uuid) / "loop"


def _state_filename(state_key: str) -> str:
    """Derive a safe state filename from the portable state key."""
    return hashlib.md5(state_key.encode()).hexdigest()[:12]


def _state_key(task_id: str, workspace_root: str | Path | None = None) -> str:
    """状态键：优先相对工作区根，其次相对项目根，最后绝对路径。

    状态文件本身就在工作区 .cili/tools/loop/ 下；用相对键后，整个工作区
    （乃至整个 cili 目录）搬家后仍能匹配到原任务。
    """
    if workspace_root:
        try:
            root = os.path.abspath(str(workspace_root))
            target = os.path.abspath(task_id)
            root_key = os.path.normcase(root)
            target_key = os.path.normcase(target)
            if target_key == root_key or target_key.startswith(root_key + os.sep):
                rel = os.path.relpath(target, root)
                return "ws:" + Path(rel).as_posix()
        except (OSError, ValueError):
            pass
    try:
        return "root:" + to_storage_path(task_id)
    except ValueError:
        return "abs:" + task_id


# 同一任务的加载-修改-保存串行化：多 agent 同时操作同一 source_file 时
# 避免读-改-写竞态丢更新（SEC-20 系）。加载/保存本身已原子，但 read-modify-write
# 需要进程内互斥。
_state_locks: dict[str, threading.Lock] = {}
_state_locks_guard = threading.Lock()


def _get_state_lock(task_id: str) -> threading.Lock:
    """Get or create the per-task state lock (双重检查加锁)。"""
    with _state_locks_guard:
        if task_id not in _state_locks:
            _state_locks[task_id] = threading.Lock()
        return _state_locks[task_id]


def _load_state(task_id: str, workspace_uuid: str = "",
                workspace_root: str | Path | None = None) -> dict:
    """Load loop state from file（兼容旧版绝对路径键）。"""
    state_dir = _get_loop_state_dir(workspace_uuid)
    candidates = [_state_key(task_id, workspace_root), task_id]
    seen: set[str] = set()
    for key in candidates:
        if key in seen:
            continue
        seen.add(key)
        state_path = state_dir / f"{_state_filename(key)}.json"
        if state_path.exists():
            return load_json_or_backup(state_path, {})
    return {}


def _save_state(task_id: str, state: dict, workspace_uuid: str = "",
                workspace_root: str | Path | None = None) -> None:
    """Save loop state to file. Includes portable source key for traceability."""
    try:
        key = _state_key(task_id, workspace_root)
        state[f"{_META_PREFIX}source_file"] = key
        state[f"{_META_PREFIX}source_file_abs"] = task_id
        h = _state_filename(key)
        state_dir = _get_loop_state_dir(workspace_uuid)
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / f"{h}.json"
        atomic_write_json(state_path, state)
    except Exception as e:
        logger.error(f"[loop] Failed to save state for {task_id}: {e}")


def _iter_items(state: dict) -> dict:
    """Return only real items (exclude metadata keys starting with _)."""
    return {k: v for k, v in state.items() if not k.startswith(_META_PREFIX)}


def _count(state: dict) -> dict:
    """Count items by status (excludes metadata keys)."""
    items = _iter_items(state)
    total = len(items)
    done = sum(1 for v in items.values() if v == "done")
    pending = sum(1 for v in items.values() if v == "pending")
    failed = sum(1 for v in items.values() if isinstance(v, str) and v.startswith("failed:"))
    return {"total": total, "done": done, "pending": pending, "failed": failed}


class LoopTool(Tool):
    name = "loop"
    search_keywords = ("循环", "迭代", "进度", "loop", "iteration")
    description = (
        "**Progress tracking for iterative tasks across scheduling cycles.**\n"
        "Tracks items through cycles; each item is pending / done / failed:{reason}.\n"
        "The task is identified by its source_file path — all actions require it.\n\n"
        "## Actions:\n"
        "- next: get next pending item + progress stats (auto-loads items from source_file)\n"
        "- done / fail: mark an item completed, or failed with a reason\n"
        "- status: progress statistics\n"
        "- reset: clear all progress (items re-queue as pending on next `next`)\n\n"
        "## File format:\n"
        "Plain text, one item per line; empty lines and surrounding whitespace ignored.\n"
        "Example: loop(action=\"next\", source_file=\"data/files.txt\")"
    )

    def __init__(
        self,
        cwd: str = ".",
        workspace_uuid: str = "",
        session=None,
        permission_rules=None,
    ):
        super().__init__(
            cwd=cwd, workspace_uuid=workspace_uuid, session=session,
            permission_rules=permission_rules,
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["next", "done", "fail", "status", "reset"],
                    "description": "Action to perform.",
                },
                "source_file": {
                    "type": "string",
                    "description": (
                        "Path to the items file (one item per line). "
                        "Used as the task identifier — the same file path identifies the same task. "
                        "Supports absolute paths or relative to cwd."
                    ),
                },
                "item": {
                    "type": "string",
                    "description": "Item identifier for done/fail actions.",
                },
                "error": {
                    "type": "string",
                    "description": "Error reason for fail action.",
                },
            },
            "required": ["action", "source_file"],
        }

    def execute(
        self,
        action: str = "status",
        source_file: str | None = None,
        item: str | None = None,
        error: str | None = None,
    ) -> ToolResult:
        """Execute loop action."""
        if not source_file:
            return ToolResult(
                "Error: 'source_file' is required. "
                "Provide the path to the items file (one item per line).",
                error=True,
            )

        # Resolve to absolute path
        file_path = Path(source_file)
        if not file_path.is_absolute():
            file_path = Path(self.cwd) / file_path
        tid = str(file_path.resolve())

        if action == "next":
            return self._next(tid, file_path)
        elif action == "done":
            return self._done(tid, item)
        elif action == "fail":
            return self._fail(tid, item, error)
        elif action == "status":
            return self._status(tid)
        elif action == "reset":
            return self._reset(tid)
        else:
            return ToolResult(f"Error: unknown action '{action}'", error=True)

    def _next(self, task_id: str, file_path: Path) -> ToolResult:
        """Get next pending item. Auto-loads items from source_file."""
        ws = self.workspace_uuid
        with _get_state_lock(task_id):
            state = _load_state(task_id, ws, self.cwd)

            # Auto-sync items from file on first call or when file is newer
            if file_path.exists():
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        items = [line.strip() for line in f if line.strip()]
                except Exception as e:
                    return ToolResult(f"Error: failed to read file: {e}", error=True)

                # Add new items as pending (idempotent)
                added = 0
                for item in items:
                    if item not in state:
                        state[item] = "pending"
                        added += 1

                if added > 0:
                    _save_state(task_id, state, ws, self.cwd)
            elif not state:
                return ToolResult(f"Error: file not found: {file_path}", error=True)

            # Find first pending item (skip metadata keys)
            for item, status in _iter_items(state).items():
                if status == "pending":
                    # 附带进度统计，让 agent 始终知道任务进展
                    counts = _count(state)
                    lines = [
                        f"进度: {counts['done']}/{counts['total']} 已完成, "
                        f"{counts['failed']} 失败, {counts['pending']} 待处理",
                        f"当前项: {item}",
                    ]
                    return ToolResult("\n".join(lines))

            # No pending items
            counts = _count(state)
            return ToolResult(f"所有项已处理完毕 (完成: {counts['done']}, 失败: {counts['failed']})")

    def _done(self, task_id: str, item: str | None) -> ToolResult:
        """Mark item as completed."""
        if not item:
            return ToolResult("Error: 'item' is required for done action", error=True)

        ws = self.workspace_uuid
        with _get_state_lock(task_id):
            state = _load_state(task_id, ws, self.cwd)

            if item not in state:
                return ToolResult(f"Error: item '{item}' not found in state", error=True)

            state[item] = "done"
            _save_state(task_id, state, ws, self.cwd)

            counts = _count(state)
            return ToolResult(json.dumps(counts, ensure_ascii=False))

    def _fail(self, task_id: str, item: str | None, error: str | None) -> ToolResult:
        """Mark item as failed with reason."""
        if not item:
            return ToolResult("Error: 'item' is required for fail action", error=True)

        ws = self.workspace_uuid
        with _get_state_lock(task_id):
            state = _load_state(task_id, ws, self.cwd)

            if item not in state:
                return ToolResult(f"Error: item '{item}' not found in state", error=True)

            # Store as "failed:{reason}"
            reason = error or "unknown"
            state[item] = f"failed:{reason}"
            _save_state(task_id, state, ws, self.cwd)

            counts = _count(state)
            return ToolResult(json.dumps(counts, ensure_ascii=False))

    def _status(self, task_id: str) -> ToolResult:
        """Get progress statistics."""
        state = _load_state(task_id, self.workspace_uuid, self.cwd)
        counts = _count(state)
        return ToolResult(json.dumps(counts, ensure_ascii=False))

    def _reset(self, task_id: str) -> ToolResult:
        """Clear all item progress, keeping metadata keys (T19).

        源文件删除/重跑时清理残留状态；下次 next 会按文件重新加载为 pending。
        """
        ws = self.workspace_uuid
        with _get_state_lock(task_id):
            state = _load_state(task_id, ws, self.cwd)
            cleared = len(_iter_items(state))
            state = {k: v for k, v in state.items() if k.startswith(_META_PREFIX)}
            _save_state(task_id, state, ws, self.cwd)
            return ToolResult(
                f"已重置 {cleared} 项进度（任务元数据保留）"
            )
