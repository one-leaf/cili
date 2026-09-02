"""Loop tool - progress tracking for iterative tasks across scheduling cycles.

Tracks progress of items (files, records, etc.) through multiple execution cycles.
Each item has a status: "pending", "done", or "failed:{reason}".

Usage:
    loop(action="sync", items=["file1.md", "file2.md"])  # Sync item list
    loop(action="next")  # Get next pending item
    loop(action="done", item="file1.md")  # Mark as completed
    loop(action="fail", item="file2.md", error="encoding error")  # Mark as failed
    loop(action="status")  # Get progress statistics
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.tools.shared.base import Tool, ToolResult
from core.config import DATA_DIR

logger = logging.getLogger(__name__)

# State file directory
LOOP_STATE_DIR = DATA_DIR / "tools" / "loop"


def _load_state(task_id: str) -> dict:
    """Load loop state from file."""
    state_path = LOOP_STATE_DIR / f"{task_id}.json"
    if not state_path.exists():
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[loop] Failed to load state for {task_id}: {e}")
        return {}


def _save_state(task_id: str, state: dict) -> None:
    """Save loop state to file."""
    LOOP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = LOOP_STATE_DIR / f"{task_id}.json"
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[loop] Failed to save state for {task_id}: {e}")


def _sync_cron_remaining(cron_task_id: str, pending_count: int) -> None:
    """Sync cron remaining counter when triggered by cron.

    Updates cron state's remaining counter to pending_count + 1 (includes current execution).
    """
    from core.cron import CRON_STATE_DIR

    cron_state_path = CRON_STATE_DIR / f"{cron_task_id}.json"
    if not cron_state_path.exists():
        return

    try:
        with open(cron_state_path, "r", encoding="utf-8") as f:
            cron_state = json.load(f)

        # Update remaining to pending_count + 1 (current execution included)
        cron_state["remaining"] = pending_count + 1

        with open(cron_state_path, "w", encoding="utf-8") as f:
            json.dump(cron_state, f, indent=2, ensure_ascii=False)

        logger.debug(f"[loop] Synced cron remaining={pending_count + 1} for {cron_task_id}")
    except Exception as e:
        logger.warning(f"[loop] Failed to sync cron remaining: {e}")


class LoopTool(Tool):
    name = "loop"
    description = (
        "**Progress tracking for iterative tasks across scheduling cycles.**\n"
        "Track items through multiple execution cycles. Each item has a status: "
        '"pending", "done", or "failed:{reason}".\n\n'
        "## Actions:\n"
        "- **sync**: Sync item list (idempotent - only adds new items as pending)\n"
        "- **next**: Get next pending item\n"
        "- **done**: Mark item as completed\n"
        "- **fail**: Mark item as failed with reason\n"
        "- **status**: Get progress statistics\n\n"
        "## Usage with Cron:\n"
        "When triggered by cron, the tool automatically syncs the remaining execution counter. "
        "If all items are processed, the cron task will auto-disable.\n\n"
        "## Examples:\n"
        "```python\n"
        "# Sync file list\n"
        'loop(action="sync", items=["file1.md", "file2.md"])\n'
        '→ {"added": 2, "total": 2, "pending": 2, "done": 0, "failed": 0}\n'
        "\n"
        "# Get next pending item\n"
        'loop(action="next")\n'
        '→ {"item": "file1.md"} or {"item": null}\n'
        "\n"
        "# Mark as completed\n"
        'loop(action="done", item="file1.md")\n'
        '→ {"done": 1, "pending": 1, "failed": 0}\n'
        "\n"
        "# Mark as failed\n"
        'loop(action="fail", item="file2.md", error="encoding error")\n'
        '→ {"done": 1, "pending": 0, "failed": 1}\n'
        "\n"
        "# Get statistics\n"
        'loop(action="status")\n'
        '→ {"total": 2, "done": 1, "pending": 0, "failed": 1}\n'
        "```\n"
    )

    def __init__(
        self,
        cwd: str = ".",
        workspace_uuid: str = "",
        session_manager=None,
        cron_task_id: str = "",
    ):
        super().__init__(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager)
        self.cron_task_id = cron_task_id

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["sync", "next", "done", "fail", "status"],
                    "description": "Action to perform.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task identifier (defaults to cron task_id if triggered by cron).",
                },
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Item list for sync action.",
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
            "required": ["action"],
        }

    def execute(
        self,
        action: str = "status",
        task_id: str | None = None,
        items: list[str] | None = None,
        item: str | None = None,
        error: str | None = None,
    ) -> ToolResult:
        """Execute loop action."""
        # Resolve task_id: explicit > cron context > error
        tid = task_id or self.cron_task_id
        if not tid:
            return ToolResult(
                "Error: task_id is required. "
                "Provide task_id parameter or use within a cron-triggered context.",
                error=True,
            )

        if action == "sync":
            return self._sync(tid, items or [])
        elif action == "next":
            return self._next(tid)
        elif action == "done":
            return self._done(tid, item)
        elif action == "fail":
            return self._fail(tid, item, error)
        elif action == "status":
            return self._status(tid)
        else:
            return ToolResult(f"Error: unknown action '{action}'", error=True)

    def _sync(self, task_id: str, items: list[str]) -> ToolResult:
        """Sync item list - idempotent, only adds new items as pending."""
        state = _load_state(task_id)

        added = 0
        for item in items:
            if item not in state:
                state[item] = "pending"
                added += 1

        _save_state(task_id, state)

        # Sync cron remaining counter if triggered by cron
        if self.cron_task_id:
            pending_count = sum(1 for v in state.values() if v == "pending")
            _sync_cron_remaining(self.cron_task_id, pending_count)

        # Calculate statistics
        total = len(state)
        done = sum(1 for v in state.values() if v == "done")
        pending = sum(1 for v in state.values() if v == "pending")
        failed = sum(1 for v in state.values() if v.startswith("failed:"))

        return ToolResult(json.dumps({
            "added": added,
            "total": total,
            "pending": pending,
            "done": done,
            "failed": failed,
        }, ensure_ascii=False))

    def _next(self, task_id: str) -> ToolResult:
        """Get next pending item."""
        state = _load_state(task_id)

        # Find first pending item
        for item, status in state.items():
            if status == "pending":
                return ToolResult(json.dumps({"item": item}, ensure_ascii=False))

        # No pending items
        return ToolResult(json.dumps({"item": None}, ensure_ascii=False))

    def _done(self, task_id: str, item: str | None) -> ToolResult:
        """Mark item as completed."""
        if not item:
            return ToolResult("Error: 'item' is required for done action", error=True)

        state = _load_state(task_id)

        if item not in state:
            return ToolResult(f"Error: item '{item}' not found in state", error=True)

        state[item] = "done"
        _save_state(task_id, state)

        # Calculate statistics
        done = sum(1 for v in state.values() if v == "done")
        pending = sum(1 for v in state.values() if v == "pending")
        failed = sum(1 for v in state.values() if v.startswith("failed:"))

        return ToolResult(json.dumps({
            "done": done,
            "pending": pending,
            "failed": failed,
        }, ensure_ascii=False))

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

        # Calculate statistics
        done = sum(1 for v in state.values() if v == "done")
        pending = sum(1 for v in state.values() if v == "pending")
        failed = sum(1 for v in state.values() if v.startswith("failed:"))

        return ToolResult(json.dumps({
            "done": done,
            "pending": pending,
            "failed": failed,
        }, ensure_ascii=False))

    def _status(self, task_id: str) -> ToolResult:
        """Get progress statistics."""
        state = _load_state(task_id)

        total = len(state)
        done = sum(1 for v in state.values() if v == "done")
        pending = sum(1 for v in state.values() if v == "pending")
        failed = sum(1 for v in state.values() if v.startswith("failed:"))

        return ToolResult(json.dumps({
            "total": total,
            "done": done,
            "pending": pending,
            "failed": failed,
        }, ensure_ascii=False))
