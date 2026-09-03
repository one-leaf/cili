"""SubAgent tool - delegate complex tasks to a sub-agent."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from core.tools.shared.base import Tool, ToolResult

logger = logging.getLogger(__name__)


class SubAgentTool(Tool):
    name = "subagent"
    description = (
        "**Delegate complex, multi-step tasks to an autonomous SubAgent.**\n"
        "The SubAgent runs a complete agent loop with full tool access "
        "(read/write/edit/bash/python/grep/find/browser/web_search/memory/llm).\n\n"
        "## When to use:\n"
        "- File processing that requires multiple read/write operations\n"
        "- Complex workflows needing tool interaction (e.g., translate file, analyze code)\n"
        "- Tasks too complex for a single tool call\n"
        "- Long-running tasks that should not block the main conversation\n\n"
        "## When NOT to use:\n"
        "- Simple file reads (use `read`)\n"
        "- Single command execution (use `bash`)\n"
        "- Python code execution (use `python`)\n"
        "- Web searches (use `web_search`)\n"
        "- Simple LLM calls like translation/summarization (use `llm`)\n\n"
        "## Task Description Guidelines:\n"
        "SubAgent runs in isolation — it does NOT see your conversation history, "
        "project rules (CLAUDE.md), or any implicit context. You must provide a "
        "complete, self-contained task description.\n"
        "- **Self-contained**: Include all necessary context, file paths, constraints, "
        "and requirements directly in `task`. Do not assume the SubAgent knows anything.\n"
        "- **Explicit over implicit**: State constraints explicitly (e.g., 'use UTF-8', "
        "'do not modify existing tests', 'output to /output/'). Never say 'as before' or "
        "'same as the file' without specifying which file.\n"
        "- **Atomic scope**: Keep the task focused. If it requires unrelated skills, "
        "split into multiple SubAgent calls.\n\n"
        "## Execution Modes:\n"
        "- **Synchronous** (default): Blocks until SubAgent completes\n"
        "- **Background** (`run_in_background: true`): Returns immediately with task_id\n\n"
        "## Background Task Management:\n"
        "- `run_in_background: true`: Start SubAgent in background, return task_id\n"
        "- `read_task(task_id)`: Check status and output of background SubAgent\n"
        "- `kill_task(task_id)`: Terminate background SubAgent\n"
        "- `list_tasks()`: List all background tasks (shell + SubAgent)\n\n"
        "## Input:\n"
        "- **task**: Clear, self-contained description of what to accomplish. Include context, constraints, and expected output format.\n"
        "- **plan**: Ordered list of concrete, actionable steps. Include file paths and verification criteria.\n\n"
        "## Output:\n"
        "- Synchronous: Returns JSON: {\"status\": \"completed\"/\"error\"/\"timeout\"/\"failed\", \"summary\": \"...\", \"iterations\": N}\n"
        "- Background: Returns task_id for later status checks\n\n"
        "## Important:\n"
        "- Timeout: 1 hour (3600s)\n"
        "- Cannot be called recursively (SubAgent cannot use `subagent` tool)\n"
        "- SubAgent has access to `llm` for single-turn LLM calls (translation, summarization, extraction)"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_check = None  # Set by RootAgent after tool creation
        self.on_subagent_start = None  # Callback(exec_id, task_summary) fired before sub-agent starts
        self.on_subagent_complete = None  # Callback(exec_id) fired when sub-agent finishes
        # Pending synchronous subagents: exec_id -> {thread, event, result, exec_id, subagent, task}
        self._pending_subagents: dict[str, dict] = {}
        self._pending_lock = threading.Lock()


    @property
    def parameters(self) -> dict:
        """Dynamic parameters schema."""
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "Clear, self-contained task description. "
                        "Include: objective, necessary context, constraints, expected output format. "
                        "Remember: SubAgent has NO access to your conversation history or project rules."
                    ),
                },
                "plan": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Execution plan: ordered list of concrete steps. "
                        "Each step should be actionable and self-explanatory. "
                        "Include file paths, expected outputs, or verification criteria where relevant."
                    ),
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "If true, run the SubAgent in background and return immediately "
                        "with a task_id. Use read_task/kill_task/list_tasks to manage it."
                    ),
                },
                "read_task": {
                    "type": "string",
                    "description": (
                        "Task ID to read status/output from (e.g., 'subagent-1'). "
                        "Returns SubAgent status and summary if completed."
                    ),
                },
                "kill_task": {
                    "type": "string",
                    "description": "Task ID of background SubAgent to terminate (e.g., 'subagent-1').",
                },
                "list_tasks": {
                    "type": "boolean",
                    "description": "If true, list all background tasks (shell commands and SubAgents).",
                },
            },
            "required": [],  # All parameters are optional; execute() validates
        }

    def execute(
        self,
        task: str | None = None,
        plan: list[str] | None = None,
        run_in_background: bool | None = None,
        read_task: str | None = None,
        kill_task: str | None = None,
        list_tasks: bool | None = None,
    ) -> ToolResult:
        """Execute the subagent tool - delegate task to a SubAgent or manage background tasks."""

        # Handle background task operations
        if list_tasks:
            return self._list_background_tasks()
        if read_task:
            # Try SubAgent first, then fall back to shell task
            from core.tools.shared.base import BackgroundTaskManager
            bg_task = BackgroundTaskManager.get(read_task)
            if bg_task and bg_task.task_type == "subagent":
                return self._read_background_subagent(read_task)
            else:
                return self._read_background_task(read_task)
        if kill_task:
            # Try SubAgent first, then fall back to shell task
            from core.tools.shared.base import BackgroundTaskManager
            bg_task = BackgroundTaskManager.get(kill_task)
            if bg_task and bg_task.task_type == "subagent":
                return self._kill_background_subagent(kill_task)
            else:
                return self._kill_background_task(kill_task)

        # Regular SubAgent execution
        if not task or not task.strip():
            return ToolResult("Error: 'task' is required and cannot be empty", error=True)

        # Deferred import to avoid circular dependency
        from core.sub_agent import SubAgent

        # Generate exec_id upfront so we can notify the UI immediately
        exec_id = ""
        if self.session_manager:
            exec_id = self.session_manager._generate_exec_id()

        # Fire callback to push SSE event immediately (before blocking on subagent.run())
        task_summary = task[:100]
        if self.on_subagent_start and exec_id:
            try:
                self.on_subagent_start(exec_id, task_summary)
            except Exception as e:
                logger.warning(f"on_subagent_start callback error: {e}")

        # Create exec directory for SubAgent logs and tool output files
        exec_dir = None
        if self.session_manager:
            exec_dir = self.session_manager.session_dir / exec_id
            exec_dir.mkdir(parents=True, exist_ok=True)

        # Create the SubAgent
        subagent = SubAgent(
            task=task,
            plan=plan,
            workspace_uuid=self.workspace_uuid,
            cwd=self.cwd,
            stop_check=self.stop_check,
            session_dir=exec_dir,
            exec_id=exec_id,
        )

        # Background mode
        if run_in_background:
            return self._start_background_subagent(
                subagent=subagent,
                session_manager=self.session_manager,
                exec_id=exec_id,
                task_summary=task_summary,
            )

        # Synchronous mode: start SubAgent in background thread, return placeholder.
        # The agent loop will exit (completed=False), SSE handler waits for completion,
        # writes back the result, then resumes the agent loop.
        entry = {"exec_id": exec_id, "thread": None, "event": threading.Event(), "result": None}

        def run_subagent():
            try:
                result = subagent.run()
                entry["result"] = result

                # Save SubAgent execution log via SessionManager
                if self.session_manager and exec_id:
                    try:
                        final_status = result.get("status", "error")
                        self.session_manager.save_subagent_log(
                            exec_id=exec_id,
                            task=task,
                            messages=subagent.messages,
                            metadata={
                                "started_at": subagent._started_at.strftime("%Y-%m-%d %H:%M:%S") if subagent._started_at else "",
                                "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "duration_seconds": subagent._elapsed_seconds(),
                                "status": final_status,
                                "iterations": result.get("iterations", 0),
                            },
                            summary=result.get("summary", ""),
                        )
                    except Exception as e:
                        logger.warning(f"Failed to save SubAgent log: {e}")

                # Forward sub-agent usage to session metadata
                sub_usage = result.get("usage", {})
                if self.session_manager and sub_usage:
                    self.session_manager.update_usage(
                        input_tokens=sub_usage.get("input_tokens", 0),
                        output_tokens=sub_usage.get("output_tokens", 0),
                        api_calls=0,
                        cache_read_tokens=sub_usage.get("cache_read_tokens", 0),
                        cache_creation_tokens=sub_usage.get("cache_creation_tokens", 0),
                    )

            except Exception as e:
                logger.error(f"SubAgent tool error: {e}")
                entry["result"] = {"status": "error", "summary": str(e), "iterations": 0}
            finally:
                subagent.close()
                # Persist main session after sub-agent writes
                if self.session_manager:
                    self.session_manager.save()
                # Signal completion
                entry["event"].set()
                # Fire completion callback
                if self.on_subagent_complete:
                    try:
                        self.on_subagent_complete(exec_id)
                    except Exception as e:
                        logger.warning(f"on_subagent_complete callback error: {e}")

        thread = threading.Thread(target=run_subagent, daemon=True)
        entry["thread"] = thread

        with self._pending_lock:
            self._pending_subagents[exec_id] = entry

        thread.start()

        # Return placeholder; agent loop will break (completed=False)
        return ToolResult(
            "SubAgent 执行中...",
            completed=False,
            meta={"exec_id": exec_id},
        )

    def get_pending_subagent(self, exec_id: str) -> dict | None:
        """Get pending subagent info by exec_id."""
        with self._pending_lock:
            return self._pending_subagents.get(exec_id)

    def remove_pending_subagent(self, exec_id: str) -> None:
        """Remove a completed subagent from pending dict."""
        with self._pending_lock:
            self._pending_subagents.pop(exec_id, None)
