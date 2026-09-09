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
from core.session import SessionManager, generate_short_id
from core.prompts import build_root_prompt
from core.tools import create_tools, get_tool_by_name
from core.tools.shared.approval import (
    APPROVE_LABEL,
    META_KEY,
    REJECT_LABEL,
    ApprovalStore,
    build_approval_question,
)

logger = logging.getLogger(__name__)


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
            max_iterations=config.system.max_iterations,
        )

        self._session_id = self.current_session_id

        # Create LLM client
        self.client = create_llm_client(config.model)

        # IMPORTANT: Share messages list with session_manager (not copy!)
        # This ensures web_api.py operations on session_manager.messages
        # are automatically reflected in self.messages
        self.messages = self.session_manager.messages
        self._usage = self.session_manager.get_usage()

        # 会话级高风险命令审批存储（内存，不持久化），根/子代理共享
        self.approval_store = ApprovalStore()

        # Build tools
        self._on_subagent_start: Callable[[str, str], None] | None = None
        self._on_subagent_complete: Callable[[str], None] | None = None
        self._streaming = True
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
            approval_store=self.approval_store,
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
            subagent_tool.on_subagent_complete = lambda exec_id: (
                self._on_subagent_complete(exec_id)
                if self._on_subagent_complete else None
            )

    def reload_config(self) -> None:
        """Reload config from disk and recreate LLM client."""
        from core.config import load_config
        try:
            new_config = load_config()
            self.max_iterations = new_config.system.max_iterations
            # 先创建新客户端，成功后再替换并关闭旧的；
            # 否则创建失败后 self.client 指向已关闭的客户端，后续调用全部失败
            new_client = create_llm_client(new_config.model)
            old_client = self.client
            self.client = new_client
            self.config = new_config
            old_client.close()
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
        on_subagent_complete: Callable[[str], None] | None = None,
        streaming: bool = True,
    ) -> None:
        """Run one turn of the agent loop."""
        self._stopped = False
        self._running = True
        self._streaming = streaming
        self._on_text = on_text
        self._on_thinking = on_thinking
        self._on_tool_call = on_tool_call
        self._on_tool_result = on_tool_result
        self._on_subagent_start = on_subagent_start
        self._on_subagent_complete = on_subagent_complete

        try:
            # Save previous turn
            self._sync_to_session_manager()
            self.session_manager.save()

            # Add user message
            self.add_message("user", user_input)

            self._agent_loop()
        finally:
            self._running = False

    def _handle_approval_required(self, approval: dict) -> None:
        """合成 ask_user 卡询问用户是否批准高风险命令，随后暂停循环等待回答。

        在批处理所有工具结果之后调用，保证消息配对正确：
        [assistant tool_use...] → [tool_result...] → [assistant ask_user tool_use] → [user ask_user 占位]
        """
        self.approval_store.set_pending(approval)

        ask_id = generate_short_id()
        ask_input = {
            "questions": [
                {
                    "question": build_approval_question(approval),
                    "header": "命令批准",
                    "options": [
                        {"label": APPROVE_LABEL, "description": "批准后本会话内执行相同命令（含委派给子代理）不再询问。"},
                        {"label": REJECT_LABEL, "description": "拒绝执行该命令。"},
                    ],
                }
            ]
        }
        # 手动补 assistant tool_use 块，避免悬挂 tool_result（否则 API 400 或触发 _pad_dangling_tool_results）
        self.add_message("assistant", [{"type": "tool_use", "id": ask_id, "name": "ask_user", "input": ask_input}])
        self.add_message("user", [self._execute_tool("ask_user", ask_input, ask_id)])
        self._sync_to_session_manager()
        self.session_manager.save()

    def resume_after_ask_user(
        self,
        on_text: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict, str], None] | None = None,
        on_tool_result: Callable[[str, str, bool, str], None] | None = None,
        on_subagent_start: Callable[[str, str], None] | None = None,
        on_subagent_complete: Callable[[str], None] | None = None,
    ) -> None:
        """Resume agent loop after ask_user tool result has been injected."""
        self._stopped = False
        self._running = True
        self._on_text = on_text
        self._on_thinking = on_thinking
        self._on_tool_call = on_tool_call
        self._on_tool_result = on_tool_result
        self._on_subagent_start = on_subagent_start
        self._on_subagent_complete = on_subagent_complete

        try:
            self._sync_to_session_manager()
            self.session_manager.save()
            self._agent_loop()
        finally:
            self._running = False

    def resume_loop(
        self,
        on_text: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict, str], None] | None = None,
        on_tool_result: Callable[[str, str, bool, str], None] | None = None,
        on_subagent_start: Callable[[str, str], None] | None = None,
        on_subagent_complete: Callable[[str], None] | None = None,
    ) -> None:
        """Resume agent loop after a subagent (or other placeholder) completes."""
        self._stopped = False
        self._running = True
        self._on_text = on_text
        self._on_thinking = on_thinking
        self._on_tool_call = on_tool_call
        self._on_tool_result = on_tool_result
        self._on_subagent_start = on_subagent_start
        self._on_subagent_complete = on_subagent_complete

        try:
            self._sync_to_session_manager()
            self.session_manager.save()
            self._agent_loop()
        finally:
            self._running = False

    def _agent_loop(self) -> None:
        """Shared agent loop body (called by run/resume_after_ask_user/resume_loop)."""
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
            response = self._call_llm(streaming=getattr(self, '_streaming', True), system_prompt=system_prompt)

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
            wait_for_external = False
            external_already = False  # 本批已有非审批占位（模型自发的 ask_user/subagent）
            approval = None  # 本批首个需用户批准的高风险命令
            for block in tool_call_blocks:
                if self._stopped:
                    break
                # Parse arguments from raw JSON string to dict at execution time
                input_data = block.parse_arguments()
                result = self._execute_tool(block.name, input_data, block.id)
                placeholder = result.get("_meta", {}).get("completed") is False
                # 高风险命令需用户批准：降级为错误提示，统一在批处理完后合成 ask_user 卡
                if META_KEY in result.get("_meta", {}):
                    if approval is None:
                        approval = result["_meta"][META_KEY]
                        result["is_error"] = True
                        result["content"] = "该命令需要用户批准，正在询问用户..."
                    else:
                        # 同批多个需批准命令：只询问第一条，其余保持拒绝
                        result["is_error"] = True
                        result["content"] = "该命令需要用户批准，本批仅询问一条，请稍后重试。"
                    result["_meta"].pop(META_KEY, None)
                    result["_meta"].pop("completed", None)
                elif placeholder:
                    # 模型自发的 ask_user/subagent 占位：正常等待，不叠加审批卡
                    wait_for_external = True
                    external_already = True
                # Add tool result to messages
                self.add_message("user", [result])
                # Sync to session manager
                self._sync_to_session_manager()
                self.session_manager.save()

            # 合成 ask_user 卡询问用户是否批准（放在所有工具结果之后，保持消息配对正确；
            # 本批已有模型自发的占位时不合成，避免与 pending 单槽冲突）
            if approval and not external_already and not self._stopped:
                self._handle_approval_required(approval)
                wait_for_external = True

            if wait_for_external:
                # Exit loop to wait for user input or subagent completion
                logger.info("[RootAgent] Waiting for external input (user or subagent)")
                self._sync_to_session_manager()
                self.session_manager.save()
                break

            if self._stopped:
                logger.info("[RootAgent] 已停止")
                # 中途停止可能留下未回应的 tool_use，补占位避免下次调用 400
                self._pad_dangling_tool_results()
                self._sync_to_session_manager()
                self.session_manager.save()
                break
        else:
            logger.warning(f"[RootAgent] 达到最大调用次数 ({self.max_iterations})")
            self._sync_to_session_manager()
            self.session_manager.save()
            if self._on_text:
                self._on_text(f"\n\n[已达到最大工具调用次数限制 ({self.max_iterations})，请继续提问以继续对话]")

    def _get_messages_with_header(self) -> list[dict]:
        """Get valid messages with dynamic project instructions injection.

        Project instructions (CLAUDE.md/agent.md) are re-read from disk on each
        LLM call and prepended to the message list. They are NOT persisted in
        self.messages (session file stays clean).

        If the first real message is also a user message with string content,
        the instructions are merged into it to avoid consecutive same-role
        messages (required by OpenAI API and Bedrock).

        Returns messages with _meta intact; _meta is stripped later
        by _strip_meta_from_messages() after _resolve_tool_results() runs.
        """
        from core.prompts import build_instructions_message

        messages = self.get_valid_messages(strip_meta=False)
        instr = build_instructions_message(self.cwd)
        if not instr:
            return messages

        if messages and messages[0].get("role") == "user":
            first_content = messages[0].get("content", "")
            if isinstance(first_content, str):
                # Merge instructions into the first user message
                merged = {
                    "role": "user",
                    "content": instr["content"] + "\n\n" + first_content,
                }
                return [merged] + messages[1:]

        # No user messages or first message has non-string content (blocks)
        return [instr] + messages

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
