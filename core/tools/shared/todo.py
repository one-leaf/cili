"""TodoWrite tool - structured task list for planning and progress tracking.

Key design decisions:
1. Whole-list replacement: each call sends the complete list (no partial updates)
2. Three-state status: pending / in_progress / completed
3. Single owner: belongs to the agent session that created it
4. Storage: stored in data/cili/tools/todo/{session_id}.json (per-session isolation)
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from core.tools.shared.base import Tool, ToolResult


# Valid status values
VALID_STATUSES = {"pending", "in_progress", "completed"}

# Todo storage directory
TODO_DIR = Path("data/cili/tools/todo")


def get_todo_file_path(session_id: str) -> Path:
    """Get the path to the todo file for a given session."""
    return TODO_DIR / f"{session_id}.json"


def read_todos(session_id: str) -> list[dict]:
    """Read todos from the session's todo file."""
    todo_file = get_todo_file_path(session_id)
    if not todo_file.exists():
        return []
    try:
        data = json.loads(todo_file.read_text(encoding="utf-8"))
        return data.get("todos", [])
    except Exception:
        return []


def write_todos(session_id: str, todos: list[dict]) -> None:
    """Write todos to the session's todo file."""
    TODO_DIR.mkdir(parents=True, exist_ok=True)
    todo_file = get_todo_file_path(session_id)
    data = {
        "session_id": session_id,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "todos": todos,
    }
    todo_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class TodoWriteTool(Tool):
    """Structured task list for planning and progress tracking.

    The agent sends the ENTIRE list on every update; the new list replaces
    the previous one (no partial updates, no per-item edits). Each item has:
    - content: imperative form ("Fix auth bug")
    - status: pending | in_progress | completed

    When to use:
    - Complex multi-step tasks (3+ distinct steps)
    - User provides multiple tasks
    - Non-trivial work requiring planning

    When NOT to use:
    - Single straightforward task
    - Trivial tasks (< 3 steps)
    - Purely conversational/informational requests
    """

    name = "todo_write"
    description = (
        "Use this tool to create and manage a structured task list for your current "
        "coding session. This helps track progress, organize complex tasks, and "
        "demonstrate thoroughness.\n\n"
        "## When to Use\n"
        "1. Complex multi-step tasks (3+ distinct steps)\n"
        "2. User provides multiple tasks (numbered or comma-separated)\n"
        "3. After receiving new instructions - capture requirements as todos\n"
        "4. Mark task as in_progress BEFORE starting work\n"
        "5. Mark task completed IMMEDIATELY after finishing\n\n"
        "## When NOT to Use\n"
        "- Single straightforward task\n"
        "- Trivial tasks (< 3 steps)\n"
        "- Purely conversational requests\n\n"
        "## Task States\n"
        "- pending: Not yet started\n"
        "- in_progress: Currently working on (multiple allowed)\n"
        "- completed: Finished successfully\n\n"
        "## Task Format\n"
        "Each task must have a 'content' field with imperative form (e.g., 'Run tests').\n\n"
        "IMPORTANT: Send the ENTIRE list on every call - it REPLACES the previous list."
    )
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "The COMPLETE task list, replacing any previous list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "What the task is - imperative form (e.g., 'Fix authentication bug')"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "Task state: pending (not started) | in_progress (working now) | completed (done)"
                        }
                    },
                    "required": ["content", "status"]
                }
            }
        },
        "required": ["todos"]
    }

    def __init__(
        self,
        cwd: str = ".",
        workspace_uuid: str = "",
        session_manager=None,
    ):
        super().__init__(cwd, workspace_uuid, session_manager)

    def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the todo_write tool.

        Validates the todo list, stores it in the session's todo file,
        and returns the new counts. The list replaces any previous list entirely.
        """
        todos = kwargs.get("todos", [])

        # Get session_id for storage
        session_id = ""
        if self.session_manager and hasattr(self.session_manager, 'session_id'):
            session_id = self.session_manager.session_id

        # Validate input
        if not isinstance(todos, list):
            return ToolResult("Error: todos must be an array", error=True)

        # Validate and normalize each item
        validated_todos = []
        seen_contents = set()

        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                return ToolResult(f"Error: todo item {i} must be an object", error=True)

            content = item.get("content", "").strip()
            status = item.get("status", "")

            # Validate content
            if not content:
                return ToolResult(
                    f"Error: todo item {i} 'content' must be a non-empty string",
                    error=True
                )

            # Validate status
            if status not in VALID_STATUSES:
                return ToolResult(
                    f"Error: todo item {i} 'status' must be one of: {', '.join(VALID_STATUSES)}",
                    error=True
                )

            # Check for duplicate content
            if content in seen_contents:
                return ToolResult(
                    f'Error: duplicate todo content "{content}"',
                    error=True
                )
            seen_contents.add(content)

            validated_todos.append({
                "content": content,
                "status": status,
            })

        # Read old todos for verification nudge check
        old_todos = read_todos(session_id) if session_id else []

        # Store in independent file (per-session isolation)
        if session_id:
            write_todos(session_id, validated_todos)

            # Migrate from session metadata if it exists (backward compatibility)
            if self.session_manager and hasattr(self.session_manager, 'metadata'):
                if "todos" in self.session_manager.metadata:
                    # Remove old todos from session metadata
                    del self.session_manager.metadata["todos"]

        # Calculate counts for response
        counts = self._calculate_counts(validated_todos)

        # Check for verification nudge
        verification_nudge = self._check_verification_nudge(old_todos, validated_todos)

        # Build response message
        response = (
            f"Todos have been updated successfully.\n"
            f"Progress: {counts['completed']}/{counts['total']} completed"
        )
        if verification_nudge:
            response += (
                "\n\nNOTE: You just completed 3+ tasks without any verification step. "
                "Consider running tests or verifying your changes before reporting completion."
            )

        return ToolResult(
            output=response,
            meta={
                "todos": validated_todos,
                "counts": counts,
                "verification_nudge": verification_nudge,
            }
        )

    def _calculate_counts(self, todos: list[dict]) -> dict:
        """Calculate todo counts for the response."""
        total = len(todos)
        pending = sum(1 for t in todos if t["status"] == "pending")
        in_progress = sum(1 for t in todos if t["status"] == "in_progress")
        completed = sum(1 for t in todos if t["status"] == "completed")
        return {
            "total": total,
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
        }

    def _check_verification_nudge(self, old_todos: list[dict], new_todos: list[dict]) -> bool:
        """Check if we should nudge the agent to verify its work.

        Triggered when:
        - All tasks are now completed
        - There are 3+ tasks
        - None of the tasks mention "verify" or "test"
        - The previous state had incomplete tasks
        """
        # Check if all tasks are now completed
        if not new_todos or not all(t["status"] == "completed" for t in new_todos):
            return False

        # Check if there are 3+ tasks
        if len(new_todos) < 3:
            return False

        # Check if any task mentions verification
        verification_keywords = ["verify", "test", "check", "validate", "confirm"]
        has_verification = any(
            any(kw in t["content"].lower() for kw in verification_keywords)
            for t in new_todos
        )
        if has_verification:
            return False

        # Check if the previous state had incomplete tasks
        if old_todos and all(t["status"] == "completed" for t in old_todos):
            return False  # Already all done

        return True


def get_todos_from_session(session_manager: SessionManager | None) -> list[dict] | None:
    """Helper to get current todos from session's todo file.

    Supports new format (independent file) and old format (session metadata).
    """
    if session_manager is None:
        return None

    # Try new format: independent todo file
    session_id = getattr(session_manager, 'session_id', None)
    if session_id:
        todos = read_todos(session_id)
        if todos:
            return todos

    # Fall back to old format: session metadata (for backward compatibility)
    if hasattr(session_manager, 'metadata'):
        old_todos = session_manager.metadata.get("todos")
        if old_todos:
            # Migrate to new format
            write_todos(session_id, old_todos)
            # Remove from session metadata
            del session_manager.metadata["todos"]
            return old_todos

    return None


# Type hint for session manager (avoid circular import)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.session import SessionManager
