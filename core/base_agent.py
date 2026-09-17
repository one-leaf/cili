"""BaseAgent - unified agent loop shared by the single Agent class.

Provides shared infrastructure for agent execution:
- Message management (self.messages, delegated to AgentContext)
- Tool execution / LLM calling / compression (delegated to Runner)

`core.agent.Agent` subclasses this and customizes behavior via role JSON
(tools, system prompt blocks, user layers) instead of per-class overrides.

执行逻辑（LLM 调用/工具执行/压缩）已抽至 core.agent_runtime.Runner；
本类保留同名薄转发方法，web 层与测试对 agent 方法的调用接口不变。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from core.agent_runtime.context import AgentContext
from core.agent_runtime.runner import Runner, RETRY_CLEAR_SENTINEL
from core.config import Config
from core.llm import LLMClient, LLMResponse, Message
from core.tools.base import Tool

# 迭代上限默认值（Loop 层在 Step 4 接管计数）
_MAX_ITERATIONS = 50


class BaseAgent:
    """Base class for agents with unified execution loop."""

    def __init__(
        self,
        config: Config,
        workspace_uuid: str = "",
        cwd: str = "",
        session_dir: Path | None = None,
        stop_check: Callable[[], bool] | None = None,
        max_iterations: int = _MAX_ITERATIONS,
    ):
        """Initialize base agent.

        Args:
            config: Configuration object
            workspace_uuid: Workspace UUID for tool creation
            cwd: Working directory
            session_dir: Directory for saving messages (None = no persistence)
            stop_check: Callable that returns True when parent agent stopped
            max_iterations: Maximum tool call iterations
        """
        self.config = config
        self.model = config.model
        self.workspace_uuid = workspace_uuid
        self.cwd = cwd or os.getcwd()
        self.session_dir = session_dir
        self.stop_check = stop_check
        self.max_iterations = max_iterations

        # 消息状态层：先建 context（Agent 的 _init_interactive 已在此前注入 session_manager），
        # messages/_usage 属性转发到 context，保证后续所有直接赋值/读取一致
        self.context = AgentContext(
            messages=[],
            session_manager=getattr(self, "session_manager", None),
            session_dir=session_dir,
        )

        # 执行层：持本 agent 引用，压缩/LLM 调用/工具执行在回合运行时访问共享状态
        self.runner = Runner(self)

        # Message management (property → context.messages)
        self.messages: list[dict] = []

        # LLM client
        self.client: LLMClient | None = None

        # Tools
        self.tools: list[Tool] = []
        self.tool_schemas: list[dict] = []

        # Execution tracking
        self._stopped = False
        self._running = False

        # Usage tracking
        self._usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "api_calls": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }

        # Callbacks (set by run())
        self._on_text: Callable[[str], None] | None = None
        self._on_thinking: Callable[[str], None] | None = None
        self._on_tool_call: Callable[[str, dict, str], None] | None = None
        self._on_tool_result: Callable[[str, str, bool, str], None] | None = None
        # 工具实时输出增量：(tool_name, content, written_bytes, tool_use_id)
        self._on_tool_output: Callable[[str, str, int, str], None] | None = None

        # Session ID for LLM routing — the unified Agent sets this before
        # super().__init__ (interactive: current session; autonomous: exec_id).
        if not getattr(self, "_session_id", None):
            self._session_id: str = ""

    # ========== Message Management ==========

    @property
    def messages(self) -> list[dict]:
        """消息列表（转发到 context.messages，保持与 session_manager 同一引用）。"""
        return self.context.messages

    @messages.setter
    def messages(self, value: list[dict]) -> None:
        self.context.messages = value

    @property
    def _usage(self) -> dict[str, int]:
        """usage 统计（转发到 context._usage）。"""
        return self.context._usage

    @_usage.setter
    def _usage(self, value: dict[str, int]) -> None:
        self.context._usage = value

    @staticmethod
    def _convert_to_message_objects(messages: list[dict]) -> list[Message]:
        """Convert dict-based messages to Message objects (delegated to Runner)."""
        return Runner._convert_to_message_objects(messages)

    def add_message(self, role: str, content: Any, meta: dict | None = None) -> None:
        """Add a message to internal message list (delegated to AgentContext)."""
        self.context.add_message(role, content, meta)

    def save_messages(self, metadata: dict | None = None) -> None:
        """Save messages (delegated to AgentContext)."""
        self.context.save_messages(metadata, session_id=getattr(self, "_session_id", ""))

    def load_messages(self) -> bool:
        """Load messages from session_dir/index.json (delegated to AgentContext)."""
        return self.context.load_messages()

    def invalidate_all_messages(self) -> int:
        """Mark all messages as invalid (_meta.valid=False). Returns count."""
        return self.context.invalidate_all_messages()

    def get_valid_messages(self, strip_meta: bool = True) -> list[dict]:
        """Get messages with _meta.valid=False filtered out (delegated to AgentContext)."""
        return self.context.get_valid_messages(strip_meta=strip_meta)

    # ========== Tool Execution ==========

    @staticmethod
    def _safe_output_filename(tool_use_id: str) -> str:
        """Generate a safe output filename for a tool_use_id (delegated to Runner)."""
        return Runner._safe_output_filename(tool_use_id)

    def _execute_tool(self, name: str, input_data: dict, tool_use_id: str) -> dict:
        """Execute a tool and return result metadata (delegated to Runner)."""
        return self.runner.execute_tool(name, input_data, tool_use_id)

    def _get_tool_by_name(self, name: str) -> Tool | None:
        """Find tool by name (delegated to Runner)."""
        return self.runner._get_tool_by_name(name)

    def _resolve_tool_results(self, messages: list[dict]) -> list[dict]:
        """Read tool output from external files before sending to LLM (delegated to Runner)."""
        return self.runner._resolve_tool_results(messages)

    def _strip_meta_from_messages(self, messages: list[dict]) -> list[dict]:
        """Strip internal _meta fields from messages (delegated to Runner)."""
        return self.runner._strip_meta_from_messages(messages)

    # ========== Compression ==========

    def _invalidate_message_cache(self) -> None:
        """压缩等原地修改 self.messages 后，失效 session_manager 的 valid 缓存。"""
        self.context.invalidate_message_cache()

    def _check_and_compress(self) -> None:
        """3-layer compression before LLM call (delegated to Runner)."""
        self.runner._check_and_compress()

    def _perform_full_compact(self, keep_user_messages: int) -> tuple[int, int]:
        """Full auto compact: summarize old messages (delegated to Runner)."""
        return self.runner._perform_full_compact(keep_user_messages)

    @staticmethod
    def _find_split_by_user_messages(messages: list[dict], keep_user_count: int) -> int:
        """Find split point keeping last N user messages (delegated to AgentContext)."""
        return AgentContext.find_split_by_user_messages(messages, keep_user_count)

    def _summarize_messages(self, messages: list[dict]) -> str:
        """Use LLM to summarize messages (delegated to Runner)."""
        return self.runner._summarize_messages(messages)

    def _mark_old_tool_calls_invalid(self, keep_recent_rounds: int = 5) -> int:
        """Mark old tool calls as invalid to reduce body size (delegated to Runner)."""
        return self.runner._mark_old_tool_calls_invalid(keep_recent_rounds)

    def _mark_old_images_invalid(self, keep_recent: int = 5) -> int:
        """Replace old tool_result images with text placeholders (delegated to Runner)."""
        return self.runner._mark_old_images_invalid(keep_recent)

    def _mark_all_images_invalid(self) -> None:
        """Replace all tool_result images with text placeholders for 413 retry (delegated to Runner)."""
        self.runner._mark_all_images_invalid()

    # ========== Token Counting & Body Size ==========

    @staticmethod
    def iter_content_blocks(messages: list[dict]):
        """Yield (block_type, data) for each content element (delegated to Runner)."""
        return Runner.iter_content_blocks(messages)

    def _count_messages_tokens(self, messages: list[dict]) -> int:
        """Count total tokens in messages (delegated to AgentContext)."""
        return self.context.count_messages_tokens(messages)

    def _estimate_request_body_size(self, messages: list[dict]) -> int:
        """Estimate JSON request body size in bytes (delegated to Runner)."""
        return self.runner._estimate_request_body_size(messages)

    def _strip_images_from_messages(self, messages: list[dict]) -> list[dict]:
        """Strip images from messages for non-multimodal models (delegated to Runner)."""
        return self.runner._strip_images_from_messages(messages)

    def _get_messages_with_header(self) -> list[dict]:
        """Get valid messages for LLM call (delegated to AgentContext).

        Returns messages with _meta intact; _meta is stripped later
        by _strip_meta_from_messages() after _resolve_tool_results() runs.
        """
        return self.context.get_messages_with_header()

    def _pad_dangling_tool_results(self) -> None:
        """为悬挂的 tool_use 补充占位 tool_result（原地修改 messages）。"""
        self.context.pad_dangling_tool_results()

    # ========== LLM Calling ==========

    def _call_llm(self, streaming: bool = False, system_prompt: str = "") -> LLMResponse:
        """Call LLM with optional streaming.

        转发到 Runner.run_round：每轮先压缩再调用 LLM（压缩入口收敛在此，
        loop 不再显式调用 _check_and_compress）。测试 patch 本方法时整体替换
        回合入口，压缩与真实调用均被跳过。

        Raises:
            RuntimeError: LLM 调用最终失败（内部重试耗尽后抛出，
                消息为 format_llm_error 生成的友好文本）。
                调用方需自行捕获处理——不抛会导致 Agent 把错误
                误判为正常完成。用户停止不视为错误（返回 stop_reason="stopped"）。
        """
        return self.runner.run_round(streaming=streaming, system_prompt=system_prompt)

    def _prepare_messages_for_llm(
        self,
        *,
        pad_dangling: bool = True,
        resolve_results: bool = True,
    ) -> list[Message]:
        """Build Message objects for the LLM API from self.messages (delegated to Runner)."""
        return self.runner._prepare_messages_for_llm(
            pad_dangling=pad_dangling,
            resolve_results=resolve_results,
        )

    def _call_llm_non_streaming(self, system_prompt: str) -> LLMResponse:
        """Non-streaming LLM call (delegated to Runner)."""
        return self.runner._call_llm_non_streaming(system_prompt)

    def _call_llm_streaming(self, system_prompt: str) -> LLMResponse:
        """Streaming LLM call. Think content passes through as-is (delegated to Runner)."""
        return self.runner._call_llm_streaming(system_prompt)

    @staticmethod
    def _is_413_error(e: Exception) -> bool:
        """Check if exception is a 413 Entity Too Large error (delegated to Runner)."""
        return Runner._is_413_error(e)

    # ========== Usage Tracking ==========

    def _update_usage(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        api_calls: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> None:
        """Update usage statistics (delegated to AgentContext)."""
        self.context.update_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            api_calls=api_calls,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )

    def get_usage(self) -> dict[str, int]:
        """Get accumulated usage statistics (delegated to AgentContext)."""
        return self.context.get_usage()

    # ========== Lifecycle ==========

    def stop(self) -> None:
        """Signal the agent to stop."""
        self._stopped = True

    def is_running(self) -> bool:
        """Check if agent is running."""
        return self._running

    def close(self) -> None:
        """Clean up resources."""
        for tool in self.tools:
            if hasattr(tool, 'close'):
                try:
                    tool.close()
                except Exception:
                    pass
        if self.client:
            self.client.close()
