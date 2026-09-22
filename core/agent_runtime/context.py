"""AgentContext - agent 消息状态层（Step 2 抽取）。

从 BaseAgent 抽出的纯消息逻辑：messages 所有权、序列化、pad、invalidate、
usage 存储。不持有 LLM client / tools / 回调 —— 这些属 Runner 与 Loop。

交互模式下 `messages` 与 `session_manager.messages` 共享同一引用，
本层只做原地修改，持久化时机（flush/checkpoint）由调用方决策。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from core.fs_utils import atomic_write_json
from core.session import generate_short_id

logger = logging.getLogger(__name__)

# 内部元数据字段：序列化/送 LLM 前剥除，不发给 API
INTERNAL_META = {
    "id", "valid", "compacted", "output_path", "file_size", "truncated",
    "tool_name", "multimodal", "completed", "answered", "exec_id", "seq",
    "summary", "error_notice",
}


class AgentContext:
    """消息状态与序列化逻辑，与执行层（Runner/Loop）解耦。"""

    def __init__(
        self,
        messages: list[dict] | None = None,
        session_manager: Any = None,
        session_dir: Path | None = None,
    ):
        self.messages: list[dict] = messages if messages is not None else []
        # 可空：interactive 由 Agent 注入 SessionManager，autonomous/直连 BaseAgent 为 None
        self.session_manager = session_manager
        self.session_dir = session_dir
        self._usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "api_calls": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }

    def set_session_manager(self, session_manager: Any) -> None:
        """会话切换等重绑 session_manager 时同步，保持本层引用与 Agent 一致。"""
        self.session_manager = session_manager

    # ─── 消息写入 ─────────────────────────────────────────────────

    def add_message(self, role: str, content: Any, meta: dict | None = None) -> None:
        """追加一条消息；交互模式下仅置脏，落盘由迭代/回合边界的 flush()/save() 批量完成。

        消息引用与 session_manager.messages 共享（同一 list），mark_dirty 递增版本号，
        jsonl 追加推迟到 _interactive_tool_batch 末尾 flush / 回合退出点 save，
        崩溃窗口从"消息级"放宽到"迭代级"（工具输出内容已外置文件不丢）。
        """
        meta = dict(meta) if meta else {}
        if "id" not in meta:
            meta["id"] = generate_short_id()
        msg = {"role": role, "content": content, "_meta": meta}
        self.messages.append(msg)
        if self.session_manager is not None:
            self.session_manager.mark_dirty()

    def invalidate_all_messages(self) -> int:
        """标记全部消息无效（_meta.valid=False）。返回标记数。"""
        count = 0
        for msg in self.messages:
            if msg.get("_meta", {}).get("valid") is not False:
                msg.setdefault("_meta", {})["valid"] = False
                count += 1
        return count

    # ─── 序列化 ──────────────────────────────────────────────────

    def get_valid_messages(self, strip_meta: bool = True) -> list[dict]:
        """过滤无效消息；strip_meta=True 时剥除内部 _meta 字段。

        Thinking 块保留（Anthropic 多轮上下文要求）；_meta.compacted 在此保留，
        序列化阶段再过滤。strip_meta=False 供 _resolve_tool_results 等中间处理。
        """
        result = []

        for msg in self.messages:
            meta = msg.get("_meta", {})
            if meta.get("valid") is False:
                continue
            if meta.get("error_notice"):
                continue  # 仅 UI 展示的系统错误通知，不进入 LLM 上下文

            role = msg.get("role")
            content = msg.get("content", "")

            if not isinstance(content, list):
                if strip_meta:
                    clean_msg = {"role": role, "content": content}
                    if meta:
                        stripped_meta = {k: v for k, v in meta.items() if k not in INTERNAL_META}
                        if stripped_meta:
                            clean_msg["_meta"] = stripped_meta
                else:
                    clean_msg = dict(msg)
                result.append(clean_msg)
                continue

            clean_blocks = []
            for block in content:
                clean_block = dict(block)
                if strip_meta and "_meta" in clean_block:
                    stripped_block_meta = {
                        k: v for k, v in clean_block["_meta"].items() if k not in INTERNAL_META
                    }
                    if stripped_block_meta:
                        clean_block["_meta"] = stripped_block_meta
                    else:
                        del clean_block["_meta"]
                clean_blocks.append(clean_block)

            if clean_blocks:
                clean_msg = {"role": role, "content": clean_blocks}
                if strip_meta:
                    if meta:
                        stripped_meta = {k: v for k, v in meta.items() if k not in INTERNAL_META}
                        if stripped_meta:
                            clean_msg["_meta"] = stripped_meta
                else:
                    if meta:
                        clean_msg["_meta"] = dict(meta)
                result.append(clean_msg)

        return result

    def get_messages_with_header(self) -> list[dict]:
        """供 LLM 调用的有效消息（保留 _meta，由 Runner 后续剥除）。"""
        return self.get_valid_messages(strip_meta=False)

    def count_messages_tokens(self, messages: list[dict]) -> int:
        """统计消息 token 数（委托 core.compression）。"""
        from core.compression import count_messages_tokens
        return count_messages_tokens(messages)

    @staticmethod
    def find_split_by_user_messages(messages: list[dict], keep_user_count: int) -> int:
        """Find split point keeping last N user messages.

        优先按纯文本 user 消息（交互模式语义：保留最近 N 轮用户提问）；
        worker/lite autonomous 模式的 user 消息多为 pinned string（任务/检查提示）
        或 list content（tool_result、预算提示），纯文本非 pinned 可能为零，
        此时退回按「全部非 pinned user 消息」切分，否则 full compact 永不触发、
        长任务上下文无限增长。
        """
        user_text_indices = []
        user_any_indices = []
        for i, msg in enumerate(messages):
            if msg.get("_meta", {}).get("pinned"):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            user_any_indices.append(i)
            if isinstance(content, str):
                user_text_indices.append(i)

        if len(user_text_indices) > keep_user_count:
            split_idx = user_text_indices[-keep_user_count]
        elif len(user_any_indices) > keep_user_count:
            split_idx = user_any_indices[-keep_user_count]
        else:
            return 0

        # Don't tear a tool round in half: a split is invalid if the last kept
        # message is an assistant tool_use (its tool_result would be discarded)
        # or the first kept message is a user tool_result (its tool_use would be
        # discarded). 回退到轮起点（assistant tool_use）为止，而不是一路退回 0——
        # 否则 worker 的全链式历史（全是 tool_use/tool_result 轮）永远切不动。
        while split_idx > 0:
            prev = messages[split_idx - 1]
            prev_content = prev.get("content", [])
            if prev.get("role") == "assistant" and isinstance(prev_content, list) and any(
                b.get("type") == "tool_use" for b in prev_content
            ):
                split_idx -= 1
                continue
            msg = messages[split_idx]
            content = msg.get("content", [])
            if msg.get("role") == "user" and isinstance(content, list) and any(
                b.get("type") == "tool_result" for b in content
            ):
                split_idx -= 1
                continue
            break
        return split_idx

    # ─── 持久化 ──────────────────────────────────────────────────

    def save_messages(self, metadata: dict | None = None, session_id: str = "") -> None:
        """保存消息到磁盘。

        interactive 统一由 SessionManager 按 3 文件布局持久化（commits 视图）；
        无 session_manager（worker/lite / 直连 BaseAgent）走旧 index.json 格式。
        """
        if self.session_manager is not None:
            self.session_manager.save()
            return

        if not self.session_dir:
            return

        self.session_dir.mkdir(parents=True, exist_ok=True)
        session_file = self.session_dir / "index.json"

        existing: dict = {}
        if session_file.exists():
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = {}

        data = {
            "session_id": session_id or self.session_dir.name,
            "messages": self.messages,
            "name": existing.get("name", ""),
            "metadata": metadata if metadata is not None else existing.get("metadata", {}),
        }

        try:
            atomic_write_json(session_file, data)
        except Exception as e:
            logger.error(f"Failed to save messages: {e}")

    def load_messages(self) -> bool:
        """从 session_dir/index.json 加载消息。文件不存在返回 False。"""
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

    def invalidate_message_cache(self) -> None:
        """压缩等原地修改 messages 后，失效 session_manager 的 valid 缓存。

        interactive 下 messages 与 session_manager.messages 共享同一引用，
        压缩直接改 block/_meta 不经过 add_message，必须手动 mark_dirty，
        否则 web_api 的 token 估算一直拿到过期快照（C1）。
        """
        if self.session_manager is not None:
            self.session_manager.mark_dirty()

    def pad_dangling_tool_results(self) -> None:
        """为悬挂的 tool_use 补充占位 tool_result（原地修改 messages）。

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

    # ─── usage ───────────────────────────────────────────────────

    def update_usage(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        api_calls: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> None:
        """累计 usage 统计。"""
        self._usage["input_tokens"] += input_tokens
        self._usage["output_tokens"] += output_tokens
        self._usage["api_calls"] += api_calls
        self._usage["cache_read_tokens"] += cache_read_tokens
        self._usage["cache_creation_tokens"] += cache_creation_tokens

    def get_usage(self) -> dict[str, int]:
        """返回 usage 快照拷贝。"""
        return self._usage.copy()

    def sync_to_session_manager(self) -> None:
        """同步 metadata/usage 到 session_manager（无 sm 时为空操作）。"""
        if self.session_manager is None:
            return
        self.session_manager.metadata["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.session_manager.metadata["usage"] = self._usage.copy()
        self.session_manager.mark_dirty()
