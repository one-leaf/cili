"""Agent tool - delegate complex tasks to a sub-agent."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from core.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


class AgentTool(Tool):
    name = "agent"
    description = (
        "**Delegate complex, multi-step tasks to an autonomous sub-agent (Worker/Lite).**\n"
        "The sub-agent runs a complete agent loop with tool access "
        "(read/write/edit/bash/python/grep/find/browser/web_search/memory).\n\n"
        "## When to use:\n"
        "- File processing that requires multiple read/write operations\n"
        "- Complex workflows needing tool interaction (e.g., translate file, analyze code)\n"
        "- Tasks too complex for a single tool call\n"
        "- Long-running tasks that should not block the main conversation\n\n"
        "## When NOT to use:\n"
        "- Simple file reads (use `read`)\n"
        "- Single command execution (use `bash`)\n"
        "- Python code execution (use `python`)\n"
        "- Web searches (use `web_search`)\n\n"
        "## Task Description Guidelines:\n"
        "Sub-agent runs in isolation — it does NOT see your conversation history, "
        "project rules (CLAUDE.md), or any implicit context. You must provide a "
        "complete, self-contained task description.\n"
        "- **Self-contained**: Include all necessary context, file paths, constraints, "
        "and requirements directly in `task`. Do not assume the sub-agent knows anything.\n"
        "- **Explicit over implicit**: State constraints explicitly (e.g., 'use UTF-8', "
        "'do not modify existing tests', 'output to /output/'). Never say 'as before' or "
        "'same as the file' without specifying which file.\n"
        "- **Atomic scope**: Keep the task focused. If it requires unrelated skills, "
        "split into multiple sub-agent calls.\n\n"
        "## Execution Modes:\n"
        "- **Synchronous** (default): Blocks until sub-agent completes\n"
        "- **Background** (`run_in_background: true`): Returns immediately with task_id\n\n"
        "## Background Task Management:\n"
        "- `run_in_background: true`: Start sub-agent in background, return task_id\n"
        "- `read_task(task_id)`: Check status and output of background sub-agent\n"
        "- `kill_task(task_id)`: Terminate background sub-agent\n"
        "- `list_tasks()`: List all background tasks (shell + sub-agent)\n\n"
        "## Input:\n"
        "- **task**: Clear, self-contained description of what to accomplish. Include context, constraints, and expected output format.\n"
        "- **plan**: Ordered list of concrete, actionable steps. Include file paths and verification criteria.\n"
        "- **agent_type**: `\"worker\"` (default, full tool set + check phase) or `\"lite\"` (minimal read/write/edit/bash, no check phase).\n\n"
        "## Output:\n"
        "- Synchronous: Returns JSON: {\"status\": \"completed\"/\"error\"/\"timeout\"/\"failed\", \"summary\": \"...\", \"iterations\": N}\n"
        "- Background: Returns task_id for later status checks\n\n"
        "## Important:\n"
        "- Timeout: 1 hour (3600s)\n"
        "- **Delegation depth limit (1 level)**: only master can delegate — to a worker\n"
        "  or lite (depth 1). A worker/lite at depth 1 cannot delegate further; calling\n"
        "  this tool returns an error, so complete the task directly."
    )

    def __init__(self, *args, config=None, approval_store=None, delegation_depth: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = config  # 全局配置，构造子 Agent 用（角色模型继承）
        self.approval_store = approval_store  # 根代理的会话级审批存储，传给子代理共享
        self.delegation_depth = delegation_depth  # 当前代理的委派深度（master=0，最大 2 层）
        self.stop_check = None  # Set by master Agent after tool creation
        self.on_agent_start = None  # Callback(exec_id, task_summary) fired before sub-agent starts
        self.on_agent_complete = None  # Callback(exec_id) fired when sub-agent finishes
        # Pending synchronous agents: exec_id -> {thread, event, result, exec_id, agent, task}
        self._pending_agents: dict[str, dict] = {}
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
                        "Remember: Agent has NO access to your conversation history or project rules."
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
                "agent_type": {
                    "type": "string",
                    "enum": ["worker", "lite"],
                    "description": (
                        "Sub-agent role: 'worker' (default, full tool set + check phase) "
                        "or 'lite' (minimal read/write/edit/bash, no check phase)."
                    ),
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "If true, run the Agent in background and return immediately "
                        "with a task_id. Use read_task/kill_task/list_tasks to manage it."
                    ),
                },
                "read_task": {
                    "type": "string",
                    "description": (
                        "Task ID to read status/output from (e.g., 'agent-1'). "
                        "Returns Agent status and summary if completed."
                    ),
                },
                "kill_task": {
                    "type": "string",
                    "description": "Task ID of background Agent to terminate (e.g., 'agent-1').",
                },
                "list_tasks": {
                    "type": "boolean",
                    "description": "If true, list all background tasks (shell commands and Agents).",
                },
                "temperature": {
                    "type": "number",
                    "description": "Override LLM temperature for this Agent (0.0=deterministic, 1.0=creative). Defaults to the configured model temperature.",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "label": {
                    "type": "string",
                    "description": "Short display label for this Agent (max 64 chars). Shown in UI instead of task summary.",
                },
            },
            "required": [],  # All parameters are optional; execute() validates
        }

    def execute(
        self,
        task: str | None = None,
        plan: list[str] | None = None,
        agent_type: str = "worker",
        run_in_background: bool | None = None,
        read_task: str | None = None,
        kill_task: str | None = None,
        list_tasks: bool | None = None,
        temperature: float | None = None,
        label: str | None = None,
    ) -> ToolResult:
        """Execute the agent tool - delegate task to a Agent or manage background tasks."""

        # Handle background task operations
        if list_tasks:
            return self._list_background_tasks()
        if read_task:
            # Try Agent first, then fall back to shell task
            from core.tools.base import BackgroundTaskManager
            bg_task = BackgroundTaskManager.get(read_task)
            if bg_task and bg_task.task_type == "agent":
                return self._read_background_agent(read_task)
            else:
                return self._read_background_task(read_task)
        if kill_task:
            # Try Agent first, then fall back to shell task
            from core.tools.base import BackgroundTaskManager
            bg_task = BackgroundTaskManager.get(kill_task)
            if bg_task and bg_task.task_type == "agent":
                return self._kill_background_agent(kill_task)
            else:
                return self._kill_background_task(kill_task)

        # Regular Agent execution
        if not task or not task.strip():
            return ToolResult("Error: 'task' is required and cannot be empty", error=True)

        # Delegation depth limit: only master (depth 0) may delegate, to a worker or
        # lite. A worker/lite at depth >= 1 cannot delegate further — complete directly.
        if self.delegation_depth >= 1:
            return ToolResult(
                "Error: delegation depth limit reached (max 1 level). "
                "Only master can delegate (to a worker or lite); a sub-agent must "
                "complete the task directly without delegating to another agent.",
                error=True,
            )

        # Deferred import to avoid circular dependency
        from core.agent import Agent

        if agent_type not in ("worker", "lite"):
            return ToolResult(
                f"Error: invalid agent_type '{agent_type}' (expected 'worker' or 'lite')",
                error=True,
            )

        # Generate exec_id upfront so we can notify the UI immediately
        exec_id = ""
        if self.session_manager:
            exec_id = self.session_manager._generate_exec_id()

        # Fire callback to push SSE event immediately (before blocking on agent.run())
        task_summary = task[:100]
        if self.on_agent_start and exec_id:
            try:
                self.on_agent_start(exec_id, task_summary)
            except Exception as e:
                logger.warning(f"on_agent_start callback error: {e}")

        # Create exec directory for sub-agent logs and tool output files
        exec_dir = None
        if self.session_manager:
            exec_dir = self.session_manager.session_dir / exec_id
            exec_dir.mkdir(parents=True, exist_ok=True)

        # Create the sub-agent (统一 Agent，autonomous 模式，角色由 agent_type 决定)
        agent = Agent(
            config=self.config,
            role=agent_type,
            task=task,
            plan=plan,
            workspace_uuid=self.workspace_uuid,
            cwd=self.cwd,
            stop_check=self.stop_check,
            session_dir=exec_dir,
            exec_id=exec_id,
            temperature=temperature,
            approval_store=self.approval_store,
            delegation_depth=self.delegation_depth + 1,
        )

        # Background mode
        if run_in_background:
            return self._start_background_agent(
                agent=agent,
                session_manager=self.session_manager,
                exec_id=exec_id,
                task_summary=task_summary,
            )

        # Synchronous mode: start Agent in background thread, wait for result.
        # The agent loop continues without exiting — no external resume needed.
        entry = {"exec_id": exec_id, "thread": None, "event": threading.Event(), "result": None}

        def run_agent():
            try:
                result = agent.run()
                result["message_count"] = len(agent.messages)
                entry["result"] = result

                # Save Agent execution log via SessionManager
                if self.session_manager and exec_id:
                    try:
                        final_status = result.get("status", "error")
                        self.session_manager.save_agent_log(
                            exec_id=exec_id,
                            task=task,
                            messages=agent.messages,
                            metadata={
                                "started_at": agent._started_at.strftime("%Y-%m-%d %H:%M:%S") if agent._started_at else "",
                                "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "duration_seconds": agent._elapsed_seconds(),
                                "status": final_status,
                                "iterations": result.get("iterations", 0),
                            },
                            summary=result.get("summary", ""),
                        )
                    except Exception as e:
                        logger.warning(f"Failed to save Agent log: {e}")

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
                logger.error(f"Agent tool error: {e}")
                entry["result"] = {"status": "error", "summary": str(e), "iterations": 0}
            finally:
                agent.close()
                # Persist main session after sub-agent writes
                if self.session_manager:
                    self.session_manager.save()
                # Signal completion
                entry["event"].set()
                # Fire completion callback
                if self.on_agent_complete:
                    try:
                        self.on_agent_complete(exec_id)
                    except Exception as e:
                        logger.warning(f"on_agent_complete callback error: {e}")

        thread = threading.Thread(target=run_agent, daemon=True)
        entry["thread"] = thread

        with self._pending_lock:
            self._pending_agents[exec_id] = entry

        thread.start()

        # Wait for Agent to complete (synchronous mode)
        entry["event"].wait(timeout=3600)
        result = entry.get("result") or {"status": "error", "summary": "Agent 执行超时", "iterations": 0}

        # Clean up pending entry
        with self._pending_lock:
            self._pending_agents.pop(exec_id, None)

        result_json = json.dumps(result, ensure_ascii=False, indent=2)
        meta = {"exec_id": exec_id, "completed": True, "iterations": result.get("iterations", 0), "message_count": result.get("message_count", 0)}
        if label:
            meta["label"] = label[:64]
        return ToolResult(result_json, completed=True, meta=meta)

    def get_pending_agent(self, exec_id: str) -> dict | None:
        """Get pending agent info by exec_id."""
        with self._pending_lock:
            return self._pending_agents.get(exec_id)

    def remove_pending_agent(self, exec_id: str) -> None:
        """Remove a completed agent from pending dict."""
        with self._pending_lock:
            self._pending_agents.pop(exec_id, None)
