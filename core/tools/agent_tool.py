"""Agent tool - delegate complex tasks to a sub-agent."""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime

from core.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


class AgentTool(Tool):
    name = "agent"
    description = (
        "**Delegate complex, multi-step tasks to an autonomous sub-agent (Worker/Lite).**\n"
        "The sub-agent runs a complete agent loop with its own tool access.\n\n"
        "## Use when:\n"
        "- Multi-step work needing many tool calls (translate a file, analyze code, batch processing)\n"
        "- Long-running work that should not block the conversation (run_in_background=true)\n\n"
        "## Do NOT use for:\n"
        "- Simple reads/edits/commands/search — use read/edit/bash/web_search directly\n\n"
        "## Task must be self-contained:\n"
        "The sub-agent does NOT see your conversation or CLAUDE.md. Put all context, file paths, "
        "constraints, and expected output format directly in `task`. Never say 'as before' or reference prior turns.\n\n"
        "## Params:\n"
        "- task: self-contained objective (required)\n"
        "- plan: optional ordered steps with file paths and verification criteria\n"
        "- agent_type: \"worker\" (default, full tool set + check phase) or \"lite\" (read/write/edit/bash only)\n"
        "- run_in_background: true returns a task_id immediately; manage via read_task/kill_task/list_tasks\n\n"
        "## Returns:\n"
        "{\"status\": \"completed|error|timeout|failed\", \"summary\": \"...\", \"iterations\": N}\n"
        "Timeout: 1 hour. Delegation is limited to 1 level: only master may delegate; a worker/lite "
        "calling this tool gets an error and must complete the task directly."
    )

    def __init__(self, *args, config=None, approval_store=None, delegation_depth: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = config  # 全局配置，构造子 Agent 用（角色模型继承）
        self.approval_store = approval_store  # 根代理的会话级审批存储，传给子代理共享
        self.delegation_depth = delegation_depth  # 当前代理的委派深度（master=0，最大 1 层）
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
        # _SessionIdRef（worker/lite 的 session 引用）无 _generate_exec_id，
        # 深度限制放开委派时避免 AttributeError，回退生成随机 exec_id（T28）
        exec_id = ""
        if self.session_manager:
            gen = getattr(self.session_manager, "_generate_exec_id", None)
            if callable(gen):
                exec_id = gen()
            else:
                exec_id = f"sub-{secrets.token_hex(4)}"

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
                            summary=result.get("summary") or result.get("message") or "",
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
        if not entry["event"].wait(timeout=3600):
            # 超时：终止孤儿线程，避免后台继续消耗 token。
            # agent.stop() 置停止标记，主循环下一轮即退出；给清理留宽限期。
            logger.warning(f"[AgentTool] 委派执行超时，停止子代理 {exec_id}")
            agent.stop()
            entry["event"].wait(timeout=10)
        result = entry.get("result") or {"status": "timeout", "summary": "Agent 执行超时", "iterations": 0}

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
