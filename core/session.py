"""Session management - independent of LLM client.

Directory structure:
  data/workspace/{uuid}/sessions/
    {short_id}/               # 每个 session 一个目录（8 位十六进制）
      index.json              # 主会话数据
      {tool_use_id}.txt       # RootAgent 工具输出（实时流式写入，供前端轮询）
      exec_{id}/              # 每个 SubAgent 一个子目录
        index.json            # SubAgent 执行日志
        {tool_use_id}.txt     # SubAgent 工具输出
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def generate_short_id() -> str:
    """生成 8 位十六进制短 ID。"""
    return secrets.token_hex(4)


class SessionManager:
    """独立管理会话数据，与 LLMClient 解耦。

    负责：
    - 消息管理（add, get, clear）
    - 持久化（load, save, delete）
    - 有效消息过滤（get_valid_messages）
    - 压缩逻辑
    - 使用量追踪
    - SubAgent 执行日志管理

    Session 格式使用 Anthropic 格式，内部字段统一放入 _meta: {}。
    """

    # _meta 中的内部字段（发送到 API 前剥离）
    _INTERNAL_META_FIELDS = frozenset({"valid", "compacted", "output_path", "file_size", "truncated", "tool_name", "multimodal"})

    def __init__(self, session_id: str, sessions_dir: Path):
        self.session_id = session_id
        # sessions_dir 是工作区级别的 sessions 目录
        self.sessions_dir = Path(sessions_dir)
        # session_dir 是当前 session 的目录
        self.session_dir = self.sessions_dir / session_id

        self.messages: list[dict] = []
        self._valid_messages_cache: list[dict] | None = None
        self._messages_dirty: bool = True
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.metadata: dict = {
            "created_at": now,
            "updated_at": now,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "api_calls": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            },
            "subagent_count": 0,
        }
        self.name: str = "New Session"

        # 确保目录存在
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)

    # ========== 消息管理 ==========

    def add_message(self, role: str, content: Any, *, flush: bool = True, extra: dict | None = None,
                    _meta: dict | None = None) -> None:
        """添加消息到会话。

        内部字段统一放入 message 级别的 _meta 字段。
        flush 参数保留以兼容旧接口，但 SessionManager 不会自动保存，
        需要显式调用 save() 来持久化。

        Args:
            role: 消息角色
            content: 消息内容
            flush: 是否立即保存（保留兼容）
            extra: 额外字段（如旧的 _valid 等），会自动迁移到 _meta
            _meta: 直接指定 _meta 字段（新格式）
        """
        message = {"role": role, "content": content}

        # 处理 _meta 字段
        if _meta:
            message["_meta"] = _meta
        elif extra:
            # 向后兼容：将旧格式的 _valid, _compacted 等迁移到 _meta
            meta = {}
            if "_valid" in extra:
                meta["valid"] = extra.pop("_valid")
            if "_compacted" in extra:
                meta["compacted"] = extra.pop("_compacted")
            if "_output_path" in extra:
                meta["output_path"] = extra.pop("_output_path")
            if "_file_size" in extra:
                meta["file_size"] = extra.pop("_file_size")
            if "_truncated" in extra:
                meta["truncated"] = extra.pop("_truncated")
            if meta:
                message["_meta"] = meta
            # 其他 extra 字段直接合并
            message.update(extra)

        self.messages.append(message)
        self._messages_dirty = True
        self.metadata["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def get_messages(self) -> list[dict]:
        """获取所有消息。"""
        return self.messages

    @staticmethod
    def _is_real_user_message(msg: dict) -> bool:
        """Check if a message is a real user/assistant message (not SubAgent metadata)."""
        if msg.get("role") == "_subagent_ref":
            return False
        return True

    def get_valid_messages(self) -> list[dict]:
        """获取有效的消息（递归过滤 _meta.valid=False 的消息和 blocks）。

        用于 API 请求前过滤。支持嵌套过滤：
        - 顶层 _meta.valid=False 的消息会被移除
        - 跳过 _subagent_ref 角色（SubAgent 引用，仅用于 UI 显示）
        - 剥离 _meta 中的内部字段（不发送给 API）
        - 向后兼容：检测旧格式的 _valid 字段并自动处理

        使用脏标记缓存：仅在消息变更时重建。
        """
        if not self._messages_dirty and self._valid_messages_cache is not None:
            return self._valid_messages_cache

        INTERNAL_META = self._INTERNAL_META_FIELDS
        result = []
        for msg in self.messages:
            # 检查消息级别的 validity（支持新格式 _meta.valid 和旧格式 _valid）
            meta = msg.get("_meta", {})
            msg_valid = meta.get("valid", msg.get("_valid", True))
            if msg_valid is False:
                continue
            if not self._is_real_user_message(msg):
                continue

            role = msg.get("role")
            content = msg.get("content", "")

            # 字符串内容直接保留
            if not isinstance(content, list):
                clean_msg = {"role": role, "content": content}
                # 剥离 _meta 中的内部字段，保留其他 _meta（如果存在）
                if meta:
                    stripped_meta = {k: v for k, v in meta.items() if k not in INTERNAL_META}
                    if stripped_meta:
                        clean_msg["_meta"] = stripped_meta
                result.append(clean_msg)
                continue

            # 列表内容：直接保留所有 blocks（不再递归过滤 block 级别的 _valid）
            # 注意：新格式中 _valid 在 message 级别，不在 block 级别
            clean_blocks = list(content)

            if clean_blocks:
                clean_msg = {"role": role, "content": clean_blocks}
                # 剥离 _meta 中的内部字段，保留其他 _meta（如果存在）
                if meta:
                    stripped_meta = {k: v for k, v in meta.items() if k not in INTERNAL_META}
                    if stripped_meta:
                        clean_msg["_meta"] = stripped_meta
                result.append(clean_msg)

        self._valid_messages_cache = result
        self._messages_dirty = False
        return result

    def clear(self) -> None:
        """清空所有消息。"""
        self.messages.clear()
        self._messages_dirty = True
        self.metadata["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def get_last_n_messages(self, n: int) -> list[dict]:
        """获取最后 N 条消息。"""
        return self.messages[-n:] if n > 0 else []

    def get_message_count(self) -> int:
        """获取消息数量。"""
        return len(self.messages)

    def update_tool_result(self, tool_use_id: str, updates: dict) -> None:
        """更新指定 tool_use_id 的 tool_result。

        支持新格式 (tool_use_id) 和旧格式 (tool_call_id)。

        Args:
            tool_use_id: 工具调用 ID
            updates: 要更新的字段
        """
        for msg in self.messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_result":
                    # 支持新格式 (tool_use_id) 和旧格式 (tool_call_id)
                    block_id = block.get("tool_use_id") or block.get("tool_call_id", "")
                    if block_id == tool_use_id:
                        block.update(updates)
                        self._messages_dirty = True
                        self.metadata["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        return

    # ========== SubAgent 执行日志 ==========

    def _generate_exec_id(self) -> str:
        """生成执行日志 ID：exec_{8位hex}。"""
        return f"exec_{secrets.token_hex(4)}"

    def add_subagent_ref(self, exec_id: str, task_summary: str, status: str = "running",
                          iterations: int = 0, message_count: int = 0) -> None:
        """添加SubAgent 引用到主消息列表。

        Args:
            exec_id: 执行日志 ID
            task_summary: 任务摘要
            status: 状态（running/completed/error/timeout）
            iterations: 迭代次数
            message_count: 消息数量
        """
        ref = {
            "role": "_subagent_ref",
            "exec_id": exec_id,
            "task_summary": task_summary,
            "status": status,
            "iterations": iterations,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "message_count": message_count,
        }
        self.messages.append(ref)
        self._messages_dirty = True
        self.metadata["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.metadata["subagent_count"] = self.metadata.get("subagent_count", 0) + 1

    def update_subagent_ref(self, exec_id: str, status: str, iterations: int,
                             message_count: int, summary: str = "",
                             current_tool: str = "") -> None:
        """更新SubAgent 引用的状态。

        Args:
            current_tool: 当前正在执行的工具名（空字符串=无/已结束）。
                          用于前端实时展示 subagent 进度。
        """
        for msg in self.messages:
            if msg.get("role") == "_subagent_ref" and msg.get("exec_id") == exec_id:
                msg["status"] = status
                msg["iterations"] = iterations
                msg["message_count"] = message_count
                msg["ended_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if summary:
                    msg["summary"] = summary
                if current_tool is not None:
                    msg["current_tool"] = current_tool
                break

    def save_subagent_log(self, exec_id: str, task: str, messages: list[dict],
                           metadata: dict, summary: str = "") -> str:
        """保存 SubAgent 执行日志到 {session_dir}/exec_{id}/index.json。

        Args:
            exec_id: 执行日志 ID
            task: 任务描述
            messages: 完整消息列表
            metadata: 元数据（started_at, ended_at, duration_seconds, status, iterations, max_iterations）
            summary: 执行摘要

        Returns:
            exec_id
        """
        exec_dir = self.session_dir / exec_id
        exec_dir.mkdir(parents=True, exist_ok=True)
        log_file = exec_dir / "index.json"
        data = {
            "exec_id": exec_id,
            "session_id": self.session_id,
            "task": task,
            "metadata": metadata,
            "summary": summary,
            "messages": messages,
        }
        try:
            temp_file = log_file.with_suffix(".json.tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp_file.replace(log_file)
        except Exception as e:
            logger.error(f"Failed to save subagent log {exec_id}: {e}")
        return exec_id

    def load_subagent_log(self, exec_id: str) -> dict | None:
        """加载单个 SubAgent 执行日志。

        Returns:
            执行日志数据，None 表示不存在
        """
        log_file = self.session_dir / exec_id / "index.json"
        if not log_file.exists():
            return None
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load subagent log {exec_id}: {e}")
            return None

    def list_subagent_logs(self) -> list[dict]:
        """列出所有 SubAgent 执行日志（仅元数据，不含消息）。

        Returns:
            元数据列表：[{exec_id, task, status, iterations, ...}, ...]
        """
        logs = []
        for exec_dir in self.session_dir.glob("exec_*"):
            if not exec_dir.is_dir():
                continue
            log_file = exec_dir / "index.json"
            if not log_file.exists():
                continue
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 只返回元数据，不返回完整消息
                logs.append({
                    "exec_id": data.get("exec_id"),
                    "task": data.get("task", "")[:100],  # 截断长任务描述
                    "summary": data.get("summary", ""),
                    "metadata": data.get("metadata", {}),
                })
            except Exception as e:
                logger.warning(f"Failed to load subagent log {log_file}: {e}")
        # 按时间排序
        logs.sort(key=lambda x: x.get("metadata", {}).get("started_at", ""), reverse=False)
        return logs

    def delete_subagent_log(self, exec_id: str) -> bool:
        """删除单个 SubAgent 执行目录（含 index.json 和工具输出文件）。

        Returns:
            True 表示成功删除
        """
        import shutil
        exec_dir = self.session_dir / exec_id
        if exec_dir.exists() and exec_dir.is_dir():
            shutil.rmtree(exec_dir)
            return True
        return False

    # ========== 持久化 ==========

    def save(self) -> None:
        """持久化到 {session_dir}/index.json（原子写入）。"""
        session_file = self.session_dir / "index.json"
        data = {
            "session_id": self.session_id,
            "name": self.name,
            "messages": self.messages,
            "metadata": self.metadata,
        }

        try:
            # 原子写入：先写临时文件再重命名
            temp_file = session_file.with_suffix(".json.tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp_file.replace(session_file)
        except Exception as e:
            logger.error(f"Failed to save session {self.session_id}: {e}")
            # 清理临时文件
            try:
                temp_file = session_file.with_suffix(".json.tmp")
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass

    def load(self) -> bool:
        """从 {session_dir}/index.json 加载会话。

        返回 True 表示成功，False 表示文件不存在。
        """
        session_file = self.session_dir / "index.json"
        if not session_file.exists():
            return False

        try:
            with open(session_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.name = data.get("name", "New Session")
            self.messages = data.get("messages", [])
            self.metadata = data.get("metadata", self.metadata)
            self._messages_dirty = True
            return True
        except Exception as e:
            logger.error(f"Failed to load session {self.session_id}: {e}")
            return False

    def delete(self) -> None:
        """删除会话目录（包括 index.json、exec 子目录和工具输出文件）。"""
        import shutil
        if self.session_dir.exists():
            shutil.rmtree(self.session_dir)

    @staticmethod
    def list_sessions(sessions_dir: Path) -> list[dict]:
        """列出所有会话，按更新时间倒序。

        返回会话元数据列表：[{session_id, name, updated_at, ...}, ...]
        """
        sessions_dir = Path(sessions_dir)
        if not sessions_dir.exists():
            return []

        sessions = []
        # 遍历子目录（每个 session 是一个目录）
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            index_file = session_dir / "index.json"
            if not index_file.exists():
                continue
            try:
                with open(index_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                session_id = data.get("session_id", session_dir.name)
                name = data.get("name", "Unnamed")
                metadata = data.get("metadata", {})

                sessions.append({
                    "session_id": session_id,
                    "name": name,
                    **metadata,
                })
            except Exception as e:
                logger.warning(f"Failed to load session {session_dir}: {e}")
                continue

        # 按更新时间倒序
        sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return sessions

    def rename(self, new_name: str) -> None:
        """重命名会话。"""
        self.name = new_name
        self.metadata["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def set_hidden(self, hidden: bool) -> None:
        """设置会话隐藏状态。"""
        self.metadata["hidden"] = hidden
        self.metadata["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def is_hidden(self) -> bool:
        """获取会话隐藏状态。"""
        return self.metadata.get("hidden", False)

    # ========== 使用量追踪 ==========

    def update_usage(self, input_tokens: int = 0, output_tokens: int = 0,
                     api_calls: int = 0, cache_read_tokens: int = 0,
                     cache_creation_tokens: int = 0) -> None:
        """更新使用量统计。"""
        usage = self.metadata.get("usage", {})
        usage["input_tokens"] = usage.get("input_tokens", 0) + input_tokens
        usage["output_tokens"] = usage.get("output_tokens", 0) + output_tokens
        usage["api_calls"] = usage.get("api_calls", 0) + api_calls
        usage["cache_read_tokens"] = usage.get("cache_read_tokens", 0) + cache_read_tokens
        usage["cache_creation_tokens"] = usage.get("cache_creation_tokens", 0) + cache_creation_tokens
        self.metadata["usage"] = usage

    def get_usage(self) -> dict:
        """获取使用量统计。"""
        return self.metadata.get("usage", {
            "input_tokens": 0,
            "output_tokens": 0,
            "api_calls": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        })

    # ========== 压缩相关 ==========

    # Microcompact 占位符（替换旧工具结果正文）
    MICROCOMPACT_PLACEHOLDER = "[Old tool result content cleared]"

    def microcompact_tool_results(self, keep_recent: int = 6) -> int:
        """Microcompact：将旧的工具结果正文替换为占位符。

        轻量压缩，不调用 LLM，每轮模型调用前自动运行。
        保留最近 keep_recent 条工具结果消息（user 消息），更早的：
        - 设置 _meta.compacted = True
        - content 替换为占位符（发送给 API）
        - 原内容保留在外部文件（_meta.output_path）

        向后兼容：检测旧格式的 _compacted 字段并自动迁移。

        Returns:
            节省的字节数（估算）
        """
        PLACEHOLDER = self.MICROCOMPACT_PLACEHOLDER
        saved = 0
        cache_was_valid = not self._messages_dirty

        # 找到所有包含 tool_result 的 user 消息索引（跳过SubAgent 消息和引用）
        tool_result_msg_indices: list[int] = []
        for i, msg in enumerate(self.messages):
            if not self._is_real_user_message(msg):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            if any(b.get("type") == "tool_result" for b in content):
                tool_result_msg_indices.append(i)

        # 保留最近 keep_recent 条，压缩更早的
        if len(tool_result_msg_indices) <= keep_recent:
            return 0

        indices_to_compact = tool_result_msg_indices[:-keep_recent]

        for idx in indices_to_compact:
            msg = self.messages[idx]
            content = msg["content"]

            # 检查是否已经压缩过（支持新格式 _meta.compacted 和旧格式 block._compacted）
            meta = msg.get("_meta", {})
            if meta.get("compacted"):
                continue
            # 检查旧格式：任意 block 有 _compacted
            if any(b.get("_compacted") for b in content):
                # 迁移旧格式到新格式
                msg["_meta"] = {"compacted": True}
                continue

            for block in content:
                if block.get("type") != "tool_result":
                    continue

                # 跳过已压缩的（旧格式）
                if block.get("_compacted"):
                    continue

                original = block.get("content", "")

                if isinstance(original, str):
                    if original and original != PLACEHOLDER:
                        saved += len(original.encode('utf-8', errors='replace'))
                        block["content"] = PLACEHOLDER
                elif isinstance(original, list):
                    # 多模态内容（图片 + 文本）
                    has_content = any(
                        (sub.get("type") == "text" and sub.get("text", "") and sub["text"] != PLACEHOLDER)
                        for sub in original
                    )
                    if has_content:
                        for sub in original:
                            if sub.get("type") == "text":
                                text = sub.get("text", "")
                                if text and text != PLACEHOLDER:
                                    saved += len(text.encode('utf-8', errors='replace'))
                        block["content"] = [{"type": "text", "text": PLACEHOLDER}]

            # 设置消息级别的 _meta.compacted
            if "_meta" not in msg:
                msg["_meta"] = {}
            msg["_meta"]["compacted"] = True

        if saved > 0:
            self._messages_dirty = True
        return saved

    def mark_old_tool_calls_invalid(self, keep_recent_rounds: int = 5) -> int:
        """标记旧的工具调用为无效。

        保留最近 keep_recent_rounds 轮的工具调用，更早的标记 _meta.valid=False。
        轮次按 assistant 消息计数（每个 assistant 消息 = 1 轮）。
        返回节省的字节数。
        """
        saved = 0

        # 找出所有工具调用及其轮次
        tool_calls = []  # (msg_idx, block_idx, block, round_number)
        round_number = 0

        for msg_idx, msg in enumerate(self.messages):
            # 跳过SubAgent 消息和引用（不纳入轮次计数）
            if not self._is_real_user_message(msg):
                continue

            role = msg.get("role")
            content = msg.get("content", [])

            if not isinstance(content, list):
                continue

            # 轮次按 assistant 消息计数（每个 assistant = 1 轮）
            if role == "assistant":
                round_number += 1

            for block_idx, block in enumerate(content):
                if not isinstance(block, dict):
                    continue

                block_type = block.get("type")
                if block_type == "tool_use":
                    tool_calls.append({
                        "type": "use",
                        "msg_idx": msg_idx,
                        "block_idx": block_idx,
                        "block": block,
                        "round": round_number,
                    })
                elif block_type == "tool_result":
                    tool_calls.append({
                        "type": "result",
                        "msg_idx": msg_idx,
                        "block_idx": block_idx,
                        "block": block,
                        "round": round_number,
                    })

        if round_number <= keep_recent_rounds:
            return 0

        for call in tool_calls:
            if call["round"] <= round_number - keep_recent_rounds:
                msg_idx = call["msg_idx"]
                msg = self.messages[msg_idx]

                # 检查消息是否已经标记为无效
                meta = msg.get("_meta", {})
                if meta.get("valid") is False:
                    continue
                # 向后兼容：检查旧格式
                if msg.get("_valid") is False:
                    continue

                # 估算大小
                block = call["block"]
                content = block.get("input", {}) if call["type"] == "use" else block.get("content", "")
                size = len(str(content))

                # 标记消息级别的 _meta.valid = False
                if "_meta" not in msg:
                    msg["_meta"] = {}
                msg["_meta"]["valid"] = False
                saved += size

        if saved > 0:
            self._messages_dirty = True
        return saved

    def mark_old_images_invalid(self, keep_recent: int = 5) -> int:
        """标记旧的图片为无效。

        保留最近 keep_recent 条消息中的图片，更早的标记 _meta.valid=False。
        返回节省的字节数。
        """
        saved = 0

        # 找出所有图片消息（跳过SubAgent 消息和引用）
        image_messages = []  # (msg_idx, block_idx, sub_idx, size)
        for i, msg in enumerate(self.messages):
            if not self._is_real_user_message(msg):
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

        # 保留最近的消息
        if len(image_messages) <= keep_recent:
            return 0

        to_strip = image_messages[:-keep_recent]
        for msg_idx, block_idx, sub_idx, data_len in to_strip:
            msg = self.messages[msg_idx]

            # 检查消息是否已经标记为无效
            meta = msg.get("_meta", {})
            if meta.get("valid") is False:
                continue

            # 标记消息级别的 _meta.valid = False
            if "_meta" not in msg:
                msg["_meta"] = {}
            msg["_meta"]["valid"] = False
            saved += data_len

        if saved > 0:
            self._messages_dirty = True
        return saved

    # ========== 工具方法 ==========

    @staticmethod
    def create_new_session(sessions_dir: Path, name: str = "New Session") -> SessionManager:
        """创建新会话（使用短 ID）。"""
        session_id = generate_short_id()
        session = SessionManager(session_id, sessions_dir)
        session.name = name
        session.save()
        return session

    @staticmethod
    def load_session(session_id: str, sessions_dir: Path) -> SessionManager | None:
        """加载已存在的会话。

        返回 None 表示会话不存在。
        """
        session = SessionManager(session_id, sessions_dir)
        if session.load():
            return session
        return None

    def to_dict(self) -> dict:
        """转换为字典格式（用于 API 返回）。"""
        return {
            "session_id": self.session_id,
            "name": self.name,
            "messages": self.messages,
            "metadata": self.metadata,
        }
