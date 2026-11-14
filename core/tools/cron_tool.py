"""Cron tool - scheduled task management (v2)."""

from __future__ import annotations

import logging

from core.config import get_all_workspace_uuids
from core.cron import service, store
from core.cron.flags import cron_run_active
from core.cron.model import CronValidationError
from core.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


class CronTool(Tool):
    name = "cron"
    description = (
        "**action is REQUIRED** — always include it.\n"
        "Manage scheduled tasks: create / list / update / delete / run / enable / disable / history.\n"
        "Tasks persist across restarts and run in their own '[Cron] <name>' session via the master agent.\n\n"
        "## Schedule\n"
        '- interval: {"type": "interval", "minutes": N} (optional initial_delay_minutes)\n'
        '- cron: {"type": "cron", "expr": "minute hour day month weekday", "timezone": "Asia/Shanghai"}\n'
        '- at: {"type": "at", "time": "2026-11-15T09:00:00+08:00"} (one-shot)\n'
        "timezone is optional; omit it to use the server local time.\n\n"
        "## Delivery\n"
        '- delivery: {"mode": "origin"} returns the result to the session that created the task; '
        '{"mode": "session", "workspace_uuid": "...", "session_id": "..."} targets another session; '
        '{"mode": "none"} keeps it only in the task\'s cron session.\n'
        "- QQ / WeChat cannot push proactively: a result written to those sessions becomes visible "
        "the next time the user interacts.\n\n"
        "## Rules\n"
        "- Default to one-time: unless the user explicitly wants recurring, create one_time=true tasks "
        "(deleted after the first run); use action=\"run\" for immediate execution.\n"
        "- Managed global tasks (managed=true) are read-only: only list/history are allowed.\n"
        "- update only touches the fields provided.\n"
        "- workspace_uuid=\"system\" runs the task in the System workspace."
    )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "create",
                        "list",
                        "update",
                        "delete",
                        "run",
                        "enable",
                        "disable",
                        "history",
                    ],
                    "description": "Action to perform. REQUIRED.",
                },
                "name": {
                    "type": "string",
                    "description": "Task id or display name (required for most actions).",
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable task description.",
                },
                "schedule": {
                    "type": "object",
                    "description": (
                        "When to run. Required for create. "
                        '{"type":"interval","minutes":N}, '
                        '{"type":"cron","expr":"0 9 * * *","timezone":"Asia/Shanghai"}, '
                        '{"type":"at","time":"..."}'
                    ),
                    "properties": {
                        "type": {"type": "string", "enum": ["at", "interval", "cron"]},
                        "minutes": {"type": "integer"},
                        "initial_delay_minutes": {"type": "integer"},
                        "expr": {"type": "string"},
                        "time": {"type": "string"},
                        "timezone": {"type": "string"},
                    },
                    "required": ["type"],
                },
                "misfire": {
                    "type": "object",
                    "description": 'Missed-run policy: {"policy":"once"|"skip"|"none","grace_minutes":120}',
                    "properties": {
                        "policy": {"type": "string", "enum": ["once", "skip", "none"]},
                        "grace_minutes": {"type": "integer"},
                    },
                },
                "task": {
                    "type": "string",
                    "description": "Task description for the master agent (required for create).",
                },
                "plan": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Execution steps (optional).",
                },
                "delivery": {
                    "type": "object",
                    "description": "Where the result should be delivered.",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["none", "origin", "session"],
                        },
                        "workspace_uuid": {"type": "string"},
                        "session_id": {"type": "string"},
                    },
                },
                "workspace_uuid": {
                    "type": "string",
                    "description": "Target workspace (defaults to current workspace; use 'system' for system tasks).",
                },
                "one_time": {
                    "type": "boolean",
                    "description": "Delete after first execution. Default: true.",
                    "default": True,
                },
                "max_executions": {
                    "type": "integer",
                    "description": "Maximum execution count (1-9999). Default: 9999.",
                    "minimum": 1,
                    "maximum": 9999,
                    "default": 9999,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Per-run timeout in seconds (0 = no limit). Default: 900.",
                    "minimum": 0,
                    "default": 900,
                },
                "limit": {
                    "type": "integer",
                    "description": "History entries to return (default 20, max 100).",
                    "minimum": 1,
                    "default": 20,
                },
            },
            "required": ["action"],
        }

    def set_interface_profile(self, profile) -> None:
        self._interface_name = getattr(profile, "name", "") or ""
        return None

    def execute(
        self,
        action: str | None = None,
        name: str | None = None,
        description: str | None = None,
        schedule: dict | None = None,
        misfire: dict | None = None,
        task: str | None = None,
        plan: list[str] | None = None,
        delivery: dict | None = None,
        workspace_uuid: str | None = None,
        one_time: bool | None = None,
        max_executions: int | None = None,
        timeout_seconds: int | None = None,
        limit: int = 20,
    ) -> ToolResult:
        if action is None:
            if schedule is not None or task is not None:
                action = "create"
                logger.warning("[cron_tool] action missing, inferred 'create'")
            else:
                return ToolResult(
                    "Error: 'action' is required. Available: create, list, update, delete, run, enable, disable, history",
                    error=True,
                )

        if action in ("create", "update", "delete", "enable", "disable") and cron_run_active.get():
            return ToolResult(
                "Error: cron tasks cannot be created or modified from inside a cron run",
                error=True,
            )

        from core.teen_mode import get_teen_mode

        tm = get_teen_mode()
        if action in ("create", "update", "delete", "enable", "disable") \
                and tm.is_enabled() and (tm.policy().get("cron") or "deny") == "deny" \
                and not tm.is_unlocked():
            tm.audit("deny", action="cron_modify", target=name or "", source="tool")
            tm.record_denied("cron_modify")
            return ToolResult("Error: 青少年模式已开启：定时任务的增删改需要家长授权。", error=True)

        try:
            if action == "create":
                return self._create(
                    name,
                    description,
                    schedule,
                    misfire,
                    task,
                    plan,
                    delivery,
                    workspace_uuid,
                    one_time if one_time is not None else True,
                    max_executions if max_executions is not None else 9999,
                    timeout_seconds if timeout_seconds is not None else 900,
                )
            if action == "list":
                return self._list()
            if action == "history":
                if not name:
                    return ToolResult("Error: 'name' is required for history", error=True)
                workspace, job = self._resolve(name, workspace_uuid)
                records = service.history(workspace, job.id, limit)
                return self._format_history(job, records)
            if action == "run":
                if not name:
                    return ToolResult("Error: 'name' is required for run", error=True)
                workspace, job = self._resolve(name, workspace_uuid)
                service.run_now(workspace, job.id)
                return ToolResult(f"Task '{job.name}' triggered (extra run).")
            if action == "delete":
                if not name:
                    return ToolResult("Error: 'name' is required for delete", error=True)
                workspace, job = self._resolve(name, workspace_uuid)
                service.delete_job(workspace, job.id)
                return ToolResult(f"Deleted scheduled task '{job.name}'.")
            if action in ("enable", "disable"):
                if not name:
                    return ToolResult(f"Error: 'name' is required for {action}", error=True)
                workspace, job = self._resolve(name, workspace_uuid)
                service.set_enabled(workspace, job.id, action == "enable")
                return ToolResult(f"Task '{job.name}' {action}d.")
            if action == "update":
                if not name:
                    return ToolResult("Error: 'name' is required for update", error=True)
                workspace, job = self._resolve(name, workspace_uuid)
                updates = self._build_updates(
                    job, description, schedule, misfire, task, plan,
                    delivery, one_time, max_executions, timeout_seconds,
                )
                updated = service.update_job(workspace, job.id, updates)
                return ToolResult(
                    f"Updated task '{updated.name}': {', '.join(sorted(updates))}"
                )
            return ToolResult(f"Error: unknown action '{action}'", error=True)
        except (CronValidationError, service.CronError) as exc:
            return ToolResult(f"Error: {exc}", error=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[cron_tool] action %s failed", action)
            return ToolResult(f"Error: {exc}", error=True)

    # ─── helpers ────────────────────────────────────────────────

    def _origin(self) -> dict | None:
        session_id = str(getattr(self.session, "session_id", "") or "")
        if not session_id:
            return None
        return {
            "workspace_uuid": self.workspace_uuid,
            "session_id": session_id,
            "interface": getattr(self, "_interface_name", "") or None,
        }

    def _resolve(self, name: str, workspace_uuid: str | None = None):
        workspaces = [workspace_uuid] if workspace_uuid else get_all_workspace_uuids()
        for workspace in workspaces:
            job = store.load_job(workspace, name)
            if job is not None:
                return workspace, job
        for workspace in workspaces:
            for job in store.list_jobs(workspace):
                if job.name == name:
                    return workspace, job
        raise service.CronError(f"task '{name}' not found")

    def _create(
        self,
        name: str | None,
        description: str | None,
        schedule: dict | None,
        misfire: dict | None,
        task: str | None,
        plan: list[str] | None,
        delivery: dict | None,
        workspace_uuid: str | None,
        one_time: bool,
        max_executions: int,
        timeout_seconds: int,
    ) -> ToolResult:
        if not name:
            return ToolResult("Error: 'name' is required for create", error=True)
        if not task:
            return ToolResult("Error: 'task' is required for create", error=True)
        if not schedule:
            return ToolResult("Error: 'schedule' is required for create", error=True)

        data = {
            "name": name,
            "description": description or "",
            "schedule": schedule,
            "content": {"task": task, "plan": plan or []},
            "execution": {
                "timeout_seconds": timeout_seconds,
                "max_executions": max_executions,
            },
            "one_time": one_time,
            "workspace_uuid": workspace_uuid or self.workspace_uuid,
            "delivery": delivery,
        }
        if misfire:
            data["misfire"] = misfire
        if not delivery:
            data.pop("delivery")
        job = service.create_job(data, origin=self._origin())
        return ToolResult(
            f"Created {'one-time' if one_time else 'recurring'} task '{job.name}' "
            f"(id={job.id}) in workspace '{job.workspace_uuid or 'default'}'. "
            f"Schedule: {job.schedule}"
        )

    def _build_updates(
        self,
        job,
        description: str | None,
        schedule: dict | None,
        misfire: dict | None,
        task: str | None,
        plan: list[str] | None,
        delivery: dict | None,
        one_time: bool | None,
        max_executions: int | None,
        timeout_seconds: int | None,
    ) -> dict:
        updates: dict = {}
        if description is not None:
            updates["description"] = description
        if schedule is not None:
            updates["schedule"] = schedule
        if misfire is not None:
            updates["misfire"] = misfire
        if task is not None or plan is not None:
            content = dict(job.content) if isinstance(job.content, dict) else {}
            if task is not None:
                content["task"] = task
            if plan is not None:
                content["plan"] = plan
            updates["content"] = content
        if delivery is not None:
            updates["delivery"] = delivery
        if one_time is not None:
            updates["one_time"] = one_time
        execution: dict = {}
        if max_executions is not None:
            execution["max_executions"] = max_executions
        if timeout_seconds is not None:
            execution["timeout_seconds"] = timeout_seconds
        if execution:
            updates["execution"] = execution
        if not updates:
            raise service.CronError("no fields to update")
        return updates

    def _list(self) -> ToolResult:
        items = service.list_jobs()
        if not items:
            return ToolResult("No scheduled tasks found.")
        lines = [f"Found {len(items)} scheduled task(s):\n"]
        for item in items:
            state = item.get("state") or {}
            flags = []
            flags.append("enabled" if item.get("enabled") else "disabled")
            flags.append("managed-readonly" if item.get("managed") else "user")
            flags.append("one-time" if item.get("one_time") else "recurring")
            lines.append(
                f"• {item.get('name')} [{', '.join(flags)}] "
                f"(workspace: {item.get('workspace_uuid') or 'default'})"
            )
            if item.get("description"):
                lines.append(f"  {item['description']}")
            lines.append(f"  Schedule: {item.get('schedule_summary')}")
            lines.append(
                f"  Next: {state.get('next_run') or '-'} | "
                f"Last: {state.get('last_status') or '-'} "
                f"({state.get('last_run') or '-'}) | "
                f"runs={state.get('run_count', 0)} remaining={state.get('remaining')}"
            )
            delivery_mode = (item.get("delivery") or {}).get("mode", "none")
            lines.append(f"  Delivery: {delivery_mode}")
            if state.get("last_error"):
                lines.append(f"  Last error: {str(state['last_error'])[:200]}")
            content = item.get("content")
            task_text = content.get("task") if isinstance(content, dict) else ""
            if task_text:
                lines.append(f"  Task: {str(task_text)[:120]}")
            lines.append("")
        return ToolResult("\n".join(lines))

    def _format_history(self, job, records: list[dict]) -> ToolResult:
        if not records:
            return ToolResult(f"No run history for task '{job.name}'.")
        lines = [f"Run history for '{job.name}' ({len(records)}):"]
        for record in records:
            lines.append(
                f"• {record.get('slot')} | {record.get('status')} | "
                f"{record.get('duration_ms')}ms | delivery={record.get('delivery')}"
                + (f" | {record.get('error')}" if record.get("error") else "")
            )
        return ToolResult("\n".join(lines))
