"""Cron tool - user-level scheduled task management.

Allows users to create, list, delete, and manage scheduled tasks through conversation.
Tasks are persisted in data/cili/cron.d/user_tasks.json and executed by the CronScheduler.

Usage:
    cron(action="create", name="daily-report", schedule={"type": "interval", "minutes": 1440}, task="Generate daily report")
    cron(action="list")
    cron(action="delete", name="daily-report")
    cron(action="run", name="daily-report")
    cron(action="enable", name="daily-report")
    cron(action="disable", name="daily-report")
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.tools.shared.base import Tool, ToolResult
from core.cron import USER_TASKS_FILE

logger = logging.getLogger(__name__)


def _load_user_tasks() -> list[dict]:
    """Load user task configs from JSON file."""
    if not USER_TASKS_FILE.exists():
        return []
    try:
        with open(USER_TASKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[cron_tool] Failed to load user tasks: {e}")
        return []


def _save_user_tasks(tasks: list[dict]) -> None:
    """Save user task configs to JSON file."""
    try:
        USER_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(USER_TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[cron_tool] Failed to save user tasks: {e}")


class CronTool(Tool):
    name = "cron"
    description = (
        "**User-level scheduled task management.**\n"
        "Create, list, update, delete, and manage scheduled tasks through conversation.\n\n"
        "## Actions:\n"
        "- **create**: Create a new scheduled task\n"
        "- **list**: List all scheduled tasks\n"
        "- **update**: Update an existing task's configuration\n"
        "- **delete**: Delete a scheduled task\n"
        "- **run**: Immediately execute a task\n"
        "- **enable**: Enable a disabled task\n"
        "- **disable**: Disable a task (without deleting)\n\n"
        "## Schedule Types:\n"
        "- **interval**: Run every N minutes (e.g., {\"type\": \"interval\", \"minutes\": 60})\n"
        "- **cron**: Standard cron expression (e.g., {\"type\": \"cron\", \"expr\": \"0 9 * * *\"} = daily at 9am)\n\n"
        "## Task Configuration:\n"
        "- **name**: Unique task identifier (required)\n"
        "- **description**: Human-readable description\n"
        "- **schedule**: When to run (interval or cron)\n"
        "- **task**: What to do (task description)\n"
        "- **plan**: Execution steps (optional list)\n"
        "- **workspace_uuid**: Target workspace (optional, defaults to current workspace)\n"
        "- **enabled**: Whether task is active (default: true)\n\n"
        "## Workspace:\n"
        "Tasks execute in a dedicated 'Cron Tasks' session within the target workspace.\n"
        "The RootAgent decides whether to handle the task directly or delegate to SubAgent.\n"
        "Use workspace_uuid=\"system\" for system maintenance tasks (cwd: data/).\n\n"
        "## Examples:\n"
        "```python\n"
        "# Create a daily report task (runs in current workspace)\n"
        "cron(action=\"create\", name=\"daily-report\", \n"
        "     schedule={\"type\": \"cron\", \"expr\": \"0 9 * * *\"},\n"
        "     task=\"Generate daily status report and save to reports/\")\n"
        "\n"
        "# Update a task's schedule and task content\n"
        "cron(action=\"update\", name=\"daily-report\",\n"
        "     schedule={\"type\": \"cron\", \"expr\": \"0 10 * * *\"},\n"
        "     task=\"Generate daily report at 10am\")\n"
        "\n"
        "# Update only the task description\n"
        "cron(action=\"update\", name=\"daily-report\", task=\"New task content\")\n"
        "\n"
        "# Create a system maintenance task\n"
        "cron(action=\"create\", name=\"cleanup\",\n"
        "     schedule={\"type\": \"cron\", \"expr\": \"0 3 * * *\"},\n"
        "     task=\"Clean up old temporary files\",\n"
        "     workspace_uuid=\"system\")\n"
        "\n"
        "# List all tasks\n"
        "cron(action=\"list\")\n"
        "\n"
        "# Run a task immediately\n"
        "cron(action=\"run\", name=\"daily-report\")\n"
        "\n"
        "# Disable a task\n"
        "cron(action=\"disable\", name=\"daily-report\")\n"
        "\n"
        "# Delete a task\n"
        "cron(action=\"delete\", name=\"daily-report\")\n"
        "```\n\n"
        "## Notes:\n"
        "- **Default to one-time execution**: Unless the user explicitly mentions recurring schedules (e.g., \"every day\", \"every hour\", \"每X分钟/小时/天\"), create tasks as one-time execution. For one-time tasks, set a schedule with large interval or use action=\"run\" to execute immediately, then delete the task after completion.\n"
        "- **Update**: Only specified fields are updated, others remain unchanged\n"
        "- Tasks are persisted and survive server restarts\n"
        "- Tasks run through RootAgent in a 'Cron Tasks' session\n"
        "- Results are visible in the workspace UI\n"
        "- Use standard 5-field cron expressions (minute hour day month weekday)"
    )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "update", "delete", "run", "enable", "disable"],
                    "description": "Action to perform.",
                },
                "name": {
                    "type": "string",
                    "description": "Task name (required for create/update/delete/run/enable/disable).",
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable task description (for create/update).",
                },
                "schedule": {
                    "type": "object",
                    "description": (
                        "When to run the task. Required for create. Optional for update. "
                        "Either {\"type\": \"interval\", \"minutes\": N} or "
                        "{\"type\": \"cron\", \"expr\": \"minute hour day month weekday\"}."
                    ),
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["interval", "cron"],
                            "description": "Schedule type.",
                        },
                        "minutes": {
                            "type": "integer",
                            "description": "Interval in minutes (for type='interval').",
                        },
                        "expr": {
                            "type": "string",
                            "description": "Cron expression (for type='cron'). Example: '0 9 * * *' = daily at 9am.",
                        },
                    },
                    "required": ["type"],
                },
                "task": {
                    "type": "string",
                    "description": "Task description for SubAgent (required for create, optional for update).",
                },
                "plan": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Execution steps (optional, for create/update).",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Maximum iterations (default: 30, for create/update).",
                    "default": 30,
                },
                "workspace_uuid": {
                    "type": "string",
                    "description": (
                        "Target workspace for task execution (for create/update). "
                        "Defaults to current workspace. "
                        "Use \"system\" for system maintenance tasks (cwd: data/)."
                    ),
                },
                "one_time": {
                    "type": "boolean",
                    "description": (
                        "If true, task is automatically deleted after first execution. "
                        "Default: true. Set to false only for recurring tasks "
                        "(e.g., 'every day', 'every hour')."
                    ),
                    "default": True,
                },
                "max_executions": {
                    "type": "integer",
                    "description": (
                        "Maximum execution count (1-9999). Default: 9999. "
                        "Used with remaining counter for auto-disable. "
                        "Ignored if one_time=true."
                    ),
                    "minimum": 1,
                    "maximum": 9999,
                    "default": 9999,
                },
            },
            "required": ["action"],
        }

    def execute(
        self,
        action: str = "list",
        name: str | None = None,
        description: str | None = None,
        schedule: dict | None = None,
        task: str | None = None,
        plan: list[str] | None = None,
        max_iterations: int | None = None,
        workspace_uuid: str | None = None,
        one_time: bool | None = None,
        max_executions: int | None = None,
    ) -> ToolResult:
        """Execute cron action."""
        if action == "create":
            # create 使用默认值
            return self._create(
                name, description, schedule, task, plan,
                max_iterations if max_iterations is not None else 30,
                workspace_uuid,
                one_time if one_time is not None else True,
                max_executions if max_executions is not None else 9999,
            )
        elif action == "list":
            return self._list()
        elif action == "update":
            return self._update(name, description, schedule, task, plan, max_iterations, workspace_uuid, one_time, max_executions)
        elif action == "delete":
            return self._delete(name)
        elif action == "run":
            return self._run(name)
        elif action == "enable":
            return self._set_enabled(name, True)
        elif action == "disable":
            return self._set_enabled(name, False)
        else:
            return ToolResult(f"Error: unknown action '{action}'", error=True)

    def _create(
        self,
        name: str | None,
        description: str | None,
        schedule: dict | None,
        task: str | None,
        plan: list[str] | None,
        max_iterations: int,
        workspace_uuid: str | None = None,
        one_time: bool = True,
        max_executions: int = 9999,
    ) -> ToolResult:
        """Create a new scheduled task."""
        if not name:
            return ToolResult("Error: 'name' is required for create action", error=True)
        if not task:
            return ToolResult("Error: 'task' is required for create action", error=True)
        if not schedule:
            return ToolResult("Error: 'schedule' is required for create action", error=True)

        # Validate schedule
        schedule_type = schedule.get("type")
        if schedule_type == "interval":
            minutes = schedule.get("minutes")
            if not minutes or minutes <= 0:
                return ToolResult("Error: 'minutes' must be positive for interval schedule", error=True)
        elif schedule_type == "cron":
            expr = schedule.get("expr")
            if not expr:
                return ToolResult("Error: 'expr' is required for cron schedule", error=True)
            # Basic cron expression validation (5 fields)
            parts = expr.split()
            if len(parts) != 5:
                return ToolResult("Error: cron expression must have 5 fields (minute hour day month weekday)", error=True)
        else:
            return ToolResult("Error: schedule type must be 'interval' or 'cron'", error=True)

        # Validate max_executions
        if max_executions < 1 or max_executions > 9999:
            return ToolResult("Error: 'max_executions' must be between 1 and 9999", error=True)

        # Load existing tasks
        tasks = _load_user_tasks()

        # Check for duplicate name
        for t in tasks:
            if t["name"] == name:
                return ToolResult(f"Error: task '{name}' already exists. Delete it first or use a different name.", error=True)

        # Create new task config
        new_task = {
            "name": name,
            "workspace_uuid": workspace_uuid or self.workspace_uuid,
            "description": description or "",
            "enabled": True,
            "schedule": schedule,
            "one_time": one_time,
            "config": {
                "max_iterations": max_iterations,
                "max_executions": max_executions,
            },
            "content": {
                "task": task,
                "plan": plan or [],
            },
        }

        tasks.append(new_task)
        _save_user_tasks(tasks)

        # Reload scheduler to pick up new task
        self._reload_scheduler()

        ws_label = workspace_uuid or self.workspace_uuid or "system"
        task_type = "one-time" if one_time else "recurring"
        return ToolResult(
            f"Created {task_type} scheduled task '{name}' in workspace '{ws_label}'. "
            f"It will run according to the schedule: {schedule}"
        )

    def _list(self) -> ToolResult:
        """List all scheduled tasks."""
        tasks = _load_user_tasks()
        if not tasks:
            return ToolResult("No scheduled tasks found.")

        lines = [f"Found {len(tasks)} scheduled task(s):\n"]
        for t in tasks:
            name = t["name"]
            desc = t.get("description", "")
            enabled = t.get("enabled", True)
            schedule = t.get("schedule", {})
            schedule_type = schedule.get("type", "unknown")
            ws_uuid = t.get("workspace_uuid", "")
            ws_label = ws_uuid if ws_uuid else "system"

            status = "enabled" if enabled else "disabled"
            one_time = t.get("one_time", False)
            task_type = "one-time" if one_time else "recurring"
            if schedule_type == "interval":
                schedule_str = f"every {schedule.get('minutes', '?')} minutes"
            elif schedule_type == "cron":
                schedule_str = f"cron: {schedule.get('expr', '?')}"
            else:
                schedule_str = "unknown schedule"

            lines.append(f"• {name} [{status}] [{task_type}] (workspace: {ws_label})")
            if desc:
                lines.append(f"  {desc}")
            lines.append(f"  Schedule: {schedule_str}")
            task_desc = t.get("content", {}).get("task", "")
            if task_desc:
                lines.append(f"  Task: {task_desc[:80]}{'...' if len(task_desc) > 80 else ''}")
            lines.append("")

        return ToolResult("\n".join(lines))

    def _update(
        self,
        name: str | None,
        description: str | None,
        schedule: dict | None,
        task: str | None,
        plan: list[str] | None,
        max_iterations: int | None,
        workspace_uuid: str | None,
        one_time: bool | None,
        max_executions: int | None,
    ) -> ToolResult:
        """Update an existing scheduled task. Only updates provided fields."""
        if not name:
            return ToolResult("Error: 'name' is required for update action", error=True)

        # Load existing tasks
        tasks = _load_user_tasks()
        task_config = None
        for t in tasks:
            if t["name"] == name:
                task_config = t
                break

        if not task_config:
            return ToolResult(f"Error: task '{name}' not found", error=True)

        # Validate schedule if provided
        if schedule:
            schedule_type = schedule.get("type")
            if schedule_type == "interval":
                minutes = schedule.get("minutes")
                if not minutes or minutes <= 0:
                    return ToolResult("Error: 'minutes' must be positive for interval schedule", error=True)
            elif schedule_type == "cron":
                expr = schedule.get("expr")
                if not expr:
                    return ToolResult("Error: 'expr' is required for cron schedule", error=True)
                parts = expr.split()
                if len(parts) != 5:
                    return ToolResult("Error: cron expression must have 5 fields (minute hour day month weekday)", error=True)
            else:
                return ToolResult("Error: schedule type must be 'interval' or 'cron'", error=True)

        # Validate max_executions if provided
        if max_executions is not None and (max_executions < 1 or max_executions > 9999):
            return ToolResult("Error: 'max_executions' must be between 1 and 9999", error=True)

        # Update fields that are provided
        updated_fields = []

        if description is not None:
            task_config["description"] = description
            updated_fields.append("description")

        if schedule is not None:
            task_config["schedule"] = schedule
            updated_fields.append("schedule")

        if task is not None:
            if "content" not in task_config:
                task_config["content"] = {}
            task_config["content"]["task"] = task
            updated_fields.append("task")

        if plan is not None:
            if "content" not in task_config:
                task_config["content"] = {}
            task_config["content"]["plan"] = plan
            updated_fields.append("plan")

        if max_iterations is not None:
            if "config" not in task_config:
                task_config["config"] = {}
            task_config["config"]["max_iterations"] = max_iterations
            updated_fields.append("max_iterations")

        if workspace_uuid is not None:
            task_config["workspace_uuid"] = workspace_uuid
            updated_fields.append("workspace_uuid")

        if one_time is not None:
            task_config["one_time"] = one_time
            updated_fields.append("one_time")

        if max_executions is not None:
            if "config" not in task_config:
                task_config["config"] = {}
            task_config["config"]["max_executions"] = max_executions
            # Also reset remaining counter in state
            from core.cron import CRON_STATE_DIR
            import json
            state_path = CRON_STATE_DIR / f"{name}.json"
            try:
                if state_path.exists():
                    with open(state_path, "r", encoding="utf-8") as f:
                        state = json.load(f)
                else:
                    state = {}
                state["remaining"] = max_executions
                state_path.parent.mkdir(parents=True, exist_ok=True)
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"[cron_tool] Failed to reset remaining for task '{name}': {e}")
            updated_fields.append("max_executions")

        if not updated_fields:
            return ToolResult("Error: No fields to update. Specify at least one field to update.", error=True)

        _save_user_tasks(tasks)
        self._reload_scheduler()

        return ToolResult(f"Updated task '{name}': {', '.join(updated_fields)}")

    def _delete(self, name: str | None) -> ToolResult:
        """Delete a scheduled task."""
        if not name:
            return ToolResult("Error: 'name' is required for delete action", error=True)

        tasks = _load_user_tasks()
        original_count = len(tasks)
        tasks = [t for t in tasks if t["name"] != name]

        if len(tasks) == original_count:
            return ToolResult(f"Error: task '{name}' not found", error=True)

        _save_user_tasks(tasks)
        self._reload_scheduler()

        return ToolResult(f"Deleted scheduled task '{name}'.")

    def _run(self, name: str | None) -> ToolResult:
        """Immediately execute a task."""
        if not name:
            return ToolResult("Error: 'name' is required for run action", error=True)

        # Trigger task via scheduler
        from core.cron import get_scheduler
        scheduler = get_scheduler()
        result = scheduler.run_task_now(name)

        if result is None:
            return ToolResult(f"Error: task '{name}' not found or not loaded", error=True)

        return ToolResult(f"Task '{name}' triggered. Check logs for execution status.")

    def _set_enabled(self, name: str | None, enabled: bool) -> ToolResult:
        """Enable or disable a task."""
        if not name:
            action = "enable" if enabled else "disable"
            return ToolResult(f"Error: 'name' is required for {action} action", error=True)

        tasks = _load_user_tasks()
        found = False
        task_config = None
        for t in tasks:
            if t["name"] == name:
                t["enabled"] = enabled
                task_config = t
                found = True
                break

        if not found:
            return ToolResult(f"Error: task '{name}' not found", error=True)

        _save_user_tasks(tasks)
        self._reload_scheduler()

        # If enabling, reset remaining counter
        if enabled and task_config:
            from core.cron import CRON_STATE_DIR
            import json

            max_executions = task_config.get("config", {}).get("max_executions", 9999)
            state_path = CRON_STATE_DIR / f"{name}.json"

            try:
                # Load existing state or create new
                if state_path.exists():
                    with open(state_path, "r", encoding="utf-8") as f:
                        state = json.load(f)
                else:
                    state = {}

                # Reset remaining counter
                state["remaining"] = max_executions

                state_path.parent.mkdir(parents=True, exist_ok=True)
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2, ensure_ascii=False)

                logger.info(f"[cron_tool] Reset remaining={max_executions} for task '{name}'")
            except Exception as e:
                logger.warning(f"[cron_tool] Failed to reset remaining for task '{name}': {e}")

        status = "enabled" if enabled else "disabled"
        return ToolResult(f"Task '{name}' {status}.")

    def _reload_scheduler(self) -> None:
        """Reload the cron scheduler to pick up changes."""
        try:
            from core.cron import get_scheduler
            scheduler = get_scheduler()
            scheduler.load_tasks()
            logger.info("[cron_tool] Scheduler reloaded")
        except Exception as e:
            logger.warning(f"[cron_tool] Failed to reload scheduler: {e}")
