"""Runner - agent 单回合执行层（Step 3 抽取）。

从 BaseAgent 抽出的执行逻辑：LLM 调用（streaming/non-streaming + 413 去图重试）、
工具执行与外部文件存储、三层压缩。持 agent 引用访问共享状态（messages/client/
tools/回调），不自己持有会话状态 —— 会话状态归 AgentContext。

`run_round()` = 压缩 + LLM 调用，是 Loop 的回合单位；BaseAgent 的 `_call_llm`
转发至此，保证 loop 每轮 LLM 前触发压缩的语义不变，且测试对 `agent._call_llm`
的 patch 仍生效（patch 会整体替换转发入口）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

import httpx

from core.agent_runtime.context import INTERNAL_META
from core.llm import LLMResponse, Message, TextBlock, classify_llm_error, format_llm_error
from core.session import (
    INLINE_COMPACTED_PLACEHOLDER, format_compacted_placeholder, generate_short_id,
)
from core.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

# 兜底锁：仅用于不继承 Tool 基类的鸭子类型工具（如测试替身），真实工具均有 _exec_lock
_FALLBACK_EXEC_LOCK = threading.Lock()

# 压缩常量
KEEP_USER_MESSAGES = 3
_LARGE_OUTPUT_THRESHOLD = 10_000

# tool_use_id 白名单：只允许字母、数字、下划线、短横线，防止恶意 ID 路径穿越
_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# on_text 回调哨兵：413 去图/延迟重试时发出，通知前端清空已流式输出的文本
RETRY_CLEAR_SENTINEL = "\x00RETRY_CLEAR\x00"

# 估算 JSON 请求体大小时，为 system prompt + tools 预留的保守字节开销
_SYSTEM_OVERHEAD_BYTES = 10_000


class Runner:
    """单回合执行层：压缩 → LLM 调用 → 工具执行。"""

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    # ========== 回合单位 ==========

    def run_round(self, streaming: bool = False, system_prompt: str = "") -> LLMResponse:
        """执行一个完整回合：先压缩，再调用 LLM。"""
        self._check_and_compress()
        return self._call_llm(streaming=streaming, system_prompt=system_prompt)

    # ========== 工具执行 ==========

    @staticmethod
    def _safe_output_filename(tool_use_id: str) -> str:
        """Generate a safe output filename for a tool_use_id.

        非法 ID（含路径分隔符/`..` 等）回退随机文件名，防止路径穿越。
        """
        if not tool_use_id:
            return ""
        if _SAFE_ID_PATTERN.match(tool_use_id):
            return f"{tool_use_id}.txt"
        return f"{generate_short_id()}.txt"

    def _get_tool_by_name(self, name: str) -> Tool | None:
        """Find tool by name."""
        for tool in self.agent.tools:
            if tool.name == name:
                return tool
        return None

    def execute_tool(self, name: str, input_data: dict, tool_use_id: str) -> dict:
        """Execute a tool and return result metadata.

        Tool output is saved to external file {session_dir}/{tool_use_id}.txt only when needed:
        - Streaming tools (bash, python) for real-time frontend polling
        - Large outputs (exceeds threshold, will be truncated)
        - Multimodal content (images, saved as .json)
        Returns result dict with _meta containing internal fields.
        Uses Anthropic format: tool_use_id (not tool_call_id), is_error (not _is_error).

        并发：标记为 concurrency_safe 的纯读工具可跨线程并行（其 execute 不读写
        output_file/on_output）；其余工具经同实例 _exec_lock 串行化，防止并行批中
        output_file/on_output 两个实例属性被互相覆盖。
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

        if getattr(tool, "concurrency_safe", False) and name not in _STREAMING_TOOLS:
            return self._execute_tool_impl(
                name, tool, input_data, tool_use_id, _STREAMING_TOOLS, _PLACEHOLDER_TOOLS
            )
        with getattr(tool, "_exec_lock", _FALLBACK_EXEC_LOCK):
            return self._execute_tool_impl(
                name, tool, input_data, tool_use_id, _STREAMING_TOOLS, _PLACEHOLDER_TOOLS
            )

    def _execute_tool_impl(
        self,
        name: str,
        tool: Tool,
        input_data: dict,
        tool_use_id: str,
        _STREAMING_TOOLS: set[str],
        _PLACEHOLDER_TOOLS: set[str],
    ) -> dict:
        """工具执行主体（并发安全与否共用同一实现）。

        并发安全工具在其 execute 中不读写 output_file/on_output，故不设实例属性；
        非流式工具的输出落盘统一走显式路径，避免并行时实例属性互覆。
        """
        # Setup output file path
        # 净化 tool_use_id：不合法（含路径分隔符/`..` 等）则回退随机文件名，
        # 防止恶意 ID 造成路径穿越；消息体里的 tool_use_id 保持原样以匹配 API。
        output_filename = self._safe_output_filename(tool_use_id)
        output_file_path = ""
        if self.agent.session_dir and output_filename and name not in _PLACEHOLDER_TOOLS:
            output_file_path = str(self.agent.session_dir / output_filename)
            # 仅流式工具（bash/python）需要实例属性实时写 + 前端轮询文件；
            # 并发安全工具（纯读）execute 不读 output_file，不设实例属性防互覆。
            if name in _STREAMING_TOOLS:
                tool.output_file = output_file_path

                # Create empty file only for streaming tools (signals tool is running)
                try:
                    with open(output_file_path, "w", encoding="utf-8") as f:
                        f.write("")
                except Exception:
                    pass
                # 全局事件流：流式工具的实时输出增量 → agent._on_tool_output
                if self.agent._on_tool_output:
                    tool.on_output = (
                        lambda chunk, offset, _n=name, _id=tool_use_id:
                        self.agent._on_tool_output(_n, chunk, offset, _id)
                    )
                else:
                    tool.on_output = None

        # Notify callback
        if self.agent._on_tool_call:
            self.agent._on_tool_call(name, input_data, tool_use_id)

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
            tool.on_output = None

        # 空输出兜底：任何工具返回空内容时（竞态、空结果、无输出），统一补非空
        # 哨兵，避免实时流显示空白、会话重载被 hydration 误标为"[工具输出文件路径缺失]"
        if not result.output:
            from core.llm.types import TextBlock
            result.blocks.append(TextBlock(text="(no output)"))

        elapsed = time.perf_counter() - start_time

        # Log result
        output_preview = result.output
        if len(output_preview) > 500:
            output_preview = output_preview[:500] + f"\n... ({len(result.output)} chars total)"
        status = "失败" if result.error else "完成"
        logger.debug(f"[工具结果] {name} {status} ({elapsed:.2f}s)")

        # Notify callback
        if self.agent._on_tool_result:
            self.agent._on_tool_result(name, output_preview, result.error, tool_use_id)

        # Build _meta with internal fields
        file_size = len(result.output.encode('utf-8', errors='replace'))
        truncated = file_size > _LARGE_OUTPUT_THRESHOLD

        # Check if multimodal (has image blocks)
        from core.llm.types import ImageBlock
        is_multimodal = any(isinstance(block, ImageBlock) for block in result.blocks)

        # Only save external file when needed (streaming already saved, skip)
        needs_external_file = truncated or is_multimodal
        if needs_external_file and name not in _STREAMING_TOOLS and name not in _PLACEHOLDER_TOOLS:
            # 显式路径调用 save_output_to_file，避免并行时实例属性互覆
            save_path = str(self.agent.session_dir / output_filename) if self.agent.session_dir else None
            if save_path:
                saved_path = tool.save_output_to_file(result, output_file=save_path)
                # Update filename if changed to .json (multimodal)
                if saved_path:
                    output_filename = os.path.basename(saved_path)

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
        if not needs_external_file and self.agent.session_dir and output_filename:
            orphan_path = self.agent.session_dir / output_filename
            try:
                if orphan_path.exists():
                    orphan_path.unlink()
            except Exception:
                pass

        return result_dict

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
                        block["content"] = format_compacted_placeholder(output_path)
                    else:
                        # 内联压缩结果：无外置文件，原文保留在 messages.jsonl（会话历史）
                        block["content"] = INLINE_COMPACTED_PLACEHOLDER
                    continue

                # Read from external file
                if not output_path or not self.agent.session_dir:
                    block["content"] = "[工具输出文件路径缺失]"
                    continue

                file_path = (self.agent.session_dir / output_path).resolve()
                # 防御纵深：output_path 来自会话文件 _meta，篡改可能穿越 session 目录 → 拒绝。
                # 正常路径创建时已被 _safe_output_filename 消毒，此处与 routes_workspace 的
                # W10 校验对齐，双保险。
                if not file_path.is_relative_to(self.agent.session_dir.resolve()):
                    block["content"] = "[工具输出路径校验失败]"
                    continue
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

    # ========== 压缩 ==========

    def _check_and_compress(self) -> None:
        """3-layer compression before LLM call.

        Layer 1: Microcompact (replace old tool results with placeholder)
        Layer 2: Full compact (LLM summary when tokens > 80% threshold)
        Layer 3: Emergency body size (mark old tool calls/images as invalid)
        """
        from core.compression import microcompact_tool_results, count_messages_tokens

        MAX_TOKENS = self.agent.model.max_context_tokens
        MICROCOMPACT_KEEP_RECENT = 6
        FULL_COMPACT_TOKEN_RATIO = 0.80
        MAX_BODY_SIZE = 3_000_000

        # Layer 1: Microcompact
        saved = microcompact_tool_results(
            self.agent.messages,
            keep_recent=MICROCOMPACT_KEEP_RECENT,
        )
        if saved > 0:
            logger.debug(f"[Microcompact] 压缩旧工具结果，节省约 {saved:,} 字节")
            self.agent._invalidate_message_cache()

        # Calculate tokens
        messages = self.agent._get_messages_with_header()
        total_tokens = self.agent._count_messages_tokens(messages)

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
        messages = self.agent._get_messages_with_header()
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
                messages = self.agent._get_messages_with_header()
                logger.info(f"[上下文] 工具调用标记完成，节省 {saved} 字节")

            body_size = self._estimate_request_body_size(messages)
            if body_size > MAX_BODY_SIZE:
                logger.info("[上下文] 正在替换旧图片为占位符...")
                saved = self._mark_old_images_invalid(keep_recent=3)
                if saved > 0:
                    logger.info(f"[上下文] 图片替换完成，节省 {saved} 字节")

        # 上述各层压缩都可能原地修改 self.messages，统一失效 valid 缓存
        self.agent._invalidate_message_cache()

    def _perform_full_compact(self, keep_user_messages: int) -> tuple[int, int]:
        """Full auto compact: summarize old messages, keep recent user messages.

        Returns (before_tokens, after_tokens) tuple.
        """
        from core.compression import count_messages_tokens

        all_messages = self.agent.messages
        total_tokens = self.agent._count_messages_tokens(self.agent.get_valid_messages())

        # Find split point
        valid_messages = self.agent.get_valid_messages()
        split_idx = self.agent._find_split_by_user_messages(valid_messages, keep_user_messages)
        if split_idx <= 0:
            raise ValueError("Not enough messages to compress")

        # Summarize old messages FIRST — only invalidate them after the summary
        # succeeds. Invalidating before summarizing would permanently hide the
        # old history if the summary LLM call fails.
        old_messages = valid_messages[:split_idx]
        if not old_messages:
            # Nothing to summarize
            return total_tokens, total_tokens

        # 经 agent 转发调用：测试 patch agent._summarize_messages 时生效，且
        # 未被 patch 时经转发落到 Runner 真实实现（可覆写 seam 与 _get_messages_with_header 同理）
        summary = self.agent._summarize_messages(old_messages)
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

        # Add summary messages（summary=True：进 commits 视图内嵌摘要，不进 jsonl，
        # UI 完整历史保留压缩前原始消息，不展示摘要）
        self.agent.add_message(
            "user",
            "[Our previous conversation has been compacted due to context length.]",
            meta={"summary": True},
        )
        self.agent.add_message("assistant", summary, meta={"summary": True})

        self.agent._invalidate_message_cache()

        new_tokens = self.agent._count_messages_tokens(self.agent.get_valid_messages())
        logger.info(f"[Full Compact] 完成: {total_tokens:,} → {new_tokens:,} tokens")
        return total_tokens, new_tokens

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
        max_chars = min(50000, self.agent.model.max_context_tokens * 2)
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
            response = self.agent.client.chat(
                messages=[Message(role="user", content=summary_prompt)],
                system="你是一个对话总结助手。请用中文简洁地总结对话要点。",
                session_id=self.agent._session_id,
            )
            # Track usage (UsageData object)
            if response.usage:
                self.agent._update_usage(
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

        for msg in self.agent.messages:
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

        for i, msg in enumerate(self.agent.messages):
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
            rc = self.agent.messages[msg_idx]["content"][block_idx]["content"]
            rc[sub_idx] = {"type": "text", "text": "[image removed to reduce request size]"}
            saved += data_len

        return saved

    def _mark_all_images_invalid(self) -> None:
        """Replace all tool_result images with text placeholders for 413 retry.

        Replaces images in place (instead of invalidating whole messages):
        invalidating a user message here would leave the preceding assistant
        tool_use dangling, and the API would reject the next request with 400.
        """
        for msg in self.agent.messages:
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

    # ========== Token 计数 & 请求体大小 ==========

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
        size += _SYSTEM_OVERHEAD_BYTES
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

    # ========== LLM 调用 ==========

    @staticmethod
    def _convert_to_message_objects(messages: list[dict]) -> list[Message]:
        """Convert dict-based messages to Message objects.

        Internal helper for passing messages to LLMClient.
        """
        return [Message.from_dict(msg) for msg in messages]

    def _prepare_messages_for_llm(
        self,
        *,
        pad_dangling: bool = True,
        resolve_results: bool = True,
    ) -> list[Message]:
        """Build Message objects for the LLM API from agent.messages.

        共享的消息预处理管道（non-streaming / streaming / 413 重试 / 超时兜底）。
        ``pad_dangling``：为悬挂 tool_use 补占位（重试或超时兜底路径已修补过，跳过）。
        ``resolve_results``：从外部文件解析工具输出并剥离内部 _meta
        （non-streaming 413 重试路径首轮已解析过，跳过）。
        """
        if pad_dangling:
            self.agent._pad_dangling_tool_results()
        messages = self.agent._get_messages_with_header()
        if resolve_results:
            messages = self._resolve_tool_results(messages)
            messages = self._strip_meta_from_messages(messages)
        if not self.agent.model.multimodal:
            messages = self._strip_images_from_messages(messages)
        return self._convert_to_message_objects(messages)

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
        message_objects = self._prepare_messages_for_llm()

        try:
            response = self.agent.client.chat(
                messages=message_objects,
                system=system_prompt,
                tools=self.agent.tool_schemas,
                session_id=self.agent._session_id,
            )
            # Track usage (UsageData object)
            if response.usage:
                self.agent._update_usage(
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
                # autonomous 模式写 exec schema（exec_id/task），避免覆盖 exec 日志；
                # interactive 模式写普通 session schema。
                if getattr(self.agent, "_mode", "interactive") == "autonomous":
                    self.agent._save_progress(len(self.agent.messages), status="running")
                else:
                    self.agent.save_messages()

                # 首轮已 pad/解析过工具输出，重试仅重新组装并去图
                retry_message_objects = self._prepare_messages_for_llm(
                    pad_dangling=False, resolve_results=False
                )
                try:
                    response = self.agent.client.chat(
                        messages=retry_message_objects,
                        system=system_prompt,
                        tools=self.agent.tool_schemas,
                        session_id=self.agent._session_id,
                    )
                    if response.usage:
                        self.agent._update_usage(
                            input_tokens=response.usage.input_tokens,
                            output_tokens=response.usage.output_tokens,
                            api_calls=1,
                            cache_read_tokens=response.usage.cache_read_tokens,
                            cache_creation_tokens=response.usage.cache_write_tokens,
                        )
                    return response
                except Exception as retry_e:
                    err_msg = format_llm_error(retry_e, self.agent.client.base_url if self.agent.client else "")
                    logger.error(f"[LLM] {err_msg}")
                    raise RuntimeError(err_msg) from retry_e

            err_msg = format_llm_error(e, self.agent.client.base_url if self.agent.client else "")
            logger.error(f"[LLM] {err_msg}")
            raise RuntimeError(err_msg) from e

    def _call_llm_streaming(self, system_prompt: str) -> LLMResponse:
        """Streaming LLM call. Think content passes through as-is."""
        text_parts: list[str] = []

        def on_text_delta(text: str):
            text_parts.append(text)
            if text and self.agent._on_text:
                safe = text.encode("utf-8", errors="replace").decode("utf-8")
                self.agent._on_text(safe)

        def on_thinking_delta(thinking: str):
            if self.agent._on_thinking:
                self.agent._on_thinking(thinking)

        message_objects = self._prepare_messages_for_llm()

        # 重试配置：3次重试，退避 5/10/20 秒
        max_retries = 3
        retry_delays = [5, 10, 20]
        images_stripped = False

        for attempt in range(max_retries + 1):
            try:
                response = self.agent.client.chat_stream(
                    messages=message_objects,
                    system=system_prompt,
                    tools=self.agent.tool_schemas,
                    on_text=on_text_delta,
                    on_thinking=on_thinking_delta,
                    stop_check=lambda: self.agent._stopped,
                    session_id=self.agent._session_id,
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
                    err_msg = format_llm_error(e, self.agent.client.base_url if self.agent.client else "")
                    logger.error(f"[LLM] {err_msg}")
                    raise RuntimeError(err_msg) from e

                # 413 错误：去掉图片后重试
                if self._is_413_error(e) and not images_stripped:
                    logger.warning("[LLM] 请求体过大，正在去掉图片重试...")
                    self._mark_all_images_invalid()
                    self.agent.save_messages()
                    if self.agent._on_text:
                        self.agent._on_text(RETRY_CLEAR_SENTINEL)
                    text_parts.clear()
                    images_stripped = True

                    # 去图重试：已 pad 过，但需重新解析工具输出（图片标记为无效后重组装）
                    message_objects = self._prepare_messages_for_llm(pad_dangling=False)
                else:
                    # 统一 taxonomy（三态之二/三）：非瞬态（quota/auth/context_length/
                    # bad_request）立即放弃，不消耗剩余重试次数——原来 quota 429 也会空等 3 次。
                    info = classify_llm_error(e, self.agent.client.base_url if self.agent.client else "")
                    if not info.should_retry:
                        logger.error(f"[LLM] {info.message}")
                        raise RuntimeError(info.message) from e

                    # 瞬态错误：退避重试（尊重 Retry-After），分段 sleep 期间响应停止请求
                    delay = info.retry_after_s if info.retry_after_s is not None else retry_delays[attempt]
                    logger.warning(f"[LLM] 请求失败 ({info.kind}，尝试 {attempt + 1}/{max_retries + 1})，{delay:.0f}s 后重试")
                    for _ in range(int(delay * 10)):
                        if self.agent._stopped:
                            return LLMResponse(
                                content=[TextBlock(text="".join(text_parts))],
                                stop_reason="stopped",
                            )
                        time.sleep(0.1)
                    text_parts.clear()
                    if self.agent._on_text:
                        self.agent._on_text(RETRY_CLEAR_SENTINEL)

        # Track usage (UsageData object)
        if response.usage:
            self.agent._update_usage(
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
