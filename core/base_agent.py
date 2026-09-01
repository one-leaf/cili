"""BaseAgent - unified agent loop for RootAgent and SubAgent.

Provides shared infrastructure for agent execution:
- Message management (self.messages)
- Tool execution with external file storage
- 3-layer compression (microcompact → full compact → emergency body size)
- LLM calling with 413 retry (thinking content passes through)
- Usage tracking

Subclasses (RootAgent, SubAgent) customize:
- Tool creation (override _create_tools())
- System prompt building (override _build_system_prompt())
- Callbacks (RootAgent has streaming callbacks)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from core.config import Config, ModelConfig
from core.llm import LLMClient, LLMResponse, format_llm_error, Message, TextBlock, UsageData
from core.tools.shared.base import Tool, ToolResult

logger = logging.getLogger(__name__)

_CHINESE_RE = re.compile(r'[一-鿿]')

# Compression constants
KEEP_USER_MESSAGES = 3
_LARGE_OUTPUT_THRESHOLD = 30_000
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
        self._compression_attempted = False

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

        # Session ID for LiteLLM routing (subclasses set this)
        self._session_id: str = ""

    # ========== Message Management ==========

    def _convert_to_message_objects(self, messages: list[dict]) -> list[Message]:
        """Convert dict-based messages to Message objects.

        Internal helper for passing messages to LLMClient.
        """
        return [Message.from_dict(msg) for msg in messages]

    def add_message(self, role: str, content: Any) -> None:
        """Add a message to internal message list."""
        # Add _valid field to content blocks
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "_valid" not in block:
                    block["_valid"] = True
        self.messages.append({"role": role, "content": content})

    def save_messages(self, metadata: dict | None = None) -> None:
        """Save messages to session_dir/index.json.

        Args:
            metadata: Optional metadata to include in the file
        """
        if not self.session_dir:
            return

        self.session_dir.mkdir(parents=True, exist_ok=True)
        session_file = self.session_dir / "index.json"

        data = {
            "session_id": self._session_id or self.session_dir.name,
            "messages": self.messages,
            "metadata": metadata or {},
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

    def get_valid_messages(self) -> list[dict]:
        """Get messages with _valid=False blocks filtered out.

        Used before sending to LLM API. Thinking blocks are preserved because
        Anthropic API requires them in subsequent messages for multi-turn context.
        Note: _compacted is preserved here, filtered later during serialization.
        """
        INTERNAL = {"_valid", "_content"}  # _compacted kept for _resolve_tool_results
        result = []

        for msg in self.messages:
            # Skip messages marked as invalid
            if msg.get("_valid") is False:
                continue
            # Skip internal message types
            if msg.get("role") == "_subagent_ref":
                continue

            role = msg.get("role")
            content = msg.get("content", "")

            # String content
            if not isinstance(content, list):
                result.append({"role": role, "content": content})
                continue

            # Filter invalid blocks
            valid_blocks = []
            for block in content:
                if not block.get("_valid", True):
                    continue

                # Recursively filter tool_result sub-blocks
                if block.get("type") == "tool_result":
                    rc = block.get("content", "")
                    if isinstance(rc, list):
                        filtered_sub = [s for s in rc if s.get("_valid", True)]
                        if not filtered_sub:
                            continue
                        clean_sub = [{k: v for k, v in s.items() if k not in INTERNAL} for s in filtered_sub]
                        clean_block = {k: v for k, v in block.items() if k not in INTERNAL}
                        clean_block["content"] = clean_sub
                        valid_blocks.append(clean_block)
                        continue

                clean_block = {k: v for k, v in block.items() if k not in INTERNAL}
                valid_blocks.append(clean_block)

            if valid_blocks:
                result.append({"role": role, "content": valid_blocks})

        return result

    # ========== Tool Execution ==========

    def _execute_tool(self, name: str, input_data: dict, tool_use_id: str) -> dict:
        """Execute a tool and return result metadata.

        Tool output is saved to external file {session_dir}/{tool_use_id}.txt.
        Returns metadata dict (not content) for LLM context.
        """
        tool = self._get_tool_by_name(name)

        if tool is None:
            return {
                "type": "tool_result",
                "tool_call_id": tool_use_id,
                "tool_name": name,
                "content": f"Error: unknown tool '{name}'",
                "_completed": True,
                "_is_error": True,
            }

        logger.debug(f"[工具调用] {name}")

        # Setup output file path
        output_filename = f"{tool_use_id}.txt" if tool_use_id else ""
        output_file_path = ""
        if self.session_dir and output_filename:
            output_file_path = str(self.session_dir / output_filename)
            tool.output_file = output_file_path

            # Create empty file (signals tool is running)
            try:
                with open(output_file_path, "w", encoding="utf-8") as f:
                    f.write("")
            except Exception:
                pass

        # Create pending result
        pending_result = {
            "type": "tool_result",
            "tool_call_id": tool_use_id,
            "tool_name": name,
            "_file_size": 0,
            "_truncated": False,
            "_compacted": False,
            "_output_path": output_filename,
            "_is_error": False,
            "_completed": False,
        }

        # Notify callback
        if self._on_tool_call:
            self._on_tool_call(name, input_data, tool_use_id)

        # Execute tool
        start_time = time.perf_counter()
        result = ToolResult("Error: tool execution was interrupted", error=True)

        try:
            input_data = tool.coerce_input(input_data)
            result = tool.execute(**input_data)
        except Exception as e:
            result = ToolResult(f"Error executing tool: {e}", error=True)
        finally:
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

        # Return final result metadata
        return {
            "type": "tool_result",
            "tool_call_id": tool_use_id,
            "tool_name": name,
            "_file_size": len(result.output.encode('utf-8', errors='replace')),
            "_truncated": len(result.output) > _LARGE_OUTPUT_THRESHOLD,
            "_compacted": False,
            "_output_path": output_filename,
            "_is_error": result.error,
            "_completed": True,
            "_wait_for_user": getattr(result, 'wait_for_user', False),
            "_meta": result.meta,
        }

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

                # Handle compacted marker - include filename for read_tool_result
                if block.get("_compacted"):
                    output_filename = block.get("_output_path", "")
                    if output_filename:
                        # Extract tool_use_id from filename (remove extension)
                        tool_use_id = output_filename.replace(".txt", "").replace(".json", "")
                        block["content"] = f"[Compacted: use `read_tool_result` tool with tool_use_id=\"{tool_use_id}\" to retrieve original content]"
                    else:
                        block["content"] = "[Compacted: tool_use_id unknown]"
                    continue

                # Read from external file
                output_path = block.get("_output_path", "")
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
                        if block.get("_truncated"):
                            truncated = Tool.truncate_middle(file_content, 8000)
                            file_size = block.get('_file_size', len(file_content))
                            guide = (
                                f"\n\n---\n"
                                f"[提示] 工具输出过长（{file_size:,} 字符），已截断显示。"
                                f"完整输出保存在文件: {output_path}。"
                                f"如需查看完整内容，请使用 read 工具分批读取该文件。"
                            )
                            block["content"] = truncated + guide
                        else:
                            block["content"] = Tool.truncate_result(file_content, Tool.MAX_TOOL_RESULT_SIZE_CHARS)

                except Exception as e:
                    block["content"] = f"[读取工具输出失败: {e}]"
                    continue

        return messages

    # ========== Compression ==========

    def _check_and_compress(self) -> None:
        """3-layer compression before LLM call.

        Layer 1: Microcompact (replace old tool results with placeholder)
        Layer 2: Full compact (LLM summary when tokens > 80% threshold)
        Layer 3: Emergency body size (mark old tool calls/images as invalid)
        """
        from core.compression import microcompact_tool_results, count_messages_tokens

        MAX_TOKENS = self.config.model.max_context_tokens
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
            self._compression_attempted = True
        else:
            self._compression_attempted = False

        # Layer 3: Emergency body size
        messages = self._get_messages_with_header()
        body_size = self._estimate_request_body_size(messages)

        if body_size > MAX_BODY_SIZE:
            logger.info("[上下文] 请求体过大，正在标记旧工具调用为无效...")
            saved = self._mark_old_tool_calls_invalid(keep_recent_rounds=3)
            if saved > 0:
                messages = self._get_messages_with_header()
                logger.info(f"[上下文] 工具调用标记完成，节省 {saved} 字节")

            body_size = self._estimate_request_body_size(messages)
            if body_size > MAX_BODY_SIZE:
                logger.info("[上下文] 正在标记旧图片为无效...")
                saved = self._mark_old_images_invalid(keep_recent=3)
                if saved > 0:
                    logger.info(f"[上下文] 图片标记完成，节省 {saved} 字节")

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

        # Mark messages before split as invalid
        valid_count = 0
        for i, msg in enumerate(all_messages):
            if msg.get("_valid") is False:
                continue
            if valid_count < split_idx:
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            block["_valid"] = False
                else:
                    msg["_valid"] = False
                valid_count += 1
            else:
                break

        # Summarize old messages
        old_messages = valid_messages[:split_idx]
        summary = self._summarize_messages(old_messages)
        if summary.startswith("(摘要生成失败") or summary.startswith("（摘要生成失败"):
            logger.error("摘要生成失败，跳过压缩")
            return total_tokens, total_tokens

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
        max_chars = min(50000, self.config.model.max_context_tokens * 2)
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
            if msg.get("_valid") is False or msg.get("role") == "_subagent_ref":
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
                    tool_calls.append({"block": block, "round": round_number})

        if round_number <= keep_recent_rounds:
            return 0

        for call in tool_calls:
            if call["round"] <= round_number - keep_recent_rounds:
                block = call["block"]
                if "_valid" not in block or block["_valid"]:
                    content = block.get("input", {}) if block.get("type") == "tool_use" else block.get("content", "")
                    size = len(str(content))
                    block["_valid"] = False
                    saved += size

        return saved

    def _mark_old_images_invalid(self, keep_recent: int = 5) -> int:
        """Mark old images as invalid to reduce body size."""
        saved = 0
        image_messages = []

        for i, msg in enumerate(self.messages):
            if msg.get("_valid") is False or msg.get("role") == "_subagent_ref":
                continue
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue

            for block_idx, block in enumerate(content):
                if block.get("type") != "tool_result":
                    continue
                rc = block.get("content", "")
                if not isinstance(rc, list):
                    continue
                for sub_idx, sub in enumerate(rc):
                    if sub.get("type") == "image":
                        data_len = len(sub.get("source", {}).get("data", ""))
                        image_messages.append((i, block_idx, sub_idx, data_len))

        if len(image_messages) <= keep_recent:
            return 0

        to_strip = image_messages[:-keep_recent]
        for msg_idx, block_idx, sub_idx, data_len in to_strip:
            block = self.messages[msg_idx]["content"][block_idx]
            sub = block["content"][sub_idx]
            sub["_valid"] = False
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

    def _count_tokens(self, text: str) -> int:
        """Estimate token count."""
        if not text:
            return 0
        chinese_chars = len(_CHINESE_RE.findall(text))
        other_chars = len(text) - chinese_chars
        return int(chinese_chars / 2.5 + other_chars / 4)

    def _count_messages_tokens(self, messages: list[dict]) -> int:
        """Count total tokens in messages."""
        total = 0
        for btype, data in self.iter_content_blocks(messages):
            if btype == "text":
                total += self._count_tokens(data)
            elif btype == "tool_use":
                total += self._count_tokens(json.dumps(data.get("input", {}), ensure_ascii=False))
            elif btype == "tool_result_text":
                total += self._count_tokens(data)
            elif btype == "tool_result_image":
                total += max(750, len(data) // 100)
            elif btype == "tool_result_str":
                total += self._count_tokens(data)
        return total

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
        """Get valid messages for LLM call."""
        return self.get_valid_messages()

    # ========== LLM Calling ==========

    def _call_llm(self, streaming: bool = False, system_prompt: str = "") -> LLMResponse:
        """Call LLM with optional streaming.

        Args:
            streaming: Whether to use streaming mode
            system_prompt: System prompt to use

        Returns:
            LLMResponse with content and usage
        """
        if streaming:
            return self._call_llm_streaming(system_prompt)
        else:
            return self._call_llm_non_streaming(system_prompt)

    def _call_llm_non_streaming(self, system_prompt: str) -> LLMResponse:
        """Non-streaming LLM call."""
        messages = self._get_messages_with_header()
        messages = self._resolve_tool_results(messages)

        if not self.config.model.multimodal:
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
            err_msg = format_llm_error(e, self.client.base_url if self.client else "")
            logger.error(f"[LLM] {err_msg}")
            return LLMResponse(
                content=[TextBlock(text=err_msg)],
                stop_reason="error",
            )

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

        messages = self._get_messages_with_header()
        messages = self._resolve_tool_results(messages)

        if not self.config.model.multimodal:
            messages = self._strip_images_from_messages(messages)

        # Convert dict messages to Message objects
        message_objects = self._convert_to_message_objects(messages)

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
        except InterruptedError:
            logger.info("[LLM] 已中断")
            return LLMResponse(
                content=[TextBlock(text="".join(text_parts))],
                stop_reason="stopped",
            )
        except Exception as e:
            # Handle 413 by stripping images and retrying
            if "413" in str(e) or "Entity Too Large" in str(e):
                logger.warning("[LLM] 请求体过大，正在重试...")
                self._mark_all_images_invalid()
                self.save_messages()
                if self._on_text:
                    self._on_text("\x00RETRY_CLEAR\x00")
                text_parts.clear()

                retry_messages = self._get_messages_with_header()
                if not self.config.model.multimodal:
                    retry_messages = self._strip_images_from_messages(retry_messages)
                retry_message_objects = self._convert_to_message_objects(retry_messages)
                response = self.client.chat_stream(
                    messages=retry_message_objects,
                    system=system_prompt,
                    tools=self.tool_schemas,
                    on_text=on_text_delta,
                    on_thinking=on_thinking_delta,
                    stop_check=lambda: self._stopped,
                    session_id=self._session_id,
                )
            else:
                raise

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

    def _mark_all_images_invalid(self) -> None:
        """Mark all images as invalid for 413 retry."""
        for msg in self.messages:
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                rc = block.get("content", "")
                if not isinstance(rc, list):
                    continue
                for sub in rc:
                    if sub.get("type") == "image":
                        sub["_valid"] = False

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
