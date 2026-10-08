"""Session tool - delegate complex tasks to a sub-session runner."""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime

from core.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


class SessionTool(Tool):
    name = "session"
    description = (
        "**Delegate complex, multi-step tasks to an autonomous sub-session (Worker/Lite).**\n"
        "The sub-session runs a complete loop with its own tool access.\n\n"
        "## Actions:\n"
        "- \"start\" (default): delegate a task. Params: task (required), plan, agent_type, run_in_background, label, temperature\n"
        "- \"read\": check a background session's status/output. Params: task (pass task_id, e.g. \"agent-1\")\n"
        "- \"kill\": terminate a background session. Params: task (pass task_id)\n"
        "- \"list\": list all background tasks. No extra params needed.\n\n"
        "## Use when:\n"
        "- Multi-step work needing many tool calls (translate a file, analyze code, batch processing)\n"
        "- Long-running work that should not block the conversation (action=\"start\", run_in_background=true)\n\n"
        "## Do NOT use for:\n"
        "- Simple reads/edits/commands/search — use read/edit/bash/web_search directly\n\n"
        "## Task must be self-contained (action=\"start\"):\n"
        "The sub-session does NOT see your conversation or CLAUDE.md. Put all context, file paths, "
        "constraints, and expected output format directly in `task`. Never say 'as before' or reference prior turns.\n\n"
        "## Examples:\n"
        "- Start in background: {\"action\": \"start\", \"task\": \"...\", \"run_in_background\": true}\n"
        "- Check status: {\"action\": \"read\", \"task\": \"agent-1\"}\n"
        "- Kill: {\"action\": \"kill\", \"task\": \"agent-1\"}\n"
        "- List all: {\"action\": \"list\"}\n\n"
        "## Returns (action=\"start\"):\n"
        "{\"status\": \"completed|error|timeout|failed\", \"summary\": \"...\", \"iterations\": N}\n"
        "Timeout: 1 hour. Delegation limits: master (depth 0) may delegate to worker/lite; "
        "a sub-session (depth 1) may delegate only to 'lite'; sessions two levels deep cannot delegate."
    )

    def __init__(self, *args, config=None, approval_store=None, delegation_depth: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = config  # 全局配置，构造子 SessionRunner 用（角色模型继承）
        self.approval_store = approval_store  # 根 runner 的会话级审批存储，传给子 runner 共享
        self.delegation_depth = delegation_depth  # 当前 runner 的委派深度（master=0；depth1 仅可委派 lite；depth≥2 禁止）
        self.stop_check = None  # Set by master SessionRunner after tool creation
        self.on_session_start = None  # Callback(exec_id, task_summary) fired before sub-session starts
        self.on_session_complete = None  # Callback(exec_id) fired when sub-session finishes
        self.on_background_complete = None  # Callback(exec_id, status) fired when background session completes (for auto-resume)
        # Pending synchronous sessions: exec_id -> {thread, event, result, exec_id, runner, task}
        self._pending_sessions: dict[str, dict] = {}
        self._pending_lock = threading.Lock()


    def _publish(self, ev_type: str, **extra) -> None:
        """发布事件到全局事件流（worker 事件）。事件壳带 workspace_uuid/session_id。"""
        try:
            sm = self.session
            if sm is None:
                return
            session_id = getattr(sm, "session_id", "")
            if not session_id:
                return
            from core.event_bus import get_event_bus
            get_event_bus().publish({
                "type": ev_type,
                "workspace_uuid": self.workspace_uuid,
                "session_id": session_id,
                **extra,
            })
        except Exception:
            logger.debug(f"event publish failed: {ev_type}", exc_info=True)

    def _notify_session_start(self, exec_id: str, task_summary: str, background: bool = False) -> None:
        """广播 session_start：保留 on_session_start 回调（master POST SSE）+ 全局事件流。"""
        if self.on_session_start and exec_id:
            try:
                self.on_session_start(exec_id, task_summary)
            except Exception as e:
                logger.warning(f"on_session_start callback error: {e}")
        if exec_id:
            self._publish("session_start", exec_id=exec_id, task_summary=task_summary, background=background)

    def _notify_session_complete(self, exec_id: str, status: str = "completed") -> None:
        """广播 session_complete：保留 on_session_complete 回调 + 全局事件流。"""
        if exec_id:
            self._publish("session_complete", exec_id=exec_id, status=status)
        if self.on_session_complete:
            try:
                self.on_session_complete(exec_id)
            except Exception as e:
                logger.warning(f"on_session_complete callback error: {e}")

    @property
    def parameters(self) -> dict:
        """Dynamic parameters schema."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "read", "kill", "list"],
                    "description": (
                        "Operation to perform. Defaults to 'start' if omitted. "
                        "'start': delegate a task (requires 'task' as description). "
                        "'read': check background agent status (requires 'task' as task_id, e.g. 'agent-1'). "
                        "'kill': terminate a background agent (requires 'task' as task_id). "
                        "'list': list all background tasks."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "For action='start': clear, self-contained task description. "
                        "Include objective, necessary context, constraints, expected output format. "
                        "Agent has NO access to your conversation history or project rules.\n"
                        "For action='read'/'kill': the task_id string (e.g. 'agent-1')."
                    ),
                },
                "plan": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Execution plan: ordered list of concrete steps (action='start' only). "
                        "Each step should be actionable and self-explanatory. "
                        "Include file paths, expected outputs, or verification criteria where relevant."
                    ),
                },
                "agent_type": {
                    "type": "string",
                    "enum": ["worker", "lite"],
                    "description": (
                        "Sub-agent role (action='start' only): 'worker' (default, full tool set + check phase) "
                        "or 'lite' (minimal read/write/edit/bash, no check phase). "
                        "If you are already a sub-runner (delegation depth 1), only 'lite' is allowed."
                    ),
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "action='start' only. If true, run the Agent in background and return immediately "
                        "with a task_id. You will receive an automatic notification when it "
                        "completes — no polling needed."
                    ),
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
        action: str | None = None,
        task: str | None = None,
        plan: list[str] | None = None,
        agent_type: str = "worker",
        run_in_background: bool | None = None,
        temperature: float | None = None,
        label: str | None = None,
        exec_id: str | None = None,
    ) -> ToolResult:
        """Execute the session tool - delegate task to a SessionRunner or manage background tasks.

        Dispatch logic:
        - action='read': read background session status (task = task_id)
        - action='kill': kill background session (task = task_id)
        - action='list': list all background tasks
        - action='start' (or omitted): start a new session (default)
        """
        effective_action = action or "start"

        # Handle read / kill / list operations
        if effective_action == "list":
            return self._list_background_tasks()
        if effective_action == "read":
            if not task:
                return ToolResult("Error: action='read' requires 'task' parameter (task_id, e.g. 'agent-1')", error=True)
            from core.tools.base import BackgroundTaskManager
            bg_task = BackgroundTaskManager.get(task)
            if bg_task and bg_task.task_type == "session":
                return self._read_background_runner(task)
            else:
                return self._read_background_task(task)
        if effective_action == "kill":
            if not task:
                return ToolResult("Error: action='kill' requires 'task' parameter (task_id, e.g. 'agent-1')", error=True)
            from core.tools.base import BackgroundTaskManager
            bg_task = BackgroundTaskManager.get(task)
            if bg_task and bg_task.task_type == "session":
                return self._kill_background_runner(task)
            else:
                return self._kill_background_task(task)

        # Regular Agent execution
        if not task or not task.strip():
            return ToolResult("Error: 'task' is required and cannot be empty", error=True)

        if agent_type not in ("worker", "lite"):
            return ToolResult(
                f"Error: invalid agent_type '{agent_type}' (expected 'worker' or 'lite')",
                error=True,
            )

        # Delegation depth limit:
        #   depth 0 (master): may delegate to worker or lite.
        #   depth 1 (sub-runner): may delegate only to lite (lighter, budget-bounded).
        #   depth >= 2: cannot delegate further — complete the task directly.
        if self.delegation_depth >= 2:
            return ToolResult(
                "Error: delegation depth limit reached (max 2 levels). "
                "An agent two levels below master must complete the task directly "
                "without delegating to another runner.",
                error=True,
            )
        if self.delegation_depth == 1 and agent_type != "lite":
            return ToolResult(
                "Error: a sub-runner (delegation depth 1) may only delegate to a "
                "'lite' agent, not to another worker. Complete the task directly, "
                "or delegate with agent_type='lite'.",
                error=True,
            )

        # Deferred import to avoid circular dependency
        from core.session_runner import SessionRunner

        # Generate exec_id upfront so we can notify the UI immediately
        # _SessionRef（worker/lite 的 session 引用）无 agent_logs，
        # 深度限制放开委派时避免 AttributeError，回退生成随机 exec_id（T28）
        # GoalRunner 会预生成 exec_id 传入，以便占位消息引用同一 id
        if not exec_id:
            exec_id = ""
            if self.session:
                gen = getattr(getattr(self.session, "agent_logs", None), "_generate_exec_id", None)
                if callable(gen):
                    exec_id = gen()
                else:
                    exec_id = f"sub-{secrets.token_hex(4)}"

        # Fire callback + 全局事件流广播（before blocking on runner.run()）
        task_summary = task[:100]
        self._notify_session_start(exec_id, task_summary, background=bool(run_in_background))

        # Create exec directory for sub-session logs and tool output files
        exec_dir = None
        if self.session:
            exec_dir = self.session.session_dir / exec_id
            exec_dir.mkdir(parents=True, exist_ok=True)

        # Create the sub-session (统一 SessionRunner，autonomous 模式，角色由 agent_type 决定)
        runner = SessionRunner(
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

        # 全局事件流：worker 逐 token 消息与工具增量 → 事件总线（实时推送，去前端轮询）。
        # 这些是 BaseSessionRunner 的普通实例属性（默认 None），run() 前赋值即可，无需改 runner 循环。
        runner._on_text = lambda content, _id=exec_id: self._publish(
            "text", exec_id=_id, content=content)
        runner._on_thinking = lambda content, _id=exec_id: self._publish(
            "thinking", exec_id=_id, content=content)
        runner._on_tool_call = lambda tool, input_data, tool_use_id, _id=exec_id: self._publish(
            "tool_use", exec_id=_id, tool=tool, input=input_data, tool_use_id=tool_use_id)
        runner._on_tool_result = lambda tool, content, is_error, tool_use_id, _id=exec_id: self._publish(
            "tool_result", exec_id=_id, tool=tool, content=content, is_error=is_error,
            tool_use_id=tool_use_id)
        runner._on_tool_output = lambda tool, content, offset, tool_use_id, _id=exec_id: self._publish(
            "tool_output", exec_id=_id, tool=tool, content=content, offset=offset,
            tool_use_id=tool_use_id)
        # 进度事件发布回调（_save_progress 调用时广播 iterations/message_count/tool_call_count）
        runner._event_publisher = lambda ev_type, **kw: self._publish(ev_type, **kw)

        # 注册子 runner 到 MessageBus（支持 runner 级消息传递）
        # exec_id 作为主地址，label 作为可选别名
        sub_runner_session_id = exec_id  # autonomous 模式 session_id = exec_id
        try:
            from core.message_bus import get_message_bus
            mbus = get_message_bus()
            mbus.register_agent(exec_id, sub_runner_session_id)
            if label and label != exec_id:
                mbus.register_agent(label, sub_runner_session_id)
        except Exception as e:
            logger.warning(f"Failed to register sub-runner with MessageBus: {e}")

        # Background mode
        if run_in_background:
            return self._start_background_runner(
                runner=runner,
                session=self.session,
                exec_id=exec_id,
                task_summary=task_summary,
            )

        # Synchronous mode: start Agent in background thread, wait for result.
        # The runner loop continues without exiting — no external resume needed.
        entry = {"exec_id": exec_id, "thread": None, "event": threading.Event(), "result": None}

        def run_session():
            try:
                result = runner.run()
                result["message_count"] = len(runner.messages)
                entry["result"] = result

                # Save Agent execution log via SessionManager
                # worker/lite 的 session 是 _SessionRef（无 agent_logs），跳过
                if self.session and exec_id and hasattr(self.session, "agent_logs"):
                    try:
                        final_status = result.get("status", "error")
                        self.session.agent_logs.save_agent_log(
                            exec_id=exec_id,
                            task=task,
                            messages=runner.messages,
                            metadata={
                                "started_at": runner._started_at.strftime("%Y-%m-%d %H:%M:%S") if runner._started_at else "",
                                "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "duration_seconds": runner._elapsed_seconds(),
                                "status": final_status,
                                "iterations": result.get("iterations", 0),
                                "message_count": result.get("message_count", 0),
                            },
                            summary=result.get("summary") or result.get("message") or "",
                        )
                    except Exception as e:
                        logger.warning(f"Failed to save Agent log: {e}")

                # Forward sub-runner usage to session metadata
                sub_usage = result.get("usage", {})
                if self.session and sub_usage:
                    try:
                        self.session.update_usage(
                            input_tokens=sub_usage.get("input_tokens", 0),
                            output_tokens=sub_usage.get("output_tokens", 0),
                            api_calls=0,
                            cache_read_tokens=sub_usage.get("cache_read_tokens", 0),
                            cache_creation_tokens=sub_usage.get("cache_creation_tokens", 0),
                        )
                    except Exception as e:
                        logger.warning(f"Failed to forward sub-runner usage: {e}")

            except Exception as e:
                logger.error(f"Agent tool error: {e}")
                entry["result"] = {"status": "error", "summary": str(e), "iterations": 0}
            finally:
                runner.close()
                # 注销子代理的 MessageBus 注册（exec_id + 所有别名如 label）
                try:
                    from core.message_bus import get_message_bus
                    mbus = get_message_bus()
                    for agent_name in mbus.get_session_agents(exec_id):
                        mbus.unregister_agent(agent_name)
                except Exception as e:
                    logger.warning(f"Failed to unregister sub-runner from MessageBus: {e}")
                # Persist main session after sub-runner writes.
                # worker/lite 的 _SessionRef 无 save()：此处若抛异常，
                # 下方 event.set() 永不执行，委派方会永久阻塞在 event.wait(3600)。
                try:
                    if self.session and hasattr(self.session, "save"):
                        self.session.save()
                except Exception as e:
                    logger.warning(f"Failed to save session after sub-runner: {e}")
                # Signal completion（必须无条件执行）
                entry["event"].set()
                # Fire completion callback + 事件流广播
                status = (entry.get("result") or {}).get("status", "completed")
                self._notify_session_complete(exec_id, status=status)

        thread = threading.Thread(target=run_session, daemon=True)
        entry["thread"] = thread

        with self._pending_lock:
            self._pending_sessions[exec_id] = entry

        thread.start()

        # Wait for Agent to complete (synchronous mode)
        if not entry["event"].wait(timeout=3600):
            # 超时：终止孤儿线程，避免后台继续消耗 token。
            # runner.stop() 置停止标记，主循环下一轮即退出；给清理留宽限期。
            logger.warning(f"[SessionTool] 委派执行超时，停止子 runner {exec_id}")
            runner.stop()
            entry["event"].wait(timeout=10)
        result = entry.get("result") or {"status": "timeout", "summary": "Agent 执行超时", "iterations": 0}

        # Clean up pending entry
        with self._pending_lock:
            self._pending_sessions.pop(exec_id, None)

        result_json = json.dumps(result, ensure_ascii=False, indent=2)
        meta = {"exec_id": exec_id, "completed": True, "iterations": result.get("iterations", 0), "message_count": result.get("message_count", 0), "tool_call_count": result.get("tool_call_count", 0), "background": False}
        if label:
            meta["label"] = label[:64]
        return ToolResult(result_json, completed=True, meta=meta)

    def get_pending_session(self, exec_id: str) -> dict | None:
        """Get pending session info by exec_id."""
        with self._pending_lock:
            return self._pending_sessions.get(exec_id)

    def remove_pending_session(self, exec_id: str) -> None:
        """Remove a completed session from pending dict."""
        with self._pending_lock:
            self._pending_sessions.pop(exec_id, None)
