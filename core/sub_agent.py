"""SubAgent - autonomous agent loop for delegated tasks.

Provides SubAgent.run() for executing tasks inside a Python script
with its own tool set and message history.

Key design:
- Inherits from BaseAgent for unified execution loop
- Task objective embedded in system prompt (immune to compression)
- Independent message history (self.messages)
- Execution logged to {session_dir}/index.json
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core.config import load_config
from core.llm import create_llm_client, format_llm_error
from core.base_agent import BaseAgent
from core.prompts import build_sub_prompt
from core.tools.sub import create_sub_tools

logger = logging.getLogger(__name__)


class _SessionIdRef:
    """简单的 session_id 引用，供工具获取 session 标识。

    SubAgent 使用 exec_id 作为 session 标识。
    """

    def __init__(self, session_id: str):
        self.session_id = session_id


class SubAgent(BaseAgent):
    """SubAgent for delegated task execution.

    - Full tool set (read/write/edit/bash/grep/find/python/browser/web_search/memory)
    - Nesting forbidden (subagent cannot delegate further)
    - Independent message history
    - Execution logged to separate directory
    """

    def __init__(
        self,
        task: str,
        plan: list[str] | None = None,
        workspace_uuid: str = "",
        cwd: str = "",
        max_consecutive_failures: int = 5,
        session_dir: Path | None = None,
        stop_check: Callable[[], bool] | None = None,
        exec_id: str = "",
        temperature: float | None = None,
    ):
        """Initialize SubAgent.

        Args:
            task: Task description
            plan: Optional execution plan (list of steps)
            workspace_uuid: Workspace UUID
            cwd: Working directory
            max_consecutive_failures: Maximum consecutive tool failures before abort
            session_dir: Directory for saving messages
            stop_check: Callable that returns True when parent stopped
            exec_id: Pre-assigned execution ID
            temperature: Optional temperature override for LLM (0.0~1.0)
        """
        self.task = task
        self.plan = plan
        self._exec_id = exec_id

        # Load config
        config = load_config()

        # Initialize base agent
        super().__init__(
            config=config,
            workspace_uuid=workspace_uuid,
            cwd=cwd or os.getcwd(),
            session_dir=session_dir,
            stop_check=stop_check,
            max_iterations=200,  # Fixed value
        )

        self._session_id = exec_id
        logger.debug(f"[SubAgent] Session ID set to: {exec_id}")

        # 创建 session 引用，供工具获取 session_id
        self._session_ref = _SessionIdRef(exec_id)

        # Create LLM client
        logger.debug(f"[SubAgent] Creating LLM client for task: {task[:50]}...")
        self.client = create_llm_client(config.model)
        if temperature is not None:
            self.client.temperature = temperature

        # Create sub tool set (pass session_ref for temp tool etc.)
        self.tools = create_sub_tools(
            cwd=self.cwd,
            workspace_uuid=self.workspace_uuid,
            session_manager=self._session_ref,
            config=config,
        )
        self.tool_schemas = [t.to_schema() for t in self.tools]

        # Build system prompt
        base_prompt = build_sub_prompt(self.workspace_uuid, self.cwd)
        task_section = self._build_task_section()
        self._system_prompt = base_prompt + "\n\n" + task_section if task_section else base_prompt

        # Execution tracking
        self._started_at: datetime | None = None
        self.max_consecutive_failures = max_consecutive_failures

    def _build_task_section(self) -> str:
        """Build task section to append to system prompt END."""
        lines = ["## Assigned Task", ""]

        lines.append("### Objective")
        lines.append("")
        lines.append(self.task)
        lines.append("")

        if self.plan:
            lines.append("### Execution Plan")
            lines.append("")
            for i, step in enumerate(self.plan, 1):
                lines.append(f"{i}. {step}")
            lines.append("")
            lines.append("Execute these steps in order. Report progress as you complete each step.")
            lines.append("")

        return "\n".join(lines)

    def run(self) -> dict[str, Any]:
        """Execute SubAgent loop, return structured result."""
        self._started_at = datetime.now()
        self._stopped = False
        self._running = True

        # Build initial message
        self.add_message("user", "Execute.")

        consecutive_failures = 0
        status = "completed"
        summary = ""

        try:
            for i in range(self.max_iterations):
                # Check stop
                if self.stop_check and self.stop_check():
                    status = "stopped"
                    summary = "Stopped by user"
                    self._finalize(status, summary, i)
                    return {"status": status, "message": summary, "iterations": i, "usage": self._usage}

                try:
                    # Compress if needed
                    self._check_and_compress()

                    # Call LLM (non-streaming)
                    response = self._call_llm(streaming=False, system_prompt=self._system_prompt)
                except Exception as e:
                    summary = format_llm_error(e, self.client.base_url if self.client else "")
                    task_brief = self.task[:50].replace("\n", " ")
                    logger.error(f"[SubAgent] LLM 错误 (iter={i}, exec={self._exec_id}, task='{task_brief}'): {summary}")
                    status = "error"
                    self._finalize(status, summary, i)
                    return {"status": status, "message": summary, "iterations": i, "usage": self._usage}

                tool_calls = response.get_tool_calls()

                if not tool_calls:
                    # Task complete
                    summary = response.get_text()
                    self.add_message("assistant", response.content_as_dicts())
                    status = "completed"
                    self._finalize(status, summary, i + 1)
                    return {"status": status, "summary": summary, "iterations": i + 1, "usage": self._usage}

                # Add assistant message with tool calls - convert to dicts for storage
                self.add_message("assistant", response.content_as_dicts())

                # Save progress
                self._save_progress(i + 1, status="running")

                # Execute tools
                for tc in tool_calls:
                    self._save_progress(i + 1, status="running", current_tool=tc.name)

                    # Parse arguments from raw JSON string to dict at execution time
                    input_data = tc.parse_arguments()
                    result = self._execute_tool(tc.name, input_data, tc.id)
                    self.add_message("user", [result])

                    self._save_progress(i + 1, status="running")

                    # Track consecutive failures (is_error is Anthropic format)
                    if result.get("is_error"):
                        consecutive_failures += 1
                        if consecutive_failures >= self.max_consecutive_failures:
                            status = "failed"
                            summary = f"Exceeded max consecutive failures ({self.max_consecutive_failures})"
                            self._finalize(status, summary, i + 1)
                            return {"status": status, "message": summary, "iterations": i + 1, "usage": self._usage}
                    else:
                        consecutive_failures = 0

            # Timeout
            status = "timeout"
            summary = f"Exceeded max iterations ({self.max_iterations})"
            self._finalize(status, summary, self.max_iterations)
            return {"status": status, "iterations": self.max_iterations, "usage": self._usage}

        finally:
            self._running = False

    def _elapsed_seconds(self) -> float:
        """Calculate elapsed seconds since start."""
        if self._started_at is None:
            return 0.0
        return (datetime.now() - self._started_at).total_seconds()

    def _save_progress(self, iterations: int, status: str = "running", current_tool: str = "") -> None:
        """Save execution progress in real-time.

        Writes to {exec_dir}/index.json in the format SessionManager.load_subagent_log() expects.
        This ensures the file always has exec_id and task, even before the final save.
        """
        if not self.session_dir or not self._exec_id:
            return

        metadata = {
            "parent_session_id": "",
            "session_id": self._session_id,
            "started_at": self._started_at.strftime("%Y-%m-%d %H:%M:%S") if self._started_at else "",
            "ended_at": None,
            "duration_seconds": self._elapsed_seconds(),
            "status": status,
            "iterations": iterations,
            "max_iterations": self.max_iterations,
            "message_count": len(self.messages),
            "current_tool": current_tool,
        }

        log_data = {
            "exec_id": self._exec_id,
            "session_id": self._session_id,
            "task": self.task,
            "metadata": metadata,
            "summary": "",
            "messages": self.messages,
        }

        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            log_file = self.session_dir / "index.json"
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(log_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[SubAgent] Failed to save progress: {e}")

    def _finalize(self, status: str, summary: str, iterations: int) -> None:
        """Finalize execution: save final log."""
        ended_at = datetime.now()
        duration_seconds = (ended_at - self._started_at).total_seconds() if self._started_at else 0.0

        if self.session_dir:
            metadata = {
                "parent_session_id": "",
                "session_id": self._session_id,
                "started_at": self._started_at.strftime("%Y-%m-%d %H:%M:%S") if self._started_at else "",
                "ended_at": ended_at.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_seconds": duration_seconds,
                "status": status,
                "iterations": iterations,
                "max_iterations": self.max_iterations,
                "message_count": len(self.messages),
                "summary": summary,
            }

            log_data = {
                "exec_id": self._exec_id,
                "session_id": self._session_id,
                "task": self.task,
                "metadata": metadata,
                "summary": summary,
                "messages": self.messages,
            }

            try:
                self.session_dir.mkdir(parents=True, exist_ok=True)
                log_file = self.session_dir / "index.json"
                with open(log_file, "w", encoding="utf-8") as f:
                    json.dump(log_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"[SubAgent] Failed to finalize: {e}")
