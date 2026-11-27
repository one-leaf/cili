"""TurnExecutor - runner 单回合执行层（Step 3 抽取）。

从 BaseSessionRunner 抽出的执行逻辑：LLM 调用（streaming/non-streaming + 413 去图重试）、
工具执行与外部文件存储、三层压缩。持 runner 引用访问共享状态（messages/client/
tools/回调），不自己持有会话状态 —— 会话状态归 ConversationStore。

`run_round()` = 压缩 + LLM 调用，是 Loop 的回合单位；BaseSessionRunner 的 `_call_llm`
转发至此，保证 loop 每轮 LLM 前触发压缩的语义不变，且测试对 `runner._call_llm`
的 patch 仍生效（patch 会整体替换转发入口）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from core.compression import approx_token_count, truncate_middle, truncate_result
from core.handoff import build_handoff_document
from core.image_pipeline import (
    DEFAULT_MAX_BYTES,
    apply_image_budget,
    collect_current_turn_image_keys,
    image_ref_text,
    normalize_image_block,
    project_images_for_request,
    write_tool_image_sidecar,
)
from core.llm import (
    LLMResponse,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    classify_llm_error,
    format_llm_error,
)
from core.session import INTERNAL_META_FIELDS as INTERNAL_META
from core.session import generate_short_id
from core.tool_limits import LARGE_OUTPUT_EXTERNALIZE_BYTES
from core.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

# 兜底锁：仅用于不继承 Tool 基类的鸭子类型工具（如测试替身），真实工具均有 _exec_lock
_FALLBACK_EXEC_LOCK = threading.Lock()

# 压缩常量
KEEP_USER_MESSAGES = 3
FULL_COMPACT_TOKEN_RATIO = 0.80
# 回合中途的上下文压力（相对 max_context_tokens 工作预算）：
# ≥100% 注入收尾提示，≥150% 强制全压缩
CONTEXT_WARN_RATIO = 1.00
CONTEXT_FORCE_COMPACT_RATIO = 1.50
# 完整压缩后保留最近约 25% 上下文的原始消息，其余摘要化
COMPACT_KEEP_TOKEN_RATIO = 0.25
COMPACT_KEEP_MIN_TOKENS = 4000
COMPACT_KEEP_MAX_TOKENS = 32000
_LARGE_OUTPUT_THRESHOLD = LARGE_OUTPUT_EXTERNALIZE_BYTES

# tool_use_id 白名单：只允许字母、数字、下划线、短横线，防止恶意 ID 路径穿越
_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# 工具能力来自 ``Tool.streams_output`` / ``Tool.placeholder_output`` 类属性
def _tool_flag(tool, attr: str) -> bool:
    """读工具类属性（测试替身 MagicMock 实例不会伪装出真值）。"""
    return bool(getattr(type(tool), attr, False))


# on_text 回调哨兵：413 去图/延迟重试时发出，通知前端清空已流式输出的文本
RETRY_CLEAR_SENTINEL = "\x00RETRY_CLEAR\x00"

# 估算 JSON 请求体大小时，为 system prompt + tools 预留的保守字节开销
_SYSTEM_OVERHEAD_BYTES = 10_000


def estimate_request_body_size(messages: list[dict]) -> int:
    """估算 JSON 请求体大小（字节）。

    模块级函数：无 runner 的只读路径（如上下文占用接口）也要同一口径；
    ``TurnExecutor._estimate_request_body_size`` 委托到这里，避免两份实现漂移。
    """
    size = 0
    for btype, data in TurnExecutor.iter_content_blocks(messages):
        if btype == "text":
            size += len(data) * 2
        elif btype == "tool_use":
            size += len(json.dumps(data.get("input", {}), ensure_ascii=False)) * 2
        elif btype == "tool_result_text":
            size += len(data) * 2
        elif btype in ("tool_result_image", "image"):
            size += len(data)
        elif btype == "tool_result_str":
            size += len(data) * 2
    size += _SYSTEM_OVERHEAD_BYTES
    return size


class TurnExecutor:
    """单回合执行层：压缩 → LLM 调用 → 工具执行。"""

    def __init__(self, runner: Any) -> None:
        self.runner = runner

    # ========== 回合单位 ==========

    def run_round(self, streaming: bool = False, system_prompt: str | list[str] = "") -> LLMResponse:
        """执行一个完整回合：先压缩，再调用 LLM。"""
        self._check_and_compress()
        return self._call_llm(streaming=streaming, system_prompt=system_prompt)

    def _image_mode(self) -> str:
        """图片历史策略：full | current_turn（与 runner 共用同一读取口径）。"""
        getter = getattr(self.runner, "_image_history_mode", None)
        if callable(getter):
            return getter()
        # BaseSessionRunner（测试/极简运行器）没有该方法：直接读配置
        return getattr(
            getattr(getattr(self.runner, "config", None), "images", None),
            "history_mode", "current_turn",
        )

    def _effective_turn_id(self) -> str:
        """当前回合 id；恢复路径从最近 user 消息的 meta.turn_id 推导。"""
        turn_id = getattr(self.runner, "current_turn_id", "")
        if turn_id:
            return str(turn_id)
        for msg in reversed(self.runner.messages):
            meta = msg.get("_meta") or {}
            if msg.get("role") == "user" and meta.get("turn_id"):
                return str(meta["turn_id"])
        return ""

    def _resolved_messages(self) -> list[dict]:
        """有效消息 + 外部工具结果解析；保留 _meta，供投影/降级使用。"""
        messages = self.runner._get_messages_with_header()
        return self._resolve_tool_results(messages)

    def build_projected_messages(self, *, force_reference: bool = False) -> list[dict]:
        """构建与实际发送一致的投影消息。

        流程：有效消息 -> 外部工具结果解析 -> 图片按回合投影 -> 剥离内部 _meta。
        """
        return self._project_resolved_messages(
            self._resolved_messages(), force_reference=force_reference,
            latch_budget=False,
        )

    def _project_resolved_messages(
        self, messages: list[dict], *, force_reference: bool = False,
        latch_budget: bool = True,
    ) -> list[dict]:
        """对已解析消息做图片投影、请求预算与 meta 剥离（不含磁盘读取）。

        ``latch_budget=False`` 用于 UI/估算等只读路径：预算照常计算，
        但不把新退役的载体写入 runner 状态。
        """
        if self._image_mode() == "current_turn" or force_reference:
            messages = project_images_for_request(
                messages,
                self._effective_turn_id(),
                force_reference=force_reference,
                demoted_paths=getattr(self.runner, "_demoted_image_paths", set()),
            )
        if not force_reference:
            messages = self._apply_image_budget(messages, latch=latch_budget)
        return self._strip_meta_from_messages(messages)

    def _image_budget_limits(self) -> dict[str, int]:
        """读取 ``setting.json -> images`` 的请求预算参数。"""
        cfg = getattr(getattr(self.runner, "config", None), "images", None)

        def _int(name: str, default: int) -> int:
            try:
                return max(0, int(getattr(cfg, name, default)))
            except (TypeError, ValueError):
                return default

        return {
            "max_images": _int("max_images_per_request", 100) or 100,
            "hard_limit": _int("outbound_image_hard_limit", 100) or 100,
            "max_bytes": _int("max_request_image_bytes", 20 * 1024 * 1024),
            "eviction_batch": _int("eviction_batch", 4) or 4,
            "eviction_floor": _int("eviction_floor", 3),
        }

    def _apply_image_budget(
        self, messages: list[dict], *, latch: bool
    ) -> list[dict]:
        """请求级图片预算：退役最旧的含图 tool_result（用户上传图保留）。"""
        limits = self._image_budget_limits()
        retired = getattr(self.runner, "_retired_image_carriers", None)
        if not isinstance(retired, set):
            retired = set()
            setattr(self.runner, "_retired_image_carriers", retired)
        result = apply_image_budget(
            messages,
            max_images=limits["max_images"],
            hard_limit=limits["hard_limit"],
            max_bytes=limits["max_bytes"],
            eviction_batch=limits["eviction_batch"],
            eviction_floor=limits["eviction_floor"],
            retired_ids=retired,
        )
        if latch:
            retired.update(result.newly_retired_ids)
            if result.removed_images:
                self._append_budget_notice(messages, result)
        return messages

    @staticmethod
    def _append_budget_notice(messages: list[dict], result) -> None:
        """把本次退役的提示追加到最后一条 user 消息（请求副本，不落盘）。"""
        paths = result.removed_paths[:3]
        shown = ", ".join(f'"{p}"' for p in paths)
        more = f" (+{len(result.removed_paths) - len(paths)} more)" if len(result.removed_paths) > len(paths) else ""
        detail = f": {shown}{more}" if shown else ""
        notice = (
            f"[image budget] {result.removed_images} earlier image(s) were removed "
            f"from this request to stay within provider limits{detail}. "
            'Use read("<path>") to view one again.'
        )
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                content.append({"type": "text", "text": notice})
            elif isinstance(content, str):
                msg["content"] = [
                    {"type": "text", "text": content},
                    {"type": "text", "text": notice},
                ]
            return

    def _collect_current_turn_image_keys(self, messages: list[dict]) -> list[str]:
        """收集投影消息中属于当前回合、仍为图片的稳定 key，按出现顺序。"""
        return collect_current_turn_image_keys(messages, self._effective_turn_id())

    def _demote_current_turn_images(self, max_body_size: int) -> int:
        """把当前回合最旧图片降级为引用，直到请求体小于上限或全部降级。"""
        if self._image_mode() != "current_turn":
            return 0
        # 解析一次（含外部工具结果 IO），循环内只重复廉价的投影/估算
        resolved = self._resolved_messages()
        candidates = self._collect_current_turn_image_keys(resolved)
        if not candidates:
            return 0
        demoted = getattr(self.runner, "_demoted_image_paths", set())
        added = 0
        for key in candidates:
            if max_body_size > 0 and self._estimate_request_body_size(
                self._project_resolved_messages(resolved, latch_budget=False)
            ) <= max_body_size:
                break
            if key not in demoted:
                demoted.add(key)
                added += 1
        return added

    def _demote_all_current_turn_images(self) -> int:
        """413 兜底：当前回合图片全部转引用；历史图本来已是引用。"""
        if self._image_mode() != "current_turn":
            return 0
        return self._demote_current_turn_images(max_body_size=0)

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
        for tool in self.runner.tools:
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

        # 参数 JSON 解析失败（响应在参数中途被截断 / 模型输出非法 JSON）：
        # 直接报「缺必填参数」会让模型以为是自己漏了字段而原样重发，
        # 给出可行动的提示（拆分调用）才能让它真正绕开截断。
        # 截断内容一律不执行：半截的 write 会写出不符合预期的文件，
        # 半截的 bash/edit 更危险；宁可这一步失败，让模型拆小重发。
        if isinstance(input_data, dict) and input_data.get("_parse_error"):
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": (
                    f"Error: '{name}' 的参数不是合法 JSON，响应在生成参数途中被截断，"
                    "本次调用未执行（文件未被写入或修改）。\n"
                    "不要把截断内容当作完整内容重发；请把内容拆小后重试："
                    "先写入开头部分，再用 edit 分批补齐剩余内容。"
                ),
                "is_error": True,
                "_meta": {"tool_name": name},
            }

        logger.debug(f"[工具调用] {name}")

        if _tool_flag(tool, "concurrency_safe") and not _tool_flag(tool, "streams_output"):
            return self._execute_tool_impl(
                name, tool, input_data, tool_use_id
            )
        with getattr(tool, "_exec_lock", _FALLBACK_EXEC_LOCK):
            return self._execute_tool_impl(
                name, tool, input_data, tool_use_id
            )

    def _execute_tool_impl(
        self,
        name: str,
        tool: Tool,
        input_data: dict,
        tool_use_id: str,
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
        if self.runner.session_dir and output_filename and not _tool_flag(tool, "placeholder_output"):
            output_file_path = str(self.runner.session_dir / output_filename)
            # 仅流式工具（bash/python）需要实例属性实时写 + 前端轮询文件；
            # 并发安全工具（纯读）execute 不读 output_file，不设实例属性防互覆。
            if _tool_flag(tool, "streams_output"):
                tool.output_file = output_file_path

                # Create empty file only for streaming tools (signals tool is running)
                try:
                    with open(output_file_path, "w", encoding="utf-8") as f:
                        f.write("")
                except Exception:
                    pass
                # 全局事件流：流式工具的实时输出增量 → sink.on_tool_output
                if self.runner.sink.on_tool_output:
                    tool.on_output = (
                        lambda chunk, offset, _n=name, _id=tool_use_id:
                        self.runner.sink.on_tool_output(_n, chunk, offset, _id)
                    )
                else:
                    tool.on_output = None

        # Notify callback
        self.runner.sink.on_tool_call(name, input_data, tool_use_id)

        # Execute tool
        start_time = time.perf_counter()
        result = ToolResult("Error: tool execution was interrupted", error=True)

        try:
            input_data = tool.coerce_input(input_data)
            # coerce_input 可能返回 ToolResult（参数校验失败）
            if isinstance(input_data, ToolResult):
                result = input_data
            else:
                # agent/handoff snapshots need the current tool_use_id to close the snapshot pair
                if name in ("handoff", "agent"):
                    tool.current_tool_use_id = tool_use_id
                result = tool.execute(**input_data)
        except Exception as e:
            import traceback
            logger.error(f"Tool '{name}' raised exception:\n{traceback.format_exc()}")
            result = ToolResult(f"Error executing tool: {e}", error=True)
        finally:
            # Only save external file for streaming tools
            # Non-streaming tools save lazily based on output size (see below)
            if _tool_flag(tool, "streams_output"):
                tool.save_output_to_file(result)
                # 更新 _output_path 为实际保存的文件路径（可能是 .json）
                output_filename = os.path.basename(tool.output_file) if tool.output_file else output_filename
            tool.output_file = None
            tool.on_output = None

        # 空输出兜底：任何工具返回空内容时（竞态、空结果、无输出），统一补非空
        # 哨兵，避免实时流显示空白、会话重载被 hydration 误标为"[工具输出文件路径缺失]"
        if not result.output:
            # 注意：不要在此处局部 import TextBlock —— 局部导入会把它变成整个
            # 函数的局部名，模块级导入被遮蔽；result.output 非空时该分支不执行，
            # 后面的限量/去重分支再引用 TextBlock 就抛 UnboundLocalError。
            result.blocks.append(TextBlock(text="(no output)"))

        elapsed = time.perf_counter() - start_time

        # Log result
        output_preview = result.output
        if len(output_preview) > 500:
            output_preview = output_preview[:500] + f"\n... ({len(result.output)} chars total)"
        status = "失败" if result.error else "完成"
        logger.debug(f"[工具结果] {name} {status} ({elapsed:.2f}s)")

        # Notify callback
        self.runner.sink.on_tool_result(name, output_preview, result.error, tool_use_id)

        # Build _meta with internal fields
        file_size = len(result.output.encode('utf-8', errors='replace'))
        truncated = file_size > _LARGE_OUTPUT_THRESHOLD

        # Check if multimodal (has image blocks)
        from core.llm.types import ImageBlock
        is_multimodal = any(isinstance(block, ImageBlock) for block in result.blocks)

        image_cfg = getattr(getattr(self.runner, "config", None), "images", None)
        max_dimension = int(getattr(image_cfg, "max_dimension", 4096) or 4096)
        request_target = int(
            getattr(image_cfg, "request_image_max_bytes", DEFAULT_MAX_BYTES)
            or DEFAULT_MAX_BYTES
        )
        # DeepSeek 单图编码目标 2 MiB；不允许超过 image_pipeline 的默认目标
        raw_target = max(1, min(request_target, DEFAULT_MAX_BYTES))
        jpeg_quality = int(getattr(image_cfg, "jpeg_quality", 85) or 85)
        per_result_limit = max(
            1, int(getattr(image_cfg, "max_images_per_tool_result", 8) or 8)
        )
        dedupe_enabled = bool(getattr(image_cfg, "dedupe_inline_images", True))

        # 工具图归一化（单一收口点）：read/PDF 渲染等返回的 ImageBlock
        # 可能带着原始分辨率与体积。必须在这里就地改写，否则写进 .json 的
        # 原图会在下一轮请求里原样发给服务端（多图/大图直接 400）。
        normalized_images: dict[int, tuple[bytes, str, int | None, int | None]] = {}
        image_digests: dict[int, str] = {}
        if is_multimodal:
            for block_idx, result_block in enumerate(result.blocks):
                if not isinstance(result_block, ImageBlock):
                    continue
                info = normalize_image_block(
                    result_block,
                    max_dimension=max_dimension,
                    max_bytes=raw_target,
                    jpeg_quality=jpeg_quality,
                )
                if info is not None:
                    normalized_images[block_idx] = info
                    image_digests[block_idx] = hashlib.sha256(info[0]).hexdigest()

        notes: list[str] = []

        # 重读去重：同一内容已内联且未退役 → 不再新增图片块
        image_index_map = getattr(self.runner, "_turn_image_index", None)
        retired_carriers = getattr(self.runner, "_retired_image_carriers", set())
        if dedupe_enabled and isinstance(image_index_map, dict):
            for block_idx, result_block in enumerate(result.blocks):
                if not isinstance(result_block, ImageBlock):
                    continue
                digest = image_digests.get(block_idx)
                if not digest:
                    continue
                existing = image_index_map.get(digest)
                if existing and existing not in retired_carriers:
                    result.blocks[block_idx] = TextBlock(
                        text="[image already in context; identical image not re-attached]"
                    )
                    normalized_images.pop(block_idx, None)
                    image_digests.pop(block_idx, None)
                    notes.append("[image already in context]")

        # 单批工具结果上限：保留前 N 张，其余写 sidecar 后转引用
        image_blocks = [
            idx for idx, block in enumerate(result.blocks)
            if isinstance(block, ImageBlock)
        ]
        image_refs: list[dict] = []
        kept_indices: set[int] = set()
        if image_blocks:
            for image_index, block_idx in enumerate(image_blocks):
                result_block = result.blocks[block_idx]
                info = normalized_images.get(block_idx)
                ref = None
                if self.runner.session_dir:
                    ref = write_tool_image_sidecar(
                        self.runner.session_dir,
                        Path(self.runner.cwd),
                        tool_use_id,
                        image_index,
                        result_block,
                        payload=info[0] if info else None,
                        mime=info[1] if info else None,
                        width=info[2] if info else None,
                        height=info[3] if info else None,
                    )
                if image_index < per_result_limit:
                    kept_indices.add(block_idx)
                    if ref:
                        image_refs.append({"index": len(image_refs), **ref})
                else:
                    path = ref["path"] if ref else ""
                    if path:
                        text = (
                            f'[image omitted from this tool result: {path}; '
                            f'use read("{path}") to view]'
                        )
                    else:
                        text = "[image omitted from this tool result]"
                    result.blocks[block_idx] = TextBlock(text=text)
                    notes.append(text)

        # 登记本回合内联图片摘要，供重读去重
        if dedupe_enabled:
            if not isinstance(image_index_map, dict):
                image_index_map = {}
                setattr(self.runner, "_turn_image_index", image_index_map)
            for block_idx in kept_indices:
                digest = image_digests.get(block_idx)
                if digest:
                    image_index_map[digest] = tool_use_id

        if notes:
            # 多模态路径发送 blocks（含 TextBlock）；纯文本路径发送 output，两处都补。
            # output 是只读 property（由 blocks 推导），只能追加块，不能赋值。
            result.blocks.append(TextBlock(text="\n".join(notes)))

        is_multimodal = any(isinstance(block, ImageBlock) for block in result.blocks)

        # Only save external file when needed (streaming already saved, skip)
        needs_external_file = truncated or is_multimodal
        if (needs_external_file
                and not _tool_flag(tool, "streams_output")
                and not _tool_flag(tool, "placeholder_output")):
            # 显式路径调用 save_output_to_file，避免并行时实例属性互覆
            save_path = str(self.runner.session_dir / output_filename) if self.runner.session_dir else None
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
        # 内联 content 仅适用于纯文本结果：多模态结果（含图片）必须留空，
        # 由 _resolve_tool_results 从 .json 读回完整块列表 —— 否则带文本的图文结果
        # 会因 content 非空被跳过，图片静默丢失
        inline_content = result.output if not truncated and not is_multimodal else None
        result_dict = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": inline_content,
            "is_error": result.error,
        }
        result_dict.setdefault("_meta", {})["turn_id"] = getattr(self.runner, "current_turn_id", "")
        if image_refs:
            result_dict["_meta"]["image_refs"] = image_refs

        # 占位工具（ask_user/审批）待答标记：专用键，不再与工具完成态混用
        if result.completed is False:
            if "_meta" not in result_dict:
                result_dict["_meta"] = {}
            result_dict["_meta"]["ask_user_completed"] = False

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
        if not needs_external_file and self.runner.session_dir and output_filename:
            orphan_path = self.runner.session_dir / output_filename
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

        Uses new _meta format: _meta.output_path, etc.
        Backward compatible with old _output_path format.
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
                output_path = block_meta.get("output_path", "")
                file_size = block_meta.get("file_size", 0)
                truncated = block_meta.get("truncated", False)

                # Read from external file
                if not output_path or not self.runner.session_dir:
                    block["content"] = "[工具输出文件路径缺失]"
                    continue

                file_path = (self.runner.session_dir / output_path).resolve()
                # 防御纵深：output_path 来自会话文件 _meta，篡改可能穿越 session 目录 → 拒绝。
                # 正常路径创建时已被 _safe_output_filename 消毒，此处与 routes_workspace 的
                # W10 校验对齐，双保险。
                if not file_path.is_relative_to(self.runner.session_dir.resolve()):
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
                            from core.llm.types import block_from_dict
                            refs = block_meta.get("image_refs") or []
                            content_blocks = []
                            image_index = 0
                            for block_data in data.get("blocks", []):
                                try:
                                    parsed = block_from_dict(block_data)
                                    bd = parsed.to_dict() if hasattr(parsed, "to_dict") else parsed
                                except Exception:
                                    continue
                                if isinstance(bd, dict) and bd.get("type") == "image":
                                    ref = refs[image_index] if isinstance(refs, list) and image_index < len(refs) else {}
                                    if self._image_mode() == "current_turn" and (
                                        str(block_meta.get("turn_id") or "") != self._effective_turn_id()
                                    ):
                                        bd = {"type": "text", "text": image_ref_text(bd, block_meta, image_index)}
                                    elif isinstance(ref, dict):
                                        bd.setdefault("_meta", {}).update(ref)
                                    image_index += 1
                                content_blocks.append(bd)
                            block["content"] = content_blocks
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
                            truncated_content = truncate_middle(file_content, 8000)
                            guide = (
                                f"\n\n---\n"
                                f"[提示] 工具输出过长（{file_size:,} 字符），已截断显示。"
                                f'如需查看完整内容，请调用 read_tool_result('
                                f'tool_use_id="{block.get("tool_use_id", "")}", offset=8000) '
                                f"继续分段读取；完整输出也保存在文件: {file_path}。"
                            )
                            block["content"] = truncated_content + guide
                        else:
                            block["content"] = truncate_result(file_content, Tool.MAX_TOOL_RESULT_SIZE_CHARS)

                except Exception as e:
                    block["content"] = f"[读取工具输出失败: {e}]"
                    continue

        return messages

    def _strip_meta_from_messages(self, messages: list[dict]) -> list[dict]:
        """Strip internal _meta fields from messages before sending to LLM API.

        Recursively strips nested ``tool_result`` content as well, so image path
        metadata attached for projection never reaches the provider.
        """
        def strip_block_meta(block: Any) -> None:
            if not isinstance(block, dict):
                return
            if "_meta" in block:
                stripped = {k: v for k, v in block["_meta"].items() if k not in INTERNAL_META}
                if stripped:
                    block["_meta"] = stripped
                else:
                    del block["_meta"]
            nested = block.get("content")
            if isinstance(nested, list):
                for sub in nested:
                    strip_block_meta(sub)

        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    strip_block_meta(block)
            if "_meta" in msg:
                stripped = {k: v for k, v in msg["_meta"].items() if k not in INTERNAL_META}
                if stripped:
                    msg["_meta"] = stripped
                else:
                    del msg["_meta"]
        return messages

    # ========== 压缩 ==========

    def _record_compaction_event(self, content: str, event: dict) -> None:
        """把 L1/L3 压缩记为仅 UI 可见的事件行（有 SessionStore 时）。"""
        session = getattr(self.runner, "session", None)
        append = getattr(session, "append_ui_event", None)
        if not callable(append):
            return
        try:
            append(content, event)
        except Exception as exc:  # noqa: BLE001 —— 记录失败不影响压缩主流程
            logger.warning("[上下文] 记录压缩事件失败: %s", exc)

    def _all_index_for_valid_index(self, valid_index: int) -> int:
        """有效消息下标 → all_messages 下标（用于把摘要插到保留消息之前）。"""
        cursor = 0
        for idx, msg in enumerate(self.runner.messages):
            if not self.runner.context.is_message_in_context(msg):
                continue
            if cursor == valid_index:
                return idx
            cursor += 1
        return len(self.runner.messages)

    def _compact_keep_tokens(self) -> int:
        max_tokens = int(getattr(self.runner.model, "max_context_tokens", 0) or 0)
        if max_tokens <= 0:
            return 0
        budget = int(max_tokens * COMPACT_KEEP_TOKEN_RATIO)
        return max(COMPACT_KEEP_MIN_TOKENS, min(budget, COMPACT_KEEP_MAX_TOKENS))

    def _prompt_overhead_tokens(self) -> int:
        """系统提示词 + 工具定义的估算 token；缺构建能力时返回 0。"""
        try:
            from core.compression import count_prompt_overhead

            build = getattr(self.runner, "_build_system_prompt", None)
            if not callable(build):
                return 0
            system_text = "\n".join(build())
            return count_prompt_overhead(
                system_text, getattr(self.runner, "tool_schemas", None)
            )["overhead_tokens"]
        except Exception:
            return 0

    def _context_ratio(self, messages: list[dict]) -> tuple[int, int, float]:
        """完整请求估算 token / 模型工作预算，返回 (tokens, max_tokens, ratio)。"""
        max_tokens = int(getattr(self.runner.model, "max_context_tokens", 0) or 0)
        if max_tokens <= 0:
            return 0, 0, 0.0
        tokens = (
            self.runner._count_messages_tokens(messages)
            + self._prompt_overhead_tokens()
        )
        return tokens, max_tokens, tokens / max_tokens

    def _last_user_input_message(self) -> tuple[int, str]:
        """最近一次真实用户输入（all_messages 下标 + 文本）；无则 (-1, "")。

        跳过工具结果、摘要、循环警告、后台通知等合成消息——它们是本轮中途产物，
        不是「本轮初始用户输入」。
        """
        from core.handoff import user_input_text

        skip_meta = {
            "summary", "loop_warning", "context_warning",
            "background_notification", "budget", "error_notice",
        }
        for idx in range(len(self.runner.messages) - 1, -1, -1):
            msg = self.runner.messages[idx]
            if not self.runner.context.is_message_in_context(msg):
                continue
            if msg.get("role") != "user":
                continue
            meta = msg.get("_meta") or {}
            if any(meta.get(key) for key in skip_meta):
                continue
            content = msg.get("content")
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                continue
            text = user_input_text(content)
            if text.strip():
                return idx, text
        return -1, ""

    def _last_user_input_text(self) -> str:
        return self._last_user_input_message()[1]

    def _enforce_context_pressure(self, messages: list[dict]) -> bool:
        """回合中途的上下文压力处理；返回消息是否被修改（需要重新投影）。

        - ≥100%：注入一条 user 收尾提示（每回合一次），让模型尽快结束；
        - ≥150%：只压缩当前轮之前的上下文（以当前轮初始用户输入为切分点），
          当前轮中途消息全部保留，压缩后继续执行，不重跑。

        常规 80% 压缩仍由 maybe_full_compact_for_user_input 在用户新输入前执行。
        """
        tokens, max_tokens, ratio = self._context_ratio(messages)
        if max_tokens <= 0 or ratio < CONTEXT_WARN_RATIO:
            self.runner._context_warning_issued = False
            return False

        logger.info(f"[上下文] 压力检查: {tokens:,} / {max_tokens:,} ({ratio:.0%})")

        if ratio >= CONTEXT_FORCE_COMPACT_RATIO:
            turn_idx, user_text = self._last_user_input_message()
            turn_valid_idx = (
                sum(
                    1 for msg in self.runner.messages[:turn_idx]
                    if self.runner.context.is_message_in_context(msg)
                )
                if turn_idx >= 0 else 0
            )
            if turn_valid_idx <= 0:
                logger.warning("[上下文] 当前轮之前没有可压缩消息，跳过强制压缩")
            else:
                logger.warning(
                    f"[上下文] 超过强制压缩阈值 ({ratio:.0%} ≥ "
                    f"{CONTEXT_FORCE_COMPACT_RATIO:.0%})，压缩当前轮之前的上下文"
                )
                # 只总结当前轮之前的 {turn_valid_idx} 条消息；本轮初始输入及其后的
                # 中途消息全部保留，压缩后继续执行，不重跑。
                instruction = self._SUMMARY_INSTRUCTION_TEMPLATE.format(
                    split_idx=turn_valid_idx
                )
                if user_text:
                    instruction += (
                        "\n\n## 当前任务（保留，不要总结）\n"
                        f"{user_text}\n"
                    )
                try:
                    before, after = self._perform_full_compact(
                        KEEP_USER_MESSAGES,
                        reason=f"上下文超过 {int(CONTEXT_FORCE_COMPACT_RATIO * 100)}% 强制压缩",
                        split_idx=turn_valid_idx,
                        insert_before=turn_idx,
                        instruction=instruction,
                    )
                except Exception as e:  # noqa: BLE001 —— 压缩失败不阻断本轮
                    logger.warning(f"[上下文] 强制压缩失败: {e}")
                else:
                    if after < before:
                        self.runner.cache_state.reset_after_compact()
                        self.runner.cache_state.on_compression(2)
                        return True
                    logger.warning("[上下文] 强制压缩未生效（消息不足或摘要失败）")
            # 压缩未生效 → 至少注入收尾提示

        if getattr(self.runner, "_context_warning_issued", False):
            return False
        self.runner._context_warning_issued = True
        self.runner.add_message(
            "user",
            f"当前上下文已超过模型工作预算（{ratio:.0%}，{tokens:,} / "
            f"{max_tokens:,} tokens）。请尽快收尾：完成当前操作，"
            "不要再开启新任务，尽快给出最终答复。",
            meta={"context_warning": True},
        )
        logger.warning(
            f"[上下文] 注入收尾提示 ({ratio:.0%} ≥ {CONTEXT_WARN_RATIO:.0%})"
        )
        return True

    def _check_and_compress(self) -> None:
        """3-layer compression before LLM call.

        Layer 1: Microcompact (mark orphan tool_results and old error pairs)
        Layer 2: 常规压缩推迟到下一轮用户输入前（maybe_full_compact_for_user_input）；
                 回合中途只做压力保护（100% 收尾提示 / 150% 压缩当前轮之前的上下文）
        Layer 3: Emergency body size (replace old images with placeholders)
        """
        from core.compression import (
            microcompact_mark_orphans_and_errors,
        )

        # 10MB：仅用于 full 模式的应急降级。current_turn 模式的图片由请求级
        # 载体预算（apply_image_budget）负责，L3 不再重复降级，否则会把预算
        # 刚保留下来的图片再次砍掉。
        MAX_BODY_SIZE = 10_000_000

        # Layer 1: Microcompact
        invalidated = microcompact_mark_orphans_and_errors(self.runner.messages)
        if invalidated > 0:
            logger.debug(f"[Microcompact] 标记 {invalidated} 条消息为无效")
            self.runner._invalidate_message_cache()
            self.runner.cache_state.on_compression(1)
            self._record_compaction_event(
                f"[上下文压缩] L1 轻量压缩：标记 {invalidated} 条消息无效",
                {"layer": 1, "invalidated": invalidated},
            )

        # Layer 2 常规压缩推迟到下一轮用户输入前（maybe_full_compact_for_user_input）；
        # 回合中途只做压力保护：≥100% 收尾提示，≥150% 强制全压缩。
        messages = self.build_projected_messages()
        if self._enforce_context_pressure(messages):
            messages = self.build_projected_messages()

        # Layer 3: Emergency body size (only images)
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
                elif btype in ("tool_result_image", "image"):
                    image_size += len(data)
            logger.debug(f"[上下文] 大小分布: 文本={text_size:,}B, 图片={image_size:,}B, 工具={tool_size:,}B")

            logger.info("[上下文] 请求体过大，正在降级图片为引用...")
            if self._image_mode() == "current_turn":
                # current_turn：数量/字节预算已在上一步执行；这里只记录，不重复降级
                logger.debug("[上下文] current_turn 模式由请求级图片预算处理，跳过 L3")
            else:
                saved = self._mark_old_images_invalid(max_body_size=MAX_BODY_SIZE)
                if saved > 0:
                    logger.info(f"[上下文] 图片替换完成，节省 {saved} 字节")
                    self.runner.cache_state.on_compression(3)
                    self._record_compaction_event(
                        f"[上下文压缩] L3 紧急压缩：替换旧图片，节省 {saved} 字节",
                        {"layer": 3, "bytes_saved": saved},
                    )

        # 上述各层压缩都可能原地修改 self.messages，统一失效 valid 缓存
        self.runner._invalidate_message_cache()

    def _perform_full_compact(
        self,
        keep_user_messages: int,
        *,
        next_steps: str | None = None,
        reason: str = "",
        split_idx: int | None = None,
        insert_before: int | None = None,
        keep_tokens: int | None = None,
        instruction: str | None = None,
    ) -> tuple[int, int]:
        """Full auto compact: summarize old messages, keep recent user messages.

        ``split_idx`` 可显式指定切分点（相对有效消息）；``insert_before`` 可把摘要
        插入到 all_messages 的指定位置（紧急压缩时插到当前轮之前）。默认追加到末尾。

        摘要复用主对话的 system+tools+messages prefix，末尾追加摘要指令。
        Anthropic cache 按 prefix 匹配，前面全部 cache_read，只有末尾指令是 cache_write。
        """

        all_messages = self.runner.messages
        total_tokens = self.runner._count_messages_tokens(self.build_projected_messages(force_reference=True))

        # Find split point
        valid_messages = self.runner.get_valid_messages()
        if split_idx is None:
            # 显式 keep_tokens=0 表示「不按 token 预算」，退回 keep_user_messages 切分；
            # 只有未传（None）时才用模型上下文比例推导预算。
            budget = self._compact_keep_tokens() if keep_tokens is None else keep_tokens
            if budget > 0:
                split_idx = self.runner._find_split_by_token_budget(valid_messages, budget)
            else:
                split_idx = self.runner._find_split_by_user_messages(
                    valid_messages, keep_user_messages
                )
        split_idx = max(0, min(int(split_idx), len(valid_messages)))
        if split_idx <= 0:
            raise ValueError("Not enough messages to compress")

        # 必须在标记旧消息失效前计算插入位置：失效后 is_message_in_context
        # 会跳过它们，游标映射会错位到消息列表末尾。
        if insert_before is None:
            insert_before = self._all_index_for_valid_index(split_idx)

        # Summarize old messages FIRST — only invalidate them after the summary
        # succeeds. Invalidating before summarizing would permanently hide the
        # old history if the summary LLM call fails.
        old_messages = valid_messages[:split_idx]
        if not old_messages:
            # Nothing to summarize
            return total_tokens, total_tokens

        # Cache-aware 摘要：复用主对话 prefix 共享缓存
        if instruction is None:
            instruction = (
                build_handoff_document(next_steps, reason)
                if next_steps
                else None
            )
        summary = self.runner._summarize_messages(
            split_idx=split_idx, instruction=instruction
        )
        if summary.startswith("(摘要生成失败") or summary.startswith("（摘要生成失败"):
            logger.error("摘要生成失败，跳过压缩")
            return total_tokens, total_tokens

        # Mark messages before split as invalid (using _meta.valid).
        # valid_messages 是 get_valid_messages() 的保序拷贝（非引用），
        # 用游标在 all_messages 中按 valid_messages 索引对齐后标记；
        # pinned 消息永不标记失效（任务/检查提示等核心锚点）。
        # 跳过条件必须与 get_valid_messages 完全一致（共用谓词），否则游标漂移。
        pinned_positions = {
            pos for pos, msg in enumerate(valid_messages[:split_idx])
            if msg.get("_meta", {}).get("pinned")
        }
        invalidated_count = max(0, split_idx - len(pinned_positions))
        cursor = 0
        for msg in all_messages:
            if cursor >= split_idx:
                break
            if not self.runner.context.is_message_in_context(msg):
                continue
            if cursor not in pinned_positions:
                if "_meta" not in msg:
                    msg["_meta"] = {}
                msg["_meta"]["valid"] = False
            cursor += 1

        # 摘要作为 user 角色上下文写入，而不是伪造一个没有 thinking 块的 assistant 轮：
        # thinking 模式要求 assistant 消息原样回传思考块，本地合成的 assistant 摘要
        # 会触发网关 400（"content[].thinking ... must be passed back"）。
        summary_content = "[Previous conversation summary]\n\n" + summary
        insert_at = max(0, min(int(insert_before), len(self.runner.messages)))
        self.runner.insert_message(
            insert_at, "user", summary_content,
            meta={"summary": True, "persist_history": True},
        )
        summary_msg = self.runner.messages[insert_at]

        self.runner._invalidate_message_cache()

        new_tokens = self.runner._count_messages_tokens(self.runner.get_valid_messages())
        compaction_meta = {
            "layer": 2,
            "before_tokens": total_tokens,
            "after_tokens": new_tokens,
            "invalidated": invalidated_count,
            "reason": reason or "threshold",
            "keep_user_messages": keep_user_messages,
        }
        try:
            if isinstance(summary_msg, dict) and (summary_msg.get("_meta") or {}).get("summary"):
                summary_msg.setdefault("_meta", {})["compaction"] = compaction_meta
        except Exception as exc:  # noqa: BLE001
            logger.warning("[上下文] 记录完整压缩元数据失败: %s", exc)

        logger.info(f"[Full Compact] 完成: {total_tokens:,} → {new_tokens:,} tokens")
        return total_tokens, new_tokens

    # 结构化压缩模板：从对话中提取关键字段，防止压缩后丢失失败路径/验证状态/用户约束
    # 摘要指令模板：不含对话文本（模型通过 cache prefix 已看到完整对话）
    _SUMMARY_INSTRUCTION_TEMPLATE = """请总结我们对话的前 {split_idx} 条消息的内容。
按以下结构用中文总结，每节用简短条目（无内容写"无"）。不要调用任何工具，只输出总结文本。

## 当前目标
（用户的主要需求和期望结果）

## 成功标准
（怎样算完成）

## 用户约束 / 偏好
（用户明确提出的要求、风格偏好、禁止事项等）

## 已确认上下文
（已确定的事实、关键信息）

## 已完成工作
（已完成的关键操作和结论）

## 当前状态
（当前在做什么，卡在哪里）

## 不要重复的尝试
（尝试过但失败的方法，避免再次踩坑）

## 验证状态
- 已验证：（已运行的命令/测试及结果）
- 未验证：（计划但尚未执行的检查）

## 关键上下文
（相关文件、工具、链接、已做决策等）

## 未决问题
（待解决的疑问或分歧）

## 下一步
（接下来应做的具体动作，按顺序列出）"""

    def _summarize_messages(
        self,
        split_idx: int = 0,
        *,
        instruction: str | None = None,
        pending_tool_use_id: str = "",
        system_prompt: str | list[str] | None = None,
    ) -> str:
        """Cache-aware 摘要：复用主对话的 system+tools+messages prefix 共享缓存。

        ``instruction`` 为空时用压缩模板；handoff 传入已填充的指令（不再次 format）。
        ``pending_tool_use_id`` 非空时闭合快照里的当前 tool_use，不污染原消息。
        """
        if pending_tool_use_id:
            api_messages = self._prepare_messages_for_llm(
                pad_dangling=False, force_reference=True
            )
            self.pad_dangling_in_messages(
                api_messages, exclude_ids={pending_tool_use_id}
            )
            text = instruction or self._SUMMARY_INSTRUCTION_TEMPLATE.format(
                split_idx=split_idx
            )
            api_messages.append(Message(role="user", content=[
                ToolResultBlock(
                    tool_use_id=pending_tool_use_id,
                    content="[handoff: 正在生成交接文档]",
                    is_error=False,
                ),
                TextBlock(text=text),
            ]))
        else:
            api_messages = self._prepare_messages_for_llm(
                pad_dangling=True, force_reference=True
            )
            text = instruction or self._SUMMARY_INSTRUCTION_TEMPLATE.format(
                split_idx=split_idx
            )
            api_messages.append(Message(role="user", content=text))

        if system_prompt is None:
            system_prompt = self.runner._build_system_prompt()

        try:
            call_start = time.monotonic()
            response = self.runner.client.chat(
                messages=api_messages,
                system=system_prompt,
                tools=self.runner.tool_schemas,
                session_id=self.runner.master_session_id,
            )

            # Track usage
            if response.usage:
                self.runner._update_usage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    api_calls=1,
                    cache_read_tokens=response.usage.cache_read_tokens,
                    cache_creation_tokens=response.usage.cache_write_tokens,
                    duration=time.monotonic() - call_start,
                    model=response.model,
                )
                # 记录 cache 效率（仅日志）
                total_input = response.usage.input_tokens or 0
                cache_read = response.usage.cache_read_tokens or 0
                if total_input > 0:
                    logger.info(
                        f"[上下文] cache-aware 摘要: cache_read={cache_read:,} / "
                        f"total_input={total_input:,} ({cache_read/total_input:.0%} 命中)"
                    )

            return response.get_text() or "（摘要生成失败）"
        except Exception:
            logger.error("[上下文] 摘要生成失败")
            return "（摘要生成失败，请查看完整历史）"

    @staticmethod
    def pad_dangling_in_messages(
        messages: list[Message],
        exclude_ids: set[str] | None = None,
    ) -> None:
        """为快照副本里的悬挂 tool_use 补占位，可排除当前调用 id。"""
        exclude = exclude_ids or set()
        answered: set[str] = set()
        for msg in messages:
            if not isinstance(msg.content, list):
                continue
            for block in msg.content:
                if isinstance(block, ToolResultBlock) and block.tool_use_id:
                    answered.add(block.tool_use_id)

        pending: list[tuple[int, list[str]]] = []
        for idx, msg in enumerate(messages):
            if not isinstance(msg.content, list):
                continue
            ids = [
                block.id for block in msg.content
                if isinstance(block, ToolCallBlock) and block.id
                and block.id not in answered and block.id not in exclude
            ]
            if ids:
                pending.append((idx, ids))

        for idx, ids in reversed(pending):
            messages.insert(
                idx + 1,
                Message(role="user", content=[
                    ToolResultBlock(
                        tool_use_id=tool_use_id,
                        content="[interrupted]",
                        is_error=True,
                    )
                    for tool_use_id in ids
                ]),
            )

    def summarize_for_handoff(
        self,
        next_steps: str,
        reason: str = "",
        pending_tool_use_id: str = "",
    ) -> str:
        """Build a handoff document with cache-aware prefix sharing."""
        instruction = build_handoff_document(next_steps, reason)
        system_prompt = None
        if getattr(self.runner, "_mode", "interactive") == "autonomous":
            system_prompt = getattr(self.runner, "_system_prompt", None)
        return self._summarize_messages(
            instruction=instruction,
            pending_tool_use_id=pending_tool_use_id,
            system_prompt=system_prompt,
        )

    def maybe_full_compact_for_user_input(self, user_text: str) -> None:
        """Compact before a new user turn, using the input as the next task."""
        if not user_text or not user_text.strip():
            return
        if not self.runner.get_valid_messages():
            return

        from core.compression import microcompact_mark_orphans_and_errors

        invalidated = microcompact_mark_orphans_and_errors(self.runner.messages)
        if invalidated:
            self.runner._invalidate_message_cache()
            self._record_compaction_event(
                f"[上下文压缩] L1 轻量压缩：标记 {invalidated} 条消息无效",
                {"layer": 1, "invalidated": invalidated},
            )

        messages = self.build_projected_messages(force_reference=True)
        total = (
            self.runner._count_messages_tokens(messages)
            + self._prompt_overhead_tokens()
        )
        projected = total + approx_token_count(user_text)
        threshold = int(
            self.runner.model.max_context_tokens * FULL_COMPACT_TOKEN_RATIO
        )
        if projected <= threshold:
            return

        logger.info(
            f"[上下文] 用户输入触发全压缩: projected={projected:,} > {threshold:,}"
        )
        try:
            self._perform_full_compact(
                KEEP_USER_MESSAGES,
                next_steps=user_text,
                reason="用户新输入触发的全压缩",
            )
            self.runner.cache_state.reset_after_compact()
            self.runner.cache_state.on_compression(2)
        except Exception as e:
            logger.warning(f"[上下文] 用户输入全压缩失败: {e}")

    def _mark_old_images_invalid(self, max_body_size: int = 3_000_000) -> int:
        """Replace old tool_result images with text placeholders until body < max_body_size.

        Replaces images in order from oldest to newest, recalculating body size
        after each replacement. Stops when body size is under the limit or all
        images have been replaced.

        Replaces images in place (instead of invalidating whole messages) so the
        tool_use/tool_result pairing with the preceding assistant message stays
        intact — invalidating only the user message would leave the assistant's
        tool_use dangling and the API would reject the next request with 400.
        Returns the number of image bytes removed.
        """
        saved = 0

        # Collect all image references with their positions (oldest first)
        image_refs: list[tuple[int, int, int, int]] = []  # (msg_idx, block_idx, sub_idx, data_len)

        for i, msg in enumerate(self.runner.messages):
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

        if not image_refs:
            return 0

        # Replace images from oldest to newest until body < max_body_size
        placeholder_bytes = len("[image removed to reduce request size]".encode('utf-8'))

        # 初始体积只算一次；每替换一张图按已知字节数递减（估算函数本身是线性的）
        body_size = self._estimate_request_body_size(self.runner._get_messages_with_header())

        for msg_idx, block_idx, sub_idx, data_len in image_refs:
            if body_size <= max_body_size:
                break  # Done

            # Replace this image
            rc = self.runner.messages[msg_idx]["content"][block_idx]["content"]
            rc[sub_idx] = {"type": "text", "text": "[image removed to reduce request size]"}
            # Net savings = image data - placeholder text
            saved += data_len - placeholder_bytes
            body_size -= max(0, data_len - placeholder_bytes)

        return saved

    def _mark_all_images_invalid(self) -> None:
        """Replace all tool_result images with text placeholders for 413 retry.

        Replaces images in place (instead of invalidating whole messages):
        invalidating a user message here would leave the preceding assistant
        tool_use dangling, and the API would reject the next request with 400.
        """
        for msg in self.runner.messages:
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
                    elif btype == "image":
                        yield ("image", block.get("source", {}).get("data", ""))
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
        """Estimate JSON request body size in bytes (delegates to module helper)."""
        return estimate_request_body_size(messages)

    @staticmethod
    def _image_omitted_text(block: dict) -> str:
        """非多模态模型的图片降级文本；能取到路径时带上，便于模型/用户定位文件。"""
        meta = block.get("_meta")
        path = str(meta.get("path") or "") if isinstance(meta, dict) else ""
        if path:
            return f"[image not sent: model has no image input; file: {path}]"
        return "[image not sent: model has no image input]"

    def _strip_images_from_messages(self, messages: list[dict]) -> list[dict]:
        """Strip images from messages for non-multimodal models."""
        result = []
        for msg in messages:
            content = msg.get("content", "")
            if not isinstance(content, list):
                result.append(msg)
                continue

            changed = False
            new_content = []
            for block in content:
                if not isinstance(block, dict):
                    new_content.append(block)
                    continue
                if block.get("type") == "image":
                    changed = True
                    new_content.append({
                        "type": "text",
                        "text": self._image_omitted_text(block),
                    })
                    continue
                if block.get("type") != "tool_result":
                    new_content.append(block)
                    continue
                rc = block.get("content", "")
                if not isinstance(rc, list):
                    new_content.append(block)
                    continue
                new_sub = []
                for sub in rc:
                    if isinstance(sub, dict) and sub.get("type") == "image":
                        changed = True
                        new_sub.append({
                            "type": "text",
                            "text": self._image_omitted_text(sub),
                        })
                    else:
                        new_sub.append(sub)
                new_content.append({**block, "content": new_sub})
            result.append({**msg, "content": new_content} if changed else msg)
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
        force_reference: bool = False,
    ) -> list[Message]:
        """Build Message objects for the LLM API from runner.messages.

        共享的消息预处理管道（non-streaming / streaming / 413 重试 / 超时兜底）。
        ``pad_dangling``：为悬挂 tool_use 补占位（重试或超时兜底路径已修补过，跳过）。
        ``resolve_results``：从外部文件解析工具输出并剥离内部 _meta
        （non-streaming 413 重试路径首轮已解析过，跳过）。
        ``force_reference``：图片全部降级为路径引用（摘要/交接用，避免重复发送 base64）。
        """
        if pad_dangling:
            self.runner._pad_dangling_tool_results()
        messages = self.runner._get_messages_with_header()
        if resolve_results:
            messages = self._resolve_tool_results(messages)
        # 图片投影/预算依赖 _meta（turn_id/path），必须在剥离内部元数据之前执行
        if self._image_mode() == "current_turn" or force_reference:
            messages = project_images_for_request(
                messages,
                self._effective_turn_id(),
                force_reference=force_reference,
                demoted_paths=getattr(self.runner, "_demoted_image_paths", set()),
            )
        if not force_reference:
            messages = self._apply_image_budget(messages, latch=True)
        if "image" not in (self.runner.model.input or []):
            # 先降级再剥 meta：降级文本要读 _meta.path
            messages = self._strip_images_from_messages(messages)
        if resolve_results:
            messages = self._strip_meta_from_messages(messages)
        return self._convert_to_message_objects(messages)

    def _call_llm(self, streaming: bool = False, system_prompt: str | list[str] = "") -> LLMResponse:
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

    def _persist_before_retry(self) -> None:
        """413 去图重试前落盘：autonomous 写 exec 进度，interactive 写会话 schema。"""
        if getattr(self.runner, "_mode", "interactive") == "autonomous":
            self.runner._save_progress(self.runner._turn_iterations, phase="running")
        else:
            self.runner.save_messages()

    def _capture_request_prefix(self, system_prompt, response) -> None:
        """记录本次请求的 system/tools 配置与缓存统计，供建议生成原样复用（§3.2）。

        必须捕获而不是事后重算：回合结束路由会把接口重置为 None，接口专属工具与
        system 的 interface_caps 段会变，重算出来的字节与上一次请求不同会静默 miss。
        """
        try:
            client = self.runner.client
            usage = getattr(response, "usage", None)
            self.runner._last_request_prefix = {
                "system": system_prompt,
                "tools": list(self.runner.tool_schemas),
                "model": getattr(client, "model_name", ""),
                "base_url": getattr(client, "base_url", "") or "",
                "session_id": self.runner.master_session_id,
                "cache_read": int(getattr(usage, "cache_read_tokens", 0) or 0) if usage else 0,
                "cache_write": int(getattr(usage, "cache_write_tokens", 0) or 0) if usage else 0,
            }
        except Exception:  # noqa: BLE001 —— 捕获失败不影响主流程
            logger.debug("[suggest] 捕获请求前缀失败", exc_info=True)

    def _call_llm_non_streaming(self, system_prompt: str | list[str]) -> LLMResponse:
        """Non-streaming LLM call."""
        message_objects = self._prepare_messages_for_llm()

        try:
            call_start = time.monotonic()
            response = self.runner.client.chat(
                messages=message_objects,
                system=system_prompt,
                tools=self.runner.tool_schemas,
                session_id=self.runner.master_session_id,
            )
            # Track usage (UsageData object)
            if response.usage:
                self.runner._update_usage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    api_calls=1,
                    cache_read_tokens=response.usage.cache_read_tokens,
                    cache_creation_tokens=response.usage.cache_write_tokens,
                    duration=time.monotonic() - call_start,
                    model=response.model,
                )
                self.runner.cache_state.on_llm_response(
                    cache_read=response.usage.cache_read_tokens,
                    cache_write=response.usage.cache_write_tokens,
                    input_tokens=response.usage.input_tokens,
                )
            self._capture_request_prefix(system_prompt, response)
            return response
        except Exception as e:
            # 413（请求体过大）应急去图重试；图片类 400 由请求预算提前避免
            if self._is_413_error(e):
                logger.warning("[LLM] 请求体过大，正在去图重试...")
                if self._image_mode() == "current_turn":
                    self._demote_all_current_turn_images()
                else:
                    self._mark_all_images_invalid()
                self._persist_before_retry()

                # 重新解析并按需降级当前回合图片；禁止发送 content=None。
                retry_message_objects = self._prepare_messages_for_llm(
                    pad_dangling=False
                )
                try:
                    retry_start = time.monotonic()
                    response = self.runner.client.chat(
                        messages=retry_message_objects,
                        system=system_prompt,
                        tools=self.runner.tool_schemas,
                        session_id=self.runner.master_session_id,
                    )
                    if response.usage:
                        self.runner._update_usage(
                            input_tokens=response.usage.input_tokens,
                            output_tokens=response.usage.output_tokens,
                            api_calls=1,
                            cache_read_tokens=response.usage.cache_read_tokens,
                            cache_creation_tokens=response.usage.cache_write_tokens,
                            duration=time.monotonic() - retry_start,
                            model=response.model,
                        )
                        self.runner.cache_state.on_llm_response(
                            cache_read=response.usage.cache_read_tokens,
                            cache_write=response.usage.cache_write_tokens,
                            input_tokens=response.usage.input_tokens,
                        )
                    self._capture_request_prefix(system_prompt, response)
                    return response
                except Exception as retry_e:
                    err_msg = format_llm_error(retry_e, self.runner.client.base_url if self.runner.client else "")
                    logger.error(f"[LLM] {err_msg}")
                    raise RuntimeError(err_msg) from retry_e

            err_msg = format_llm_error(e, self.runner.client.base_url if self.runner.client else "")
            logger.error(f"[LLM] {err_msg}")
            raise RuntimeError(err_msg) from e

    def _call_llm_streaming(self, system_prompt: str | list[str]) -> LLMResponse:
        """Streaming LLM call. Think content passes through as-is."""
        text_parts: list[str] = []
        # 首 token 时刻（thinking/text 任一 delta 先到者），用于拆分预填充与生成时段
        first_delta_at: list[float | None] = [None]

        def on_text_delta(text: str):
            if first_delta_at[0] is None:
                first_delta_at[0] = time.monotonic()
            text_parts.append(text)
            if text:
                safe = text.encode("utf-8", errors="replace").decode("utf-8")
                self.runner.sink.on_text(safe)

        def on_thinking_delta(thinking: str):
            if first_delta_at[0] is None:
                first_delta_at[0] = time.monotonic()
            self.runner.sink.on_thinking(thinking)

        message_objects = self._prepare_messages_for_llm()

        # 重试配置：3次重试，退避 5/10/20 秒
        max_retries = 3
        retry_delays = [5, 10, 20]
        images_stripped = False
        attempt_start = time.monotonic()

        for attempt in range(max_retries + 1):
            try:
                attempt_start = time.monotonic()
                first_delta_at[0] = None
                response = self.runner.client.chat_stream(
                    messages=message_objects,
                    system=system_prompt,
                    tools=self.runner.tool_schemas,
                    on_text=on_text_delta,
                    on_thinking=on_thinking_delta,
                    stop_check=lambda: self.runner._stopped,
                    session_id=self.runner.master_session_id,
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
                    err_msg = format_llm_error(e, self.runner.client.base_url if self.runner.client else "")
                    logger.error(f"[LLM] {err_msg}")
                    raise RuntimeError(err_msg) from e

                # 413：去掉图片后重试（只做一次）
                if self._is_413_error(e) and not images_stripped:
                    logger.warning("[LLM] 请求体过大，正在去掉图片重试...")
                    if self._image_mode() == "current_turn":
                        self._demote_all_current_turn_images()
                    else:
                        self._mark_all_images_invalid()
                    self._persist_before_retry()
                    self.runner.sink.on_text(RETRY_CLEAR_SENTINEL)
                    text_parts.clear()
                    images_stripped = True

                    # 重新组装；当前模式只降级当前回合图片，历史图已是引用。
                    message_objects = self._prepare_messages_for_llm(pad_dangling=False)
                else:
                    # 统一 taxonomy（三态之二/三）：非瞬态（quota/auth/context_length/
                    # bad_request）立即放弃，不消耗剩余重试次数——原来 quota 429 也会空等 3 次。
                    info = classify_llm_error(e, self.runner.client.base_url if self.runner.client else "")
                    if not info.should_retry:
                        logger.error(f"[LLM] {info.message}")
                        raise RuntimeError(info.message) from e

                    # 瞬态错误：退避重试（尊重 Retry-After），分段 sleep 期间响应停止请求
                    delay = info.retry_after_s if info.retry_after_s is not None else retry_delays[attempt]
                    logger.warning(f"[LLM] 请求失败 ({info.kind}，尝试 {attempt + 1}/{max_retries + 1})，{delay:.0f}s 后重试")
                    for _ in range(int(delay * 10)):
                        if self.runner._stopped:
                            return LLMResponse(
                                content=[TextBlock(text="".join(text_parts))],
                                stop_reason="stopped",
                            )
                        time.sleep(0.1)
                    text_parts.clear()
                    self.runner.sink.on_text(RETRY_CLEAR_SENTINEL)

        # Track usage (UsageData object)
        if response.usage:
            call_duration = time.monotonic() - attempt_start
            ttft = None
            if first_delta_at[0] is not None:
                ttft = first_delta_at[0] - attempt_start
            self.runner._update_usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                api_calls=1,
                cache_read_tokens=response.usage.cache_read_tokens,
                cache_creation_tokens=response.usage.cache_write_tokens,
                duration=call_duration,
                ttft=ttft,
            )
            self.runner.cache_state.on_llm_response(
                cache_read=response.usage.cache_read_tokens,
                cache_write=response.usage.cache_write_tokens,
                input_tokens=response.usage.input_tokens,
            )

        self._capture_request_prefix(system_prompt, response)
        return response

    @staticmethod
    def _is_413_error(e: Exception) -> bool:
        """Check if exception is a 413 Entity Too Large error."""
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 413:
            return True
        # Fallback for wrapped exceptions。用词边界匹配，避免 "toolu_413..." 之类
        # 的 id 子串被误判成 413（会触发无谓的去图压缩）
        text = str(e)
        return re.search(r"\b413\b", text) is not None or "Entity Too Large" in text
