"""BaseAgent - unified agent loop shared by the single Agent class.

Provides shared infrastructure for agent execution:
- Message management (self.messages)
- Tool execution with external file storage
- 3-layer compression (microcompact → full compact → emergency body size)
- LLM calling with 413 retry (thinking content passes through)
- Usage tracking

`core.agent.Agent` subclasses this and customizes behavior via role JSON
(tools, system prompt blocks, user layers) instead of per-class overrides.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from core.config import Config, ModelConfig
from core.llm import LLMClient, LLMResponse, format_llm_error, Message, TextBlock, UsageData
from core.session import generate_short_id
from core.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

# Compression constants
KEEP_USER_MESSAGES = 3
_LARGE_OUTPUT_THRESHOLD = 10_000
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

        # Message management
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

        # Session ID for LLM routing — the unified Agent sets this before
        # super().__init__ (interactive: current session; autonomous: exec_id).
        if not getattr(self, "_session_id", None):
            self._session_id: str = ""

    # ========== Message Management ==========

    def _convert_to_message_objects(self, messages: list[dict]) -> list[Message]:
        """Convert dict-based messages to Message objects.

        Internal helper for passing messages to LLMClient.
        """
        return [Message.from_dict(msg) for msg in messages]

    def add_message(self, role: str, content: Any, meta: dict | None = None) -> None:
        """Add a message to internal message list.

        New format: no longer adds block-level _valid.
        Message-level _meta.valid is set by other logic when needed.

        Args:
            role: Message role (user/assistant/system)
            content: Message content
            meta: Optional metadata dict (e.g. {"pinned": True} to prevent compression)
        """
        meta = dict(meta) if meta else {}
        if "id" not in meta:
            meta["id"] = generate_short_id()
        msg = {"role": role, "content": content, "_meta": meta}
        self.messages.append(msg)

    def save_messages(self, metadata: dict | None = None) -> None:
        """Save messages to session_dir/index.json.

        Merges with the existing file so name/metadata written by
        SessionManager (or Agent progress logs) are preserved — this save
        only updates the messages and the caller-provided metadata.

        Args:
            metadata: Optional metadata to include in the file
        """
        if not self.session_dir:
            return

        self.session_dir.mkdir(parents=True, exist_ok=True)
        session_file = self.session_dir / "index.json"

        # Preserve existing top-level fields from the file on disk
        existing: dict = {}
        if session_file.exists():
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = {}

        data = {
            "session_id": self._session_id or self.session_dir.name,
            "messages": self.messages,
            "name": existing.get("name", ""),
            "metadata": metadata if metadata is not None else existing.get("metadata", {}),
        }

        try:
            temp_file = session_file.with_suffix(".json.tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp_file.replace(session_file)
        except Exception as e:
            logger.error(f"Failed to save messages: {e}")

    def load_messages(self) -> bool:
        """Load messages from session_dir/index.json.

        Returns:
            True if loaded successfully, False if file doesn't exist
        """
        if not self.session_dir:
            return False

        session_file = self.session_dir / "index.json"
        if not session_file.exists():
            return False

        try:
            with open(session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.messages = data.get("messages", [])
            return True
        except Exception as e:
            logger.error(f"Failed to load messages: {e}")
            return False

    def invalidate_all_messages(self) -> int:
        """Mark all messages as invalid (_meta.valid=False). Returns count."""
        count = 0
        for msg in self.messages:
            if msg.get("_meta", {}).get("valid") is not False:
                msg.setdefault("_meta", {})["valid"] = False
                count += 1
        return count

    def get_valid_messages(self, strip_meta: bool = True) -> list[dict]:
        """Get messages with _meta.valid=False filtered out.

        Used before sending to LLM API. Thinking blocks are preserved because
        Anthropic API requires them in subsequent messages for multi-turn context.
        Note: _meta.compacted is preserved here, filtered later during serialization.

        Args:
            strip_meta: If True (default), strip internal _meta fields before API call.
                        If False, keep _meta intact for intermediate processing
                        (e.g., _resolve_tool_results needs _meta.output_path).
        """
        INTERNAL_META = {"valid", "compacted", "output_path", "file_size", "truncated", "tool_name", "multimodal", "completed", "answered", "exec_id"}
        result = []

        for msg in self.messages:
            # Skip messages marked as invalid
            meta = msg.get("_meta", {})
            if meta.get("valid") is False:
                continue

            role = msg.get("role")
            content = msg.get("content", "")

            # String content
            if not isinstance(content, list):
                if strip_meta:
                    clean_msg = {"role": role, "content": content}
                    # Strip internal _meta fields, keep other _meta if exists
                    if meta:
                        stripped_meta = {k: v for k, v in meta.items() if k not in INTERNAL_META}
                        if stripped_meta:
                            clean_msg["_meta"] = stripped_meta
                else:
                    # Keep _meta intact (for _resolve_tool_results etc.)
                    clean_msg = dict(msg)
                result.append(clean_msg)
                continue

            # List content: keep all blocks
            clean_blocks = []
            for block in content:
                clean_block = dict(block)
                if strip_meta and "_meta" in clean_block:
                    # Strip block-level internal _meta fields before sending to API
                    stripped_block_meta = {k: v for k, v in clean_block["_meta"].items() if k not in INTERNAL_META}
                    if stripped_block_meta:
                        clean_block["_meta"] = stripped_block_meta
                    else:
                        del clean_block["_meta"]
                clean_blocks.append(clean_block)

            if clean_blocks:
                clean_msg = {"role": role, "content": clean_blocks}
                if strip_meta:
                    # Strip internal _meta fields, keep other _meta if exists
                    if meta:
                        stripped_meta = {k: v for k, v in meta.items() if k not in INTERNAL_META}
                        if stripped_meta:
                            clean_msg["_meta"] = stripped_meta
                else:
                    # Keep message-level _meta intact
                    if meta:
                        clean_msg["_meta"] = dict(meta)
                result.append(clean_msg)

        return result

    # ========== Tool Execution ==========

    def _execute_tool(self, name: str, input_data: dict, tool_use_id: str) -> dict:
        """Execute a tool and return result metadata.

        Tool output is saved to external file {session_dir}/{tool_use_id}.txt only when needed:
        - Streaming tools (bash, python) for real-time frontend polling
        - Large outputs (exceeds threshold, will be truncated)
        - Multimodal content (images, saved as .json)
        Returns result dict with _meta containing internal fields.
        Uses Anthropic format: tool_use_id (not tool_call_id), is_error (not _is_error).
        """
        tool = self._get_tool_by_name(name)

        if tool is None:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": f"Error: unknown tool '{name}'",
                "is_error": True,
                "_meta": {"tool_name": name},
            }

        logger.debug(f"[工具调用] {name}")

        # Tools that need external file for streaming (frontend polling)
        _STREAMING_TOOLS = {"bash", "python"}
        # Placeholder tools: output goes directly in content, no external file
        _PLACEHOLDER_TOOLS = {"ask_user", "agent"}

        # Setup output file path
        output_filename = f"{tool_use_id}.txt" if tool_use_id else ""
        output_file_path = ""
        if self.session_dir and output_filename and name not in _PLACEHOLDER_TOOLS:
            output_file_path = str(self.session_dir / output_filename)
            tool.output_file = output_file_path

            # Create empty file only for streaming tools (signals tool is running)
            if name in _STREAMING_TOOLS:
                try:
                    with open(output_file_path, "w", encoding="utf-8") as f:
                        f.write("")
                except Exception:
                    pass

        # Notify callback
        if self._on_tool_call:
            self._on_tool_call(name, input_data, tool_use_id)

        # Execute tool
        start_time = time.perf_counter()
        result = ToolResult("Error: tool execution was interrupted", error=True)

        try:
            input_data = tool.coerce_input(input_data)
            # coerce_input 可能返回 ToolResult（参数校验失败）
            if isinstance(input_data, ToolResult):
                result = input_data
            else:
                result = tool.execute(**input_data)
        except Exception as e:
            import traceback
            logger.error(f"Tool '{name}' raised exception:\n{traceback.format_exc()}")
            result = ToolResult(f"Error executing tool: {e}", error=True)
        finally:
            # Only save external file for streaming tools
            # Non-streaming tools save lazily based on output size (see below)
            if name in _STREAMING_TOOLS:
                tool.save_output_to_file(result)
            # 更新 _output_path 为实际保存的文件路径（可能是 .json）
            output_filename = os.path.basename(tool.output_file) if tool.output_file else output_filename
            tool.output_file = None

        elapsed = time.perf_counter() - start_time

        # Log result
        output_preview = result.output
        if len(output_preview) > 500:
            output_preview = output_preview[:500] + f"\n... ({len(result.output)} chars total)"
        status = "失败" if result.error else "完成"
        logger.debug(f"[工具结果] {name} {status} ({elapsed:.2f}s)")

        # Notify callback
        if self._on_tool_result:
            self._on_tool_result(name, output_preview, result.error, tool_use_id)

        # Build _meta with internal fields
        file_size = len(result.output.encode('utf-8', errors='replace'))
        truncated = file_size > _LARGE_OUTPUT_THRESHOLD

        # Check if multimodal (has image blocks)
        from core.llm.types import ImageBlock
        is_multimodal = any(isinstance(block, ImageBlock) for block in result.blocks)

        # Only save external file when needed (streaming already saved, skip)
        needs_external_file = truncated or is_multimodal
        if needs_external_file and name not in _STREAMING_TOOLS and name not in _PLACEHOLDER_TOOLS:
            # Set output_file for save_output_to_file
            tool.output_file = str(self.session_dir / output_filename) if self.session_dir else None
            if tool.output_file:
                tool.save_output_to_file(result)
                # Update filename if changed to .json (multimodal)
                output_filename = os.path.basename(tool.output_file)
                tool.output_file = None

        meta = {
            "tool_name": name,
        }
        if needs_external_file:
            meta["output_path"] = output_filename
            meta["file_size"] = file_size
            meta["truncated"] = truncated
        # Add any extra meta from tool result
        if result.meta:
            meta.update(result.meta)

        # Build result dict (Anthropic format)
        result_dict = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": result.output if not truncated else None,
            "is_error": result.error,
        }

        # Add completed=False to block-level _meta for placeholder tools (ask_user, agent)
        if result.completed is False:
            if "_meta" not in result_dict:
                result_dict["_meta"] = {}
            result_dict["_meta"]["completed"] = False

        # Add _meta if it has content
        if meta:
            if "_meta" not in result_dict:
                result_dict["_meta"] = {}
            result_dict["_meta"].update(meta)

        # Clean up orphaned external file: streaming tools (bash/python) always
        # create a .txt file for frontend polling, but when content is inlined
        # (not truncated, not multimodal) the file becomes unnecessary.
        # _resolve_tool_results skips blocks with inline content, so it never
        # needs this file. Delete it now to avoid orphaned files on disk.
        if not needs_external_file and self.session_dir and output_filename:
            orphan_path = self.session_dir / output_filename
            try:
                if orphan_path.exists():
                    orphan_path.unlink()
            except Exception:
                pass

        return result_dict

    def _get_tool_by_name(self, name: str) -> Tool | None:
        """Find tool by name."""
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def _resolve_tool_results(self, messages: list[dict]) -> list[dict]:
        """Read tool output from external files before sending to LLM.

        Session only stores metadata. This method reads actual content
        from {session_dir}/{tool_use_id}.txt or .json files.

        支持两种格式：
        - .txt: 纯文本
        - .json: 多模态内容（包含图片和文本块）

        Uses new _meta format: _meta.output_path, _meta.compacted, etc.
        Backward compatible with old _output_path, _compacted format.
        """
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                # Skip if already has content (e.g., error message)
                if block.get("content"):
                    continue

                # 从 block 级别的 _meta 读取内部元数据
                block_meta = block.get("_meta", {})
                compacted = block_meta.get("compacted", False)
                output_path = block_meta.get("output_path", "")
                file_size = block_meta.get("file_size", 0)
                truncated = block_meta.get("truncated", False)

                # Handle compacted marker - include filename for read_tool_result
                if compacted:
                    if output_path:
                        # Extract tool_use_id from filename (remove extension)
                        tool_use_id = output_path.replace(".txt", "").replace(".json", "")
                        block["content"] = f"[Compacted: use `read_tool_result` tool with tool_use_id=\"{tool_use_id}\" to retrieve original content]"
                    else:
                        block["content"] = "[Compacted: tool_use_id unknown]"
                    continue

                # Read from external file
                if not output_path or not self.session_dir:
                    block["content"] = "[工具输出文件路径缺失]"
                    continue

                file_path = self.session_dir / output_path
                if not file_path.exists():
                    block["content"] = "[工具正在执行中...]"
                    continue

                try:
                    # 检查是否是 json 文件（多模态内容）
                    if str(file_path).endswith(".json"):
                        import json as json_module
                        with open(file_path, "r", encoding="utf-8") as f:
                            data = json_module.load(f)

                        if data.get("type") == "multimodal":
                            # 多模态内容：返回 content blocks 列表
                            from core.llm.types import block_from_dict
                            content_blocks = []
                            for block_data in data.get("blocks", []):
                                try:
                                    content_blocks.append(block_from_dict(block_data))
                                except Exception:
                                    # 无法解析的块，跳过
                                    pass
                            # 将 content blocks 转为 dict 列表
                            block["content"] = [
                                cb.to_dict() if hasattr(cb, 'to_dict') else cb
                                for cb in content_blocks
                            ]
                        else:
                            block["content"] = "[工具输出格式错误]"
                    else:
                        # 纯文本文件
                        file_content = file_path.read_text(encoding='utf-8', errors='replace')

                        # Empty file = tool still running
                        if not file_content:
                            block["content"] = "[工具正在执行中...]"
                            continue

                        # Truncate + guide
                        if truncated:
                            truncated_content = Tool.truncate_middle(file_content, 8000)
                            guide = (
                                f"\n\n---\n"
                                f"[提示] 工具输出过长（{file_size:,} 字符），已截断显示。"
                                f"完整输出保存在文件: {file_path}。"
                                f"如需查看完整内容，请使用 read 工具分批读取该文件。"
                            )
                            block["content"] = truncated_content + guide
                        else:
                            block["content"] = Tool.truncate_result(file_content, Tool.MAX_TOOL_RESULT_SIZE_CHARS)

                except Exception as e:
                    block["content"] = f"[读取工具输出失败: {e}]"
                    continue

        return messages

    def _strip_meta_from_messages(self, messages: list[dict]) -> list[dict]:
        """Strip internal _meta fields from messages before sending to LLM API.

        Called after _resolve_tool_results() has populated block content from
        external files. The _meta fields (output_path, file_size, etc.) are
        only needed for that resolution step and must not reach the API.

        Modifies blocks in-place (same pattern as _strip_images_from_messages).
        """
        INTERNAL_META = frozenset({"valid", "compacted", "output_path", "file_size", "truncated", "tool_name", "multimodal", "completed", "answered", "exec_id"})
        for msg in messages:
            content = msg.get("content", "")
            if not isinstance(content, list):
                # Strip message-level _meta
                if "_meta" in msg:
                    stripped = {k: v for k, v in msg["_meta"].items() if k not in INTERNAL_META}
                    if stripped:
                        msg["_meta"] = stripped
                    else:
                        del msg["_meta"]
                continue
            for block in content:
                if "_meta" in block:
                    stripped = {k: v for k, v in block["_meta"].items() if k not in INTERNAL_META}
                    if stripped:
                        block["_meta"] = stripped
                    else:
                        del block["_meta"]
            # Strip message-level _meta too
            if "_meta" in msg:
                stripped = {k: v for k, v in msg["_meta"].items() if k not in INTERNAL_META}
                if stripped:
                    msg["_meta"] = stripped
                else:
                    del msg["_meta"]
        return messages

    # ========== Compression ==========

    def _check_and_compress(self) -> None:
        """3-layer compression before LLM call.

        Layer 1: Microcompact (replace old tool results with placeholder)
        Layer 2: Full compact (LLM summary when tokens > 80% threshold)
        Layer 3: Emergency body size (mark old tool calls/images as invalid)
        """
        from core.compression import microcompact_tool_results, count_messages_tokens

        MAX_TOKENS = self.model.max_context_tokens
        MICROCOMPACT_KEEP_RECENT = 6
        FULL_COMPACT_TOKEN_RATIO = 0.80
        MAX_BODY_SIZE = 3_000_000

        # Layer 1: Microcompact
        saved = microcompact_tool_results(self.messages, keep_recent=MICROCOMPACT_KEEP_RECENT)
        if saved > 0:
            logger.debug(f"[Microcompact] 压缩旧工具结果，节省约 {saved:,} 字节")

        # Calculate tokens
        messages = self._get_messages_with_header()
        total_tokens = self._count_messages_tokens(messages)

        # Layer 2: Full compact
        full_compact_threshold = int(MAX_TOKENS * FULL_COMPACT_TOKEN_RATIO)

        if total_tokens > full_compact_threshold:
            logger.info(
                f"[上下文] token 超过阈值 ({total_tokens:,} > {full_compact_threshold:,})，"
                f"执行完整压缩"
            )
            try:
                self._perform_full_compact(KEEP_USER_MESSAGES)
            except Exception as e:
                logger.warning(f"[上下文] 完整压缩失败: {e}")

        # Layer 3: Emergency body size
        messages = self._get_messages_with_header()
        body_size = self._estimate_request_body_size(messages)
        logger.debug(f"[上下文] 估算请求体大小: {body_size:,} 字节 ({body_size/1024/1024:.2f} MB)")

        if body_size > MAX_BODY_SIZE:
            # 详细分析大小分布
            text_size = 0
            image_size = 0
            tool_size = 0
            for btype, data in self.iter_content_blocks(messages):
                if btype in ("text", "tool_result_text", "tool_result_str"):
                    text_size += len(data.encode('utf-8')) if isinstance(data, str) else len(data)
                elif btype == "tool_use":
                    tool_size += len(json.dumps(data.get("input", {}), ensure_ascii=False).encode('utf-8'))
                elif btype == "tool_result_image":
                    image_size += len(data)
            logger.debug(f"[上下文] 大小分布: 文本={text_size:,}B, 图片={image_size:,}B, 工具={tool_size:,}B")

            logger.info("[上下文] 请求体过大，正在标记旧工具调用为无效...")
            saved = self._mark_old_tool_calls_invalid(keep_recent_rounds=3)
            if saved > 0:
                messages = self._get_messages_with_header()
                logger.info(f"[上下文] 工具调用标记完成，节省 {saved} 字节")

            body_size = self._estimate_request_body_size(messages)
            if body_size > MAX_BODY_SIZE:
                logger.info("[上下文] 正在替换旧图片为占位符...")
                saved = self._mark_old_images_invalid(keep_recent=3)
                if saved > 0:
                    logger.info(f"[上下文] 图片替换完成，节省 {saved} 字节")

    def _perform_full_compact(self, keep_user_messages: int) -> tuple[int, int]:
        """Full auto compact: summarize old messages, keep recent user messages.

        Returns (before_tokens, after_tokens) tuple.
        """
        from core.compression import count_messages_tokens

        all_messages = self.messages
        total_tokens = self._count_messages_tokens(self.get_valid_messages())

        # Find split point
        valid_messages = self.get_valid_messages()
        split_idx = self._find_split_by_user_messages(valid_messages, keep_user_messages)
        if split_idx <= 0:
            raise ValueError("Not enough messages to compress")

        # Summarize old messages FIRST — only invalidate them after the summary
        # succeeds. Invalidating before summarizing would permanently hide the
        # old history if the summary LLM call fails.
        old_messages = valid_messages[:split_idx]
        if not old_messages:
            # Nothing to summarize
            return total_tokens, total_tokens

        summary = self._summarize_messages(old_messages)
        if summary.startswith("(摘要生成失败") or summary.startswith("（摘要生成失败"):
            logger.error("摘要生成失败，跳过压缩")
            return total_tokens, total_tokens

        # Mark messages before split as invalid (using _meta.valid).
        # valid_messages 是 get_valid_messages() 的保序拷贝（非引用），
        # 用游标在 all_messages 中按 valid_messages 索引对齐后标记；
        # pinned 消息永不标记失效（任务/检查提示等核心锚点）。
        pinned_positions = {
            pos for pos, msg in enumerate(valid_messages[:split_idx])
            if msg.get("_meta", {}).get("pinned")
        }
        cursor = 0
        for msg in all_messages:
            if cursor >= split_idx:
                break
            if msg.get("_meta", {}).get("valid") is False:
                continue
            if cursor not in pinned_positions:
                if "_meta" not in msg:
                    msg["_meta"] = {}
                msg["_meta"]["valid"] = False
            cursor += 1

        # Add summary messages
        self.add_message(
            "user",
            "[Our previous conversation has been compacted due to context length.]",
        )
        self.add_message("assistant", summary)

        new_tokens = self._count_messages_tokens(self.get_valid_messages())
        logger.info(f"[Full Compact] 完成: {total_tokens:,} → {new_tokens:,} tokens")
        return total_tokens, new_tokens

    def _find_split_by_user_messages(self, messages: list[dict], keep_user_count: int) -> int:
        """Find split point keeping last N user text messages."""
        user_text_indices = []
        for i, msg in enumerate(messages):
            if msg.get("_meta", {}).get("pinned"):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                user_text_indices.append(i)

        if len(user_text_indices) <= keep_user_count:
            return 0

        split_idx = user_text_indices[-keep_user_count]

        # Don't split in middle of tool chain
        while split_idx > 0:
            msg = messages[split_idx]
            role = msg.get("role")
            content = msg.get("content", [])
            is_list = isinstance(content, list)
            if role == "user" and is_list and any(b.get("type") == "tool_result" for b in content):
                split_idx -= 1
                continue
            if role == "assistant" and is_list and any(b.get("type") == "tool_use" for b in content):
                split_idx -= 1
                continue
            break
        return split_idx

    def _summarize_messages(self, messages: list[dict]) -> str:
        """Use LLM to summarize messages."""
        conversation_parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                conversation_parts.append(f"{role}: {content}")
            elif isinstance(content, list):
                texts = []
                for block in content:
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        texts.append(f"[调用工具: {block.get('name', '')}]")
                    elif block.get("type") == "tool_result":
                        texts.append("[工具结果]")
                if texts:
                    conversation_parts.append(f"{role}: {' '.join(texts)}")

        conversation_text = "\n".join(conversation_parts)
        max_chars = min(50000, self.model.max_context_tokens * 2)
        if len(conversation_text) > max_chars:
            conversation_text = conversation_text[:max_chars] + "\n...(内容被截断)"

        summary_prompt = f"""请用中文简洁地总结以下对话的主要内容，包括：
1. 用户的主要需求和目标
2. 已完成的关键操作
3. 当前进展状态
4. 重要的上下文信息

对话内容：
{conversation_text}

请用 200-400 字总结："""

        try:
            response = self.client.chat(
                messages=[Message(role="user", content=summary_prompt)],
                system="你是一个对话总结助手。请用中文简洁地总结对话要点。",
                session_id=self._session_id,
            )
            # Track usage (UsageData object)
            if response.usage:
                self._update_usage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    api_calls=1,
                    cache_read_tokens=response.usage.cache_read_tokens,
                    cache_creation_tokens=response.usage.cache_write_tokens,
                )
            # Use the new get_text() method for typed content blocks
            return response.get_text() or "（摘要生成失败）"
        except Exception as e:
            logger.error("[上下文] 摘要生成失败")
            return "（摘要生成失败，请查看完整历史）"

    def _mark_old_tool_calls_invalid(self, keep_recent_rounds: int = 5) -> int:
        """Mark old tool calls as invalid to reduce body size."""
        saved = 0
        tool_calls = []
        round_number = 0

        for msg in self.messages:
            # Check validity
            meta = msg.get("_meta", {})
            if meta.get("valid") is False:
                continue

            role = msg.get("role")
            content = msg.get("content", [])

            if not isinstance(content, list):
                continue

            if role == "assistant":
                round_number += 1

            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type in ("tool_use", "tool_result"):
                    tool_calls.append({"block": block, "round": round_number, "msg": msg})

        if round_number <= keep_recent_rounds:
            return 0

        for call in tool_calls:
            if call["round"] <= round_number - keep_recent_rounds:
                msg = call["msg"]
                # Check if already marked invalid
                meta = msg.get("_meta", {})
                if meta.get("valid") is False:
                    continue

                block = call["block"]
                content = block.get("input", {}) if block.get("type") == "tool_use" else block.get("content", "")
                size = len(str(content))

                # Mark message-level _meta.valid = False
                if "_meta" not in msg:
                    msg["_meta"] = {}
                msg["_meta"]["valid"] = False
                saved += size

        return saved

    def _mark_old_images_invalid(self, keep_recent: int = 5) -> int:
        """Replace old tool_result images with text placeholders to reduce body size.

        Replaces images in place (instead of invalidating whole messages) so the
        tool_use/tool_result pairing with the preceding assistant message stays
        intact — invalidating only the user message would leave the assistant's
        tool_use dangling and the API would reject the next request with 400.
        Returns the number of image bytes removed.
        """
        saved = 0
        image_refs = []  # (msg_idx, block_idx, sub_idx, data_len)

        for i, msg in enumerate(self.messages):
            # Check validity
            meta = msg.get("_meta", {})
            if meta.get("valid") is False:
                continue
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue

            for block_idx, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                rc = block.get("content", "")
                if not isinstance(rc, list):
                    continue
                for sub_idx, sub in enumerate(rc):
                    if isinstance(sub, dict) and sub.get("type") == "image":
                        data_len = len(sub.get("source", {}).get("data", ""))
                        image_refs.append((i, block_idx, sub_idx, data_len))

        if len(image_refs) <= keep_recent:
            return 0

        for msg_idx, block_idx, sub_idx, data_len in image_refs[:-keep_recent]:
            rc = self.messages[msg_idx]["content"][block_idx]["content"]
            rc[sub_idx] = {"type": "text", "text": "[image removed to reduce request size]"}
            saved += data_len

        return saved

    # ========== Token Counting & Body Size ==========

    @staticmethod
    def iter_content_blocks(messages: list[dict]):
        """Yield (block_type, data) for each content element."""
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                yield ("text", content)
            elif isinstance(content, list):
                for block in content:
                    btype = block.get("type", "")
                    if btype == "text":
                        yield ("text", block.get("text", ""))
                    elif btype == "tool_use":
                        yield ("tool_use", block)
                    elif btype == "tool_result":
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            for sub in rc:
                                stype = sub.get("type", "")
                                if stype == "text":
                                    yield ("tool_result_text", sub.get("text", ""))
                                elif stype == "image":
                                    yield ("tool_result_image", sub.get("source", {}).get("data", ""))
                        else:
                            yield ("tool_result_str", str(rc))

    def _count_messages_tokens(self, messages: list[dict]) -> int:
        """Count total tokens in messages (delegates to compression.count_messages_tokens)."""
        from core.compression import count_messages_tokens
        return count_messages_tokens(messages)

    def _estimate_request_body_size(self, messages: list[dict]) -> int:
        """Estimate JSON request body size in bytes."""
        size = 0
        for btype, data in self.iter_content_blocks(messages):
            if btype == "text":
                size += len(data) * 2
            elif btype == "tool_use":
                size += len(json.dumps(data.get("input", {}), ensure_ascii=False)) * 2
            elif btype == "tool_result_text":
                size += len(data) * 2
            elif btype == "tool_result_image":
                size += len(data)
            elif btype == "tool_result_str":
                size += len(data) * 2
        size += 10000  # System prompt + tools overhead
        return size

    def _strip_images_from_messages(self, messages: list[dict]) -> list[dict]:
        """Strip images from messages for non-multimodal models."""
        result = []
        for msg in messages:
            content = msg.get("content", "")
            if not isinstance(content, list):
                result.append(msg)
                continue

            has_image = False
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                rc = block.get("content", "")
                if isinstance(rc, list):
                    for sub in rc:
                        if sub.get("type") == "image":
                            has_image = True
                            break
                if has_image:
                    break

            if not has_image:
                result.append(msg)
                continue

            new_content = []
            for block in content:
                if block.get("type") != "tool_result":
                    new_content.append(block)
                    continue
                rc = block.get("content", "")
                if not isinstance(rc, list):
                    new_content.append(block)
                    continue
                new_sub = []
                for sub in rc:
                    if sub.get("type") == "image":
                        new_sub.append({
                            "type": "text",
                            "text": "[image - model does not support multimodal]",
                        })
                    else:
                        new_sub.append(sub)
                new_content.append({**block, "content": new_sub})
            result.append({**msg, "content": new_content})
        return result

    def _get_messages_with_header(self) -> list[dict]:
        """Get valid messages for LLM call.

        Returns messages with _meta intact; _meta is stripped later
        by _strip_meta_from_messages() after _resolve_tool_results() runs.
        """
        return self.get_valid_messages(strip_meta=False)

    def _pad_dangling_tool_results(self) -> None:
        """为悬挂的 tool_use 补充占位 tool_result（原地修改 self.messages）。

        Anthropic API 要求每个 tool_use 必须在下一条 user 消息中得到
        tool_result 回应，否则返回 400。中途停止等中断场景会留下未回应的
        tool_use 并随会话持久化，导致该会话后续所有 LLM 调用失败。
        在每次 LLM 调用前修补，修补结果随下次保存持久化，可自愈历史损坏。
        """
        answered: set[str] = set()
        for msg in self.messages:
            if msg.get("_meta", {}).get("valid") is False:
                continue
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            if msg.get("role") == "user":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        answered.add(block.get("tool_use_id"))

        dangling: list[tuple[int, str]] = []  # (assistant 消息索引, tool_use_id)
        for idx, msg in enumerate(self.messages):
            if msg.get("_meta", {}).get("valid") is False:
                continue
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("id") not in answered:
                        dangling.append((idx, block.get("id")))

        if not dangling:
            return

        logger.warning(f"[Agent] 检测到 {len(dangling)} 个未回应的 tool_use，补充占位结果")
        # 倒序插入，避免索引失效；连续 user 消息由 adapter 的
        # merge_consecutive_same_role 合并，不会违反 API 的角色交替要求
        for idx, tool_use_id in reversed(dangling):
            placeholder = {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "[interrupted]",
                "is_error": True,
            }
            self.messages.insert(
                idx + 1,
                {"role": "user", "content": [placeholder], "_meta": {"id": generate_short_id()}},
            )

    # ========== LLM Calling ==========

    def _call_llm(self, streaming: bool = False, system_prompt: str = "") -> LLMResponse:
        """Call LLM with optional streaming.

        Args:
            streaming: Whether to use streaming mode
            system_prompt: System prompt to use

        Returns:
            LLMResponse with content and usage

        Raises:
            RuntimeError: LLM 调用最终失败（内部重试耗尽后抛出，
                消息为 format_llm_error 生成的友好文本）。
                调用方需自行捕获处理——不抛会导致 Agent 把错误
                误判为正常完成。用户停止不视为错误（返回 stop_reason="stopped"）。
        """
        if streaming:
            return self._call_llm_streaming(system_prompt)
        else:
            return self._call_llm_non_streaming(system_prompt)

    def _call_llm_non_streaming(self, system_prompt: str) -> LLMResponse:
        """Non-streaming LLM call."""
        self._pad_dangling_tool_results()
        messages = self._get_messages_with_header()
        messages = self._resolve_tool_results(messages)
        messages = self._strip_meta_from_messages(messages)

        if not self.model.multimodal:
            messages = self._strip_images_from_messages(messages)

        # Convert dict messages to Message objects
        message_objects = self._convert_to_message_objects(messages)

        try:
            response = self.client.chat(
                messages=message_objects,
                system=system_prompt,
                tools=self.tool_schemas,
                session_id=self._session_id,
            )
            # Track usage (UsageData object)
            if response.usage:
                self._update_usage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    api_calls=1,
                    cache_read_tokens=response.usage.cache_read_tokens,
                    cache_creation_tokens=response.usage.cache_write_tokens,
                )
            return response
        except Exception as e:
            # Handle 413 by stripping images and retrying
            if self._is_413_error(e):
                logger.warning("[LLM] 请求体过大，正在重试...")
                self._mark_all_images_invalid()
                self.save_messages()

                retry_messages = self._get_messages_with_header()
                if not self.model.multimodal:
                    retry_messages = self._strip_images_from_messages(retry_messages)
                retry_message_objects = self._convert_to_message_objects(retry_messages)
                try:
                    response = self.client.chat(
                        messages=retry_message_objects,
                        system=system_prompt,
                        tools=self.tool_schemas,
                        session_id=self._session_id,
                    )
                    if response.usage:
                        self._update_usage(
                            input_tokens=response.usage.input_tokens,
                            output_tokens=response.usage.output_tokens,
                            api_calls=1,
                            cache_read_tokens=response.usage.cache_read_tokens,
                            cache_creation_tokens=response.usage.cache_write_tokens,
                        )
                    return response
                except Exception as retry_e:
                    err_msg = format_llm_error(retry_e, self.client.base_url if self.client else "")
                    logger.error(f"[LLM] {err_msg}")
                    raise RuntimeError(err_msg) from retry_e

            err_msg = format_llm_error(e, self.client.base_url if self.client else "")
            logger.error(f"[LLM] {err_msg}")
            raise RuntimeError(err_msg) from e

    def _call_llm_streaming(self, system_prompt: str) -> LLMResponse:
        """Streaming LLM call. Think content passes through as-is."""
        text_parts: list[str] = []

        def on_text_delta(text: str):
            text_parts.append(text)
            if text and self._on_text:
                safe = text.encode("utf-8", errors="replace").decode("utf-8")
                self._on_text(safe)

        def on_thinking_delta(thinking: str):
            if self._on_thinking:
                self._on_thinking(thinking)

        self._pad_dangling_tool_results()
        messages = self._get_messages_with_header()
        messages = self._resolve_tool_results(messages)
        messages = self._strip_meta_from_messages(messages)

        if not self.model.multimodal:
            messages = self._strip_images_from_messages(messages)

        # Convert dict messages to Message objects
        message_objects = self._convert_to_message_objects(messages)

        # 重试配置：3次重试，退避 5/10/20 秒
        max_retries = 3
        retry_delays = [5, 10, 20]
        images_stripped = False

        for attempt in range(max_retries + 1):
            try:
                response = self.client.chat_stream(
                    messages=message_objects,
                    system=system_prompt,
                    tools=self.tool_schemas,
                    on_text=on_text_delta,
                    on_thinking=on_thinking_delta,
                    stop_check=lambda: self._stopped,
                    session_id=self._session_id,
                )
                # 成功，跳出重试循环
                break
            except InterruptedError:
                logger.info("[LLM] 已中断")
                return LLMResponse(
                    content=[TextBlock(text="".join(text_parts))],
                    stop_reason="stopped",
                )
            except Exception as e:
                # 最后一次重试仍失败
                if attempt == max_retries:
                    err_msg = format_llm_error(e, self.client.base_url if self.client else "")
                    logger.error(f"[LLM] {err_msg}")
                    raise RuntimeError(err_msg) from e

                # 413 错误：去掉图片后重试
                if self._is_413_error(e) and not images_stripped:
                    logger.warning("[LLM] 请求体过大，正在去掉图片重试...")
                    self._mark_all_images_invalid()
                    self.save_messages()
                    if self._on_text:
                        self._on_text("\x00RETRY_CLEAR\x00")
                    text_parts.clear()
                    images_stripped = True

                    message_objects = self._get_messages_with_header()
                    message_objects = self._resolve_tool_results(message_objects)
                    message_objects = self._strip_meta_from_messages(message_objects)
                    if not self.model.multimodal:
                        message_objects = self._strip_images_from_messages(message_objects)
                    message_objects = self._convert_to_message_objects(message_objects)
                else:
                    # 其他错误：等待后重试（分段 sleep，期间响应停止请求）
                    delay = retry_delays[attempt]
                    logger.warning(f"[LLM] 请求失败 (尝试 {attempt + 1}/{max_retries + 1})，{delay}秒后重试: {e}")
                    for _ in range(delay * 10):
                        if self._stopped:
                            return LLMResponse(
                                content=[TextBlock(text="".join(text_parts))],
                                stop_reason="stopped",
                            )
                        time.sleep(0.1)
                    text_parts.clear()
                    if self._on_text:
                        self._on_text("\x00RETRY_CLEAR\x00")

        # Track usage (UsageData object)
        if response.usage:
            self._update_usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                api_calls=1,
                cache_read_tokens=response.usage.cache_read_tokens,
                cache_creation_tokens=response.usage.cache_write_tokens,
            )

        return response

    @staticmethod
    def _is_413_error(e: Exception) -> bool:
        """Check if exception is a 413 Entity Too Large error."""
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 413:
            return True
        # Fallback for wrapped exceptions
        return "413" in str(e) or "Entity Too Large" in str(e)

    def _mark_all_images_invalid(self) -> None:
        """Replace all tool_result images with text placeholders for 413 retry.

        Replaces images in place (instead of invalidating whole messages):
        invalidating a user message here would leave the preceding assistant
        tool_use dangling, and the API would reject the next request with 400.
        """
        for msg in self.messages:
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                rc = block.get("content", "")
                if not isinstance(rc, list):
                    continue
                for sub_idx, sub in enumerate(rc):
                    if isinstance(sub, dict) and sub.get("type") == "image":
                        rc[sub_idx] = {"type": "text", "text": "[image removed to reduce request size]"}

    # ========== Usage Tracking ==========

    def _update_usage(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        api_calls: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> None:
        """Update usage statistics."""
        self._usage["input_tokens"] += input_tokens
        self._usage["output_tokens"] += output_tokens
        self._usage["api_calls"] += api_calls
        self._usage["cache_read_tokens"] = self._usage.get("cache_read_tokens", 0) + cache_read_tokens
        self._usage["cache_creation_tokens"] = self._usage.get("cache_creation_tokens", 0) + cache_creation_tokens

    def get_usage(self) -> dict[str, int]:
        """Get accumulated usage statistics."""
        return self._usage.copy()

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
