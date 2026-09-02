"""TodoWrite tool - structured task list for planning and progress tracking.

Key design decisions:
1. Whole-list replacement: each call sends the complete list (no partial updates)
2. Three-state status: pending / in_progress / completed
3. Single owner: belongs to the agent session that created it
4. Parallel control: configurable allow_parallel_in_progress
5. Storage: stored in session metadata, persisted to index.json
"""

from __future__ import annotations

from typing import Any

from core.tools.shared.base import Tool, ToolResult


# Valid status values
VALID_STATUSES = {"pending", "in_progress", "completed"}


class TodoWriteTool(Tool):
    """Structured task list for planning and progress tracking.

    The agent sends the ENTIRE list on every update; the new list replaces
    the previous one (no partial updates, no per-item edits). Each item has:
    - content: imperative form ("Fix auth bug")
    - activeForm: present continuous form ("Fixing auth bug") - shown in UI
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
        "- in_progress: Currently working on (limit to ONE at a time)\n"
        "- completed: Finished successfully\n\n"
        "## Task Format\n"
        "Each task must have TWO forms:\n"
        "- content: Imperative form (e.g., 'Run tests')\n"
        "- activeForm: Present continuous form (e.g., 'Running tests')\n\n"
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
                        "activeForm": {
                            "type": "string",
                            "description": "Present continuous form shown in UI during execution (e.g., 'Fixing authentication bug')"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "Task state: pending (not started) | in_progress (working now) | completed (done)"
                        }
                    },
                    "required": ["content", "activeForm", "status"]
                }
            }
        },
        "required": ["todos"]
    }

    # Configuration: whether to allow multiple in_progress tasks
    # Can be overridden per-instance for parallel-capable agents
    allow_parallel_in_progress: bool = False

    def __init__(
        self,
        cwd: str = ".",
        workspace_uuid: str = "",
        session_manager=None,
        allow_parallel_in_progress: bool = False,
    ):
        super().__init__(cwd, workspace_uuid, session_manager)
        self.allow_parallel_in_progress = allow_parallel_in_progress

    def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the todo_write tool.

        Validates the todo list, stores it in session metadata, and returns
        the new counts. The list replaces any previous list entirely.
        """
        todos = kwargs.get("todos", [])

        # Validate input
        if not isinstance(todos, list):
            return ToolResult("Error: todos must be an array", error=True)

        # Validate and normalize each item
        validated_todos = []
        seen_contents = set()
        in_progress_count = 0

        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                return ToolResult(f"Error: todo item {i} must be an object", error=True)

            content = item.get("content", "").strip()
            active_form = item.get("activeForm", "").strip()
            status = item.get("status", "")

            # Validate content
            if not content:
                return ToolResult(
                    f"Error: todo item {i} 'content' must be a non-empty string",
                    error=True
                )

            # Validate activeForm
            if not active_form:
                return ToolResult(
                    f"Error: todo item {i} 'activeForm' must be a non-empty string",
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

            # Count in_progress items
            if status == "in_progress":
                in_progress_count += 1

            validated_todos.append({
                "content": content,
                "activeForm": active_form,
                "status": status,
            })

        # Check parallel constraint
        if not self.allow_parallel_in_progress and in_progress_count > 1:
            return ToolResult(
                f"Error: at most one task may be in_progress at a time (got {in_progress_count}). "
                "Complete the current task before starting another, or mark only one as in_progress.",
                error=True
            )

        # Store in session metadata
        if self.session_manager and hasattr(self.session_manager, 'metadata'):
            old_todos = self.session_manager.metadata.get("todos", [])
            self.session_manager.metadata["todos"] = validated_todos

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
        else:
            # No session manager - just return the counts
            counts = self._calculate_counts(validated_todos)
            return ToolResult(
                output=f"Todo list updated: {counts['completed']}/{counts['total']} completed",
                meta={"todos": validated_todos, "counts": counts}
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
    """Helper to get current todos from session metadata."""
    if session_manager is None or not hasattr(session_manager, 'metadata'):
        return None
    return session_manager.metadata.get("todos")


# Type hint for session manager (avoid circular import)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.session import SessionManager
