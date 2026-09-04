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
from pathlib import Path

from core.tools.shared.base import Tool, ToolResult
from core.config import DATA_DIR

logger = logging.getLogger(__name__)

# State file directory
LOOP_STATE_DIR = DATA_DIR / "tools" / "loop"

# Reserved key prefix for metadata (excluded from item iteration)
_META_PREFIX = "_"


def _state_filename(task_id: str) -> str:
    """Derive a safe state filename from task_id (absolute path)."""
    return hashlib.md5(task_id.encode()).hexdigest()[:12]


def _load_state(task_id: str) -> dict:
    """Load loop state from file."""
    h = _state_filename(task_id)
    state_path = LOOP_STATE_DIR / f"{h}.json"
    if not state_path.exists():
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[loop] Failed to load state for {task_id}: {e}")
        return {}


def _save_state(task_id: str, state: dict) -> None:
    """Save loop state to file. Includes _source_file for traceability."""
    LOOP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    h = _state_filename(task_id)
    state_path = LOOP_STATE_DIR / f"{h}.json"
    try:
        # Embed source_file path in state for traceability
        state[f"{_META_PREFIX}source_file"] = task_id
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
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
    description = (
        "**Progress tracking for iterative tasks across scheduling cycles.**\n"
        "Track items through multiple execution cycles. Each item has a status: "
        '"pending", "done", or "failed:{reason}".\n'
        "The task is identified by its source file path — all actions require `source_file`.\n\n"
        "## Actions:\n"
        "- **next**: Get next pending item with progress stats (auto-loads items from source_file)\n"
        "- **done**: Mark item as completed\n"
        "- **fail**: Mark item as failed with reason\n"
        "- **status**: Get progress statistics\n\n"
        "## File Format:\n"
        "Plain text, one item per line. Empty lines and leading/trailing whitespace are ignored.\n\n"
        "## Examples:\n"
        "```python\n"
        "# Get next pending item (items auto-loaded from file)\n"
        'loop(action="next", source_file="data/files.txt")\n'
        '→ "进度: 47/1000 已完成, 0 失败, 953 待处理\\n当前项: file_048.md"\n'
        "\n"
        "# When all items are done\n"
        'loop(action="next", source_file="data/files.txt")\n'
        '→ "所有项已处理完毕 (完成: 999, 失败: 1)"\n'
        "\n"
        "# Mark as completed\n"
        'loop(action="done", source_file="data/files.txt", item="file1.md")\n'
        '→ {"done": 48, "pending": 952, "failed": 0, "total": 1000}\n'
        "\n"
        "# Mark as failed\n"
        'loop(action="fail", source_file="data/files.txt", item="file2.md", error="encoding error")\n'
        '→ {"done": 47, "pending": 952, "failed": 1, "total": 1000}\n'
        "\n"
        "# Get statistics\n"
        'loop(action="status", source_file="data/files.txt")\n'
        '→ {"total": 1000, "done": 47, "pending": 953, "failed": 0}\n'
        "```\n"
    )

    def __init__(
        self,
        cwd: str = ".",
        workspace_uuid: str = "",
        session_manager=None,
    ):
        super().__init__(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager)

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["next", "done", "fail", "status"],
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
        else:
            return ToolResult(f"Error: unknown action '{action}'", error=True)

    def _next(self, task_id: str, file_path: Path) -> ToolResult:
        """Get next pending item. Auto-loads items from source_file."""
        state = _load_state(task_id)

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
                _save_state(task_id, state)
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

        state = _load_state(task_id)

        if item not in state:
            return ToolResult(f"Error: item '{item}' not found in state", error=True)

        state[item] = "done"
        _save_state(task_id, state)

        counts = _count(state)
        return ToolResult(json.dumps(counts, ensure_ascii=False))

    def _fail(self, task_id: str, item: str | None, error: str | None) -> ToolResult:
        """Mark item as failed with reason."""
        if not item:
            return ToolResult("Error: 'item' is required for fail action", error=True)

        state = _load_state(task_id)

        if item not in state:
            return ToolResult(f"Error: item '{item}' not found in state", error=True)

        # Store as "failed:{reason}"
        reason = error or "unknown"
        state[item] = f"failed:{reason}"
        _save_state(task_id, state)

        counts = _count(state)
        return ToolResult(json.dumps(counts, ensure_ascii=False))

    def _status(self, task_id: str) -> ToolResult:
        """Get progress statistics."""
        state = _load_state(task_id)
        counts = _count(state)
        return ToolResult(json.dumps(counts, ensure_ascii=False))
