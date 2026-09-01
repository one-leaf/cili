"""RootAgent - main agent for user interaction.

Handles user conversations, tool calls, and streaming output.
Uses SessionManager for persistent session storage.

Note: self.messages is the same object as session_manager.messages,
so changes to either are automatically reflected in both.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from core.config import Config, PROJECT_ROOT
from core.llm import create_llm_client
from core.base_agent import BaseAgent
from core.session import SessionManager
from core.prompts import build_root_prompt
from core.tools import create_tools, get_tool_by_name

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 200


class RootAgent(BaseAgent):
    """Main agent for user interaction with streaming support."""

    def __init__(self, config: Config, cwd: str | None = None, workspace_uuid: str = ""):
        self._cwd_init = os.path.abspath(cwd or os.getcwd())

        # Setup sessions directory
        from core.config import get_workspace_data_dir
        self.sessions_dir = get_workspace_data_dir(workspace_uuid) / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        # Create session manager for persistence
        self.session_manager = SessionManager("", self.sessions_dir)
        self.current_session_id: str = ""

        # Load or create session
        sessions = SessionManager.list_sessions(self.sessions_dir)
        if sessions:
            latest = max(sessions, key=lambda s: s.get("updated_at", ""))
            sid = latest["session_id"]
            loaded = SessionManager.load_session(sid, self.sessions_dir)
            if loaded:
                self.session_manager = loaded
                self.current_session_id = sid
            else:
                self._create_default_session()
        else:
            self._create_default_session()

        # Initialize base agent
        session_dir = self.sessions_dir / self.current_session_id
        super().__init__(
            config=config,
            workspace_uuid=workspace_uuid,
            cwd=self._cwd_init,
            session_dir=session_dir,
            max_iterations=_MAX_ITERATIONS,
        )

        self._session_id = self.current_session_id

        # Create LLM client
        self.client = create_llm_client(config.model)

        # IMPORTANT: Share messages list with session_manager (not copy!)
        # This ensures web_api.py operations on session_manager.messages
        # are automatically reflected in self.messages
        self.messages = self.session_manager.messages
        self._usage = self.session_manager.get_usage()

        # Build tools
        self._on_subagent_start: Callable[[str, str], None] | None = None
        self._rebuild_tools()

    def _create_default_session(self) -> None:
        """Create a new default session."""
        session = SessionManager.create_new_session(self.sessions_dir, "Default")
        self.session_manager = session
        self.current_session_id = session.session_id

    def _rebuild_tools(self) -> None:
        """Create tool instances and wire callbacks."""
        self.tools = create_tools(
            cwd=self.cwd,
            workspace_uuid=self.workspace_uuid,
            session_manager=self.session_manager,
            config=self.config,
        )
        self.tool_schemas = [t.to_schema() for t in self.tools]

        # Wire subagent callbacks
        subagent_tool = get_tool_by_name(self.tools, "subagent")
        if subagent_tool:
            subagent_tool.stop_check = lambda: self._stopped
            subagent_tool.on_subagent_start = lambda exec_id, task_summary: (
                self._on_subagent_start(exec_id, task_summary)
                if self._on_subagent_start else None
            )

    def reload_config(self) -> None:
        """Reload config from disk and recreate LLM client."""
        from core.config import load_config
        try:
            self.config = load_config()
            self.client.close()
            self.client = create_llm_client(self.config.model)
            self._rebuild_tools()
        except Exception as e:
            logger.warning(f"[RootAgent] 重新加载配置失败: {e}")

    def run(
        self,
        user_input: str | list[dict],
        on_text: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict, str], None] | None = None,
        on_tool_result: Callable[[str, str, bool, str], None] | None = None,
        on_subagent_start: Callable[[str, str], None] | None = None,
    ) -> None:
        """Run one turn of the agent loop."""
        self._stopped = False
        self._running = True
        self._on_text = on_text
        self._on_thinking = on_thinking
        self._on_tool_call = on_tool_call
        self._on_tool_result = on_tool_result
        self._on_subagent_start = on_subagent_start

        try:
            # Save previous turn
            self._sync_to_session_manager()
            self.session_manager.save()

            # Inject project instructions if not already present
            self._inject_project_instructions()

            # Add user message
            self.add_message("user", user_input)

            iteration = 0
            while iteration < self.max_iterations:
                # Check stop
                if self._stopped:
                    logger.info("[RootAgent] 已停止")
                    self._sync_to_session_manager()
                    self.session_manager.save()
                    if self._on_text:
                        self._on_text("\n\n[已停止]")
                    break

                iteration += 1

                # Compress if needed
                self._check_and_compress()

                # Call LLM with streaming
                system_prompt = build_root_prompt(self.workspace_uuid, self.cwd)
                response = self._call_llm(streaming=True, system_prompt=system_prompt)

                if self._stopped:
                    logger.info("[RootAgent] 已停止")
                    self._sync_to_session_manager()
                    self.session_manager.save()
                    break

                # Add assistant response - convert typed blocks to dicts for message storage
                self.add_message("assistant", response.content_as_dicts())

                # Check if there are tool calls (typed blocks)
                tool_call_blocks = response.get_tool_calls()

                if not tool_call_blocks:
                    # No tool calls - conversation turn complete
                    self._sync_to_session_manager()
                    self.session_manager.save()
                    break

                # Process tool calls
                wait_for_user = False
                for block in tool_call_blocks:
                    if self._stopped:
                        break
                    # Parse arguments from raw JSON string to dict at execution time
                    input_data = block.parse_arguments()
                    result = self._execute_tool(block.name, input_data, block.id)
                    # Check if we need to wait for user input
                    if result.get("_wait_for_user"):
                        wait_for_user = True
                    # Add tool result to messages
                    self.add_message("user", [result])
                    # Sync to session manager
                    self._sync_to_session_manager()
                    self.session_manager.save()

                if wait_for_user:
                    # Exit loop to wait for user input
                    logger.info("[RootAgent] Waiting for user input")
                    self._sync_to_session_manager()
                    self.session_manager.save()
                    break

                if self._stopped:
                    logger.info("[RootAgent] 已停止")
                    self._sync_to_session_manager()
                    self.session_manager.save()
                    break
            else:
                logger.warning(f"[RootAgent] 达到最大调用次数 ({self.max_iterations})")
                self._sync_to_session_manager()
                self.session_manager.save()

        finally:
            self._running = False

    def resume_after_ask_user(
        self,
        on_text: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict, str], None] | None = None,
        on_tool_result: Callable[[str, str, bool, str], None] | None = None,
        on_subagent_start: Callable[[str, str], None] | None = None,
    ) -> None:
        """Resume agent loop after ask_user tool result has been injected."""
        self._stopped = False
        self._running = True
        self._on_text = on_text
        self._on_thinking = on_thinking
        self._on_tool_call = on_tool_call
        self._on_tool_result = on_tool_result
        self._on_subagent_start = on_subagent_start

        try:
            # Sync to session manager
            self._sync_to_session_manager()
            self.session_manager.save()

            iteration = 0
            while iteration < self.max_iterations:
                # Check stop
                if self._stopped:
                    logger.info("[RootAgent] 已停止")
                    self._sync_to_session_manager()
                    self.session_manager.save()
                    if self._on_text:
                        self._on_text("\n\n[已停止]")
                    break

                iteration += 1

                # Compress if needed
                self._check_and_compress()

                # Call LLM with streaming
                system_prompt = build_root_prompt(self.workspace_uuid, self.cwd)
                response = self._call_llm(streaming=True, system_prompt=system_prompt)

                if self._stopped:
                    logger.info("[RootAgent] 已停止")
                    self._sync_to_session_manager()
                    self.session_manager.save()
                    break

                # Add assistant response
                self.add_message("assistant", response.content_as_dicts())

                # Check if there are tool calls
                tool_call_blocks = response.get_tool_calls()

                if not tool_call_blocks:
                    # No tool calls - conversation turn complete
                    self._sync_to_session_manager()
                    self.session_manager.save()
                    break

                # Process tool calls
                wait_for_user = False
                for block in tool_call_blocks:
                    if self._stopped:
                        break
                    input_data = block.parse_arguments()
                    result = self._execute_tool(block.name, input_data, block.id)
                    if result.get("_wait_for_user"):
                        wait_for_user = True
                    self.add_message("user", [result])
                    self._sync_to_session_manager()
                    self.session_manager.save()

                if wait_for_user:
                    logger.info("[RootAgent] Waiting for user input")
                    self._sync_to_session_manager()
                    self.session_manager.save()
                    break

                if self._stopped:
                    logger.info("[RootAgent] 已停止")
                    self._sync_to_session_manager()
                    self.session_manager.save()
                    break
            else:
                logger.warning(f"[RootAgent] 达到最大调用次数 ({self.max_iterations})")
                self._sync_to_session_manager()
                self.session_manager.save()

        finally:
            self._running = False

    def _inject_project_instructions(self) -> None:
        """Inject project instructions from agent.md/CLAUDE.md as first user message.

        Only injects if not already present (idempotent).
        """
        from core.prompts import build_instructions_message, has_instructions_message

        if has_instructions_message(self.messages):
            return

        instr = build_instructions_message(self.cwd)
        if instr:
            self.messages.insert(0, instr)
            logger.info(f"[RootAgent] 已注入项目指令文件 (from {self.cwd})")

    def _sync_to_session_manager(self) -> None:
        """Sync metadata and usage to session_manager.

        Note: messages are shared (same reference), no need to sync them.
        """
        self.session_manager.metadata["updated_at"] = self._get_current_time()
        self.session_manager.metadata["usage"] = self._usage.copy()
        self.session_manager._messages_dirty = True  # Invalidate cache

    def _get_current_time(self) -> str:
        """Get current time as formatted string."""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def switch_session(self, session_id: str) -> None:
        """Switch to a different session.

        Called by web_api.py when user switches sessions.
        """
        # Try to load existing session
        loaded = SessionManager.load_session(session_id, self.sessions_dir)
        if loaded:
            self.session_manager = loaded
            self.current_session_id = session_id
            self._session_id = session_id
            self.session_dir = self.sessions_dir / session_id
            # Update messages reference to point to new session's messages
            self.messages = self.session_manager.messages
            self._usage = self.session_manager.get_usage()
            # Update tools' session_manager reference
            for tool in self.tools:
                tool.session_manager = loaded
            logger.info(f"Switched to session: {session_id}")
        else:
            logger.warning(f"Session not found: {session_id}")

    def reset(self) -> None:
        """Clear conversation history."""
        self.messages.clear()
        self.session_manager.clear()

    def get_usage(self) -> dict[str, int]:
        """Return usage statistics synced with session."""
        self._sync_to_session_manager()
        return self.session_manager.get_usage()

    def compact(self) -> tuple[int, int]:
        """Manually compress conversation history."""
        return self._perform_full_compact(3)

    def cleanup(self) -> None:
        """Clean up resources before exit."""
        self._sync_to_session_manager()
        self.session_manager.save()
        super().close()
