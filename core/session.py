"""Session management - independent of LLM client.

Directory structure:
  data/workspace/{uuid}/sessions/
    {short_id}/               # 每个 session 一个目录（8 位十六进制）
      messages.jsonl          # 完整消息历史（追加式，UI 直接读取，保留压缩前会话）
      index.json              # 模型提交视图：{schema_version, next_seq, commits[]}
      meta.json               # 会话属性（session_id/name/metadata）
      {tool_use_id}.txt       # Master Agent 工具输出（实时流式写入，供前端轮询）
      exec_{id}/              # 每个 Worker/Lite 子代理一个子目录
        index.json            # Worker/Lite 执行日志
        {tool_use_id}.txt     # Worker/Lite 工具输出

双读路径：
- UI 读 messages.jsonl 完整历史（append 序 = 会话序），从 index.json commits 合并 _meta
- 模型读 index.json commits 视图（seq 引用 jsonl 内容 / summary 内嵌摘要 + 全部 _meta）

压缩/无效化只改 commits（视图），jsonl 永不重写；revert/clear 是唯一显式截断 jsonl 的例外。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import secrets
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.fs_utils import atomic_write_json, atomic_write_text, load_json_or_backup

# exec_id 白名单：只允许字母、数字、下划线、短横线，防止路径穿越
_EXEC_ID_RE = re.compile(r'^[a-zA-Z0-9_-]+$')

logger = logging.getLogger(__name__)

# 新式会话文件布局
MESSAGES_FILE = "messages.jsonl"
INDEX_FILE = "index.json"   # 现在是模型提交视图（commits），不再存消息正文
META_FILE = "meta.json"
SCHEMA_VERSION = 2

# 每个 session 目录一把写锁：串行化 jsonl 追加与 index.json/meta.json 原子重写
_SESSION_LOCKS: dict[Path, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def _get_session_lock(session_dir: Path) -> threading.Lock:
    """获取（或创建）session 目录的写锁。"""
    session_dir = Path(session_dir)
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_dir)
        if lock is None:
            lock = _SESSION_LOCKS.setdefault(session_dir, threading.Lock())
        return lock


def _drop_session_lock(session_dir: Path) -> None:
    """删除 session 时释放写锁，防止泄漏。"""
    session_dir = Path(session_dir)
    with _SESSION_LOCKS_GUARD:
        _SESSION_LOCKS.pop(session_dir, None)


# ========== jsonl 读写 ==========

def read_jsonl(path: Path | str) -> list[dict]:
    """读取 jsonl，逐行解析；遇到损坏行（断尾/崩溃残留）丢弃该行及之后内容。"""
    path = Path(path)
    if not path.exists():
        return []
    lines: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    break
    except Exception as e:
        logger.warning(f"[session] 读取 jsonl 失败 {path}: {e}")
    return lines


def append_jsonl(path: Path | str, lines: list[dict]) -> None:
    """追加 jsonl 行（newline="" 保持 LF，fsync 后返回）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def write_jsonl(path: Path | str, lines: list[dict]) -> None:
    """整体重写 jsonl（原子；仅 revert/clear 等显式销毁操作使用）。"""
    atomic_write_text(
        path,
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
    )


# ========== 消息 <-> 行 / 提交 序列化 ==========

def _strip_block_meta(content: Any) -> Any:
    """深拷贝 content 并移除各 block 的 _meta（jsonl 只存内容历史）。"""
    if not isinstance(content, list):
        return content
    out = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
            continue
        b = dict(block)
        b.pop("_meta", None)
        out.append(b)
    return out


def _extract_block_meta(content: Any) -> dict:
    """从 content blocks 提取 block 级 _meta，键为 block id（tool_use_id/id）。"""
    blocks: dict = {}
    if not isinstance(content, list):
        return blocks
    for block in content:
        if not isinstance(block, dict):
            continue
        bm = block.get("_meta")
        if not bm:
            continue
        key = block.get("tool_use_id") or block.get("tool_call_id") or block.get("id")
        if key:
            blocks[key] = dict(bm)
    return blocks


def _merge_block_meta(content: Any, blocks: dict | None) -> None:
    """把 commits 的 block 级 _meta 合并回消息的 blocks（原地）。"""
    if not isinstance(content, list) or not blocks:
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        key = block.get("tool_use_id") or block.get("tool_call_id") or block.get("id")
        if key and key in blocks:
            block["_meta"] = dict(blocks[key])


def _jsonl_line(msg: dict, seq: int) -> dict:
    """把内存消息序列化为 jsonl 行（含 id，剥离 _meta）。"""
    meta = msg.get("_meta") or {}
    return {
        "seq": seq,
        "id": meta.get("id", ""),
        "role": msg.get("role"),
        "content": _strip_block_meta(msg.get("content")),
    }


def _message_from_line(line: dict, msg_meta: dict | None, seq: int) -> dict:
    """从 jsonl 行 + 消息级 _meta 重建内存消息。"""
    meta = dict(msg_meta or {})
    meta["id"] = line.get("id") or meta.get("id") or generate_short_id()
    meta["seq"] = seq
    return {"role": line.get("role"), "content": line.get("content"), "_meta": meta}


def _commit_for_message(msg: dict, seq: int) -> dict:
    """普通消息 → 提交记录（内容读 jsonl，_meta 内嵌）。"""
    meta = msg.get("_meta") or {}
    return {
        "seq": seq,
        "msg_meta": {k: v for k, v in meta.items() if k not in ("id", "seq")},
        "blocks": _extract_block_meta(msg.get("content")),
    }


def _summary_commit(msg: dict) -> dict:
    """压缩摘要消息 → 内嵌 summary 提交记录（不进 jsonl）。"""
    meta = msg.get("_meta") or {}
    return {
        "summary": msg.get("content"),
        "role": msg.get("role"),
        "msg_meta": {k: v for k, v in meta.items() if k != "id"},
        "blocks": {},
    }


def _message_from_summary_commit(commit: dict) -> dict:
    """摘要提交记录 → 内存消息。"""
    meta = dict(commit.get("msg_meta") or {})
    meta["id"] = meta.get("id") or generate_short_id()
    return {
        "role": commit.get("role", "user"),
        "content": commit.get("summary", ""),
        "_meta": meta,
    }


def _apply_model_content_rules(msg: dict) -> None:
    """模型视图重建：压缩/已回答的 tool_result 清空内联内容，交给
    _resolve_tool_results 从外部文件读取（否则 jsonl 原始内容会跳过文件读取）。"""
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if block.get("type") != "tool_result":
            continue
        bm = block.get("_meta") or {}
        # compacted 无论有无 output_path 都清空内容（内联压缩结果无外置文件，
        # 原文在 jsonl，模型视图交给 _resolve_tool_results 注入占位符）；
        # completed（已回答的 ask_user）需 output_path 才读答案文件
        if bm.get("compacted") or (bm.get("output_path") and bm.get("completed")):
            block["content"] = None


# ========== 会话文件读取入口（web_api 复用） ==========

def read_view(session_dir: Path | str) -> dict:
    """读取 index.json 提交视图。"""
    return load_json_or_backup(Path(session_dir) / INDEX_FILE, {}) or {}


def read_meta(session_dir: Path | str) -> dict:
    """读取 meta.json 会话属性。"""
    return load_json_or_backup(Path(session_dir) / META_FILE, {}) or {}


def build_model_messages(session_dir: Path | str) -> list[dict]:
    """按 index.json 的 commits 重建模型视图消息（含 _meta、应用内容规则）。"""
    view = read_view(session_dir)
    by_seq = {line.get("seq"): line for line in read_jsonl(Path(session_dir) / MESSAGES_FILE)}
    messages = []
    for commit in view.get("commits", []):
        if "summary" in commit:
            messages.append(_message_from_summary_commit(commit))
            continue
        line = by_seq.get(commit.get("seq"))
        if line is None:
            continue  # 防御：jsonl 缺失/损坏的行跳过
        msg = _message_from_line(line, commit.get("msg_meta"), commit.get("seq"))
        _merge_block_meta(msg.get("content"), commit.get("blocks"))
        _apply_model_content_rules(msg)
        messages.append(msg)
    return messages


def ensure_new_format(session_dir: Path | str) -> bool:
    """若 session 目录仍是旧版单文件格式，迁移为新 3 文件布局。

    返回是否执行了迁移。旧版 index.json 改名 index.json.legacy 保留。
    """
    from core.migration import migrate_session_to_new_layout
    index_file = Path(session_dir) / INDEX_FILE
    if not index_file.exists():
        return False
    if "commits" in (load_json_or_backup(index_file, {}) or {}):
        return False
    return migrate_session_to_new_layout(Path(session_dir))


def load_history_messages(session_dir: Path | str) -> list[dict]:
    """完整历史（UI 数据源）：所有 jsonl 行 + 从 commits 合并 _meta。

    不解析外部工具输出文件（由 web_api._resolve_tool_results_for_session 完成）。
    """
    ensure_new_format(session_dir)
    session_dir = Path(session_dir)
    view = read_view(session_dir)
    by_seq = {c.get("seq"): c for c in view.get("commits", []) if "seq" in c}
    messages = []
    for line in read_jsonl(session_dir / MESSAGES_FILE):
        seq = line.get("seq")
        commit = by_seq.get(seq)
        msg = _message_from_line(line, commit.get("msg_meta") if commit else None, seq)
        if commit:
            _merge_block_meta(msg.get("content"), commit.get("blocks"))
        messages.append(msg)
    return messages


def load_history_meta(session_dir: Path | str) -> dict:
    """会话属性（UI/列表数据源），含迁移保障。"""
    ensure_new_format(session_dir)
    meta = read_meta(session_dir)
    return {
        "session_id": meta.get("session_id") or Path(session_dir).name,
        "name": meta.get("name", "New Session"),
        "metadata": meta.get("metadata", {}),
    }


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
    - Agent 执行日志管理

    Session 格式使用 Anthropic 格式，内部字段统一放入 _meta: {}。
    """

    # _meta 中的内部字段（发送到 API 前剥离）
    # 包括消息级别和 block 级别的所有内部字段
    _INTERNAL_META_FIELDS = frozenset({"valid", "compacted", "output_path", "file_size", "truncated", "tool_name", "multimodal", "completed", "answered", "exec_id", "id", "seq", "summary"})

    def __init__(self, session_id: str, sessions_dir: Path):
        import re as _re
        # Allow empty string (means "not yet assigned") but validate non-empty IDs
        if session_id and not _re.match(r'^[a-zA-Z0-9_-]+$', session_id):
            raise ValueError(f"Invalid session_id format: {session_id!r}")
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
            "agent_count": 0,
        }
        self.name: str = "New Session"

        # 确保目录存在
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)

    # ========== 消息管理 ==========

    def add_message(self, role: str, content: Any, *, flush: bool = True, extra: dict | None = None,
                    _meta: dict | None = None) -> None:
        """添加消息到会话。

        Args:
            role: 消息角色
            content: 消息内容
            flush: 是否立即保存（保留兼容）
            extra: 额外字段，会合并到消息中
            _meta: 消息级别的 _meta 字段
        """
        message = {"role": role, "content": content}

        if _meta:
            message["_meta"] = _meta
        if extra:
            message.update(extra)

        self.messages.append(message)
        self._messages_dirty = True
        self.metadata["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def get_messages(self) -> list[dict]:
        """获取所有消息。"""
        return self.messages

    def get_valid_messages(self) -> list[dict]:
        """获取有效的消息（递归过滤 _meta.valid=False 的消息和 blocks）。

        用于 API 请求前过滤。支持嵌套过滤：
        - 顶层 _meta.valid=False 的消息会被移除
        - 剥离 _meta 中的内部字段（不发送给 API）

        使用脏标记缓存：仅在消息变更时重建。
        """
        if not self._messages_dirty and self._valid_messages_cache is not None:
            return list(self._valid_messages_cache)  # Return shallow copy to prevent cache mutation

        INTERNAL_META = self._INTERNAL_META_FIELDS
        result = []
        for msg in self.messages:
            # 检查消息级别的 validity
            meta = msg.get("_meta", {})
            if meta.get("valid") is False:
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

            # 列表内容：保留所有 blocks，剥离 block 级别 _meta 内部字段
            # 注意：新格式中 _valid 在 message 级别，不在 block 级别
            clean_blocks = []
            for block in content:
                clean_block = dict(block)
                # 剥离 block 级别的 _meta 内部字段
                if "_meta" in clean_block:
                    stripped_block_meta = {k: v for k, v in clean_block["_meta"].items() if k not in INTERNAL_META}
                    if stripped_block_meta:
                        clean_block["_meta"] = stripped_block_meta
                    else:
                        del clean_block["_meta"]
                clean_blocks.append(clean_block)

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
        """清空所有消息并落盘（显式销毁，唯一重写 jsonl 的例外之一）。

        agent.reset() 调用后不另存，故必须直接写盘。
        """
        lock = _get_session_lock(self.session_dir)
        with lock:
            self.messages.clear()
            self._messages_dirty = True
            self.metadata["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            jsonl_path = self.session_dir / MESSAGES_FILE
            if jsonl_path.exists():
                write_jsonl(jsonl_path, [])
            atomic_write_json(self.session_dir / INDEX_FILE, {
                "schema_version": SCHEMA_VERSION,
                "next_seq": 0,
                "commits": [],
            })
            atomic_write_json(self.session_dir / META_FILE, self._meta_payload())

    def get_last_n_messages(self, n: int) -> list[dict]:
        """获取最后 N 条消息。"""
        return self.messages[-n:] if n > 0 else []

    def get_message_count(self) -> int:
        """获取消息数量。"""
        return len(self.messages)

    # ========== Agent 执行日志 ==========

    def _generate_exec_id(self) -> str:
        """生成执行日志 ID：exec_{8位hex}。"""
        return f"exec_{secrets.token_hex(4)}"

    def save_agent_log(self, exec_id: str, task: str, messages: list[dict],
                           metadata: dict, summary: str = "") -> str:
        """保存 Agent 执行日志到 {session_dir}/exec_{id}/index.json。

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
            from core.fs_utils import atomic_write_json
            atomic_write_json(log_file, data)
        except Exception as e:
            logger.error(f"Failed to save agent log {exec_id}: {e}")
        return exec_id

    def load_agent_log(self, exec_id: str) -> dict | None:
        """加载单个 Agent 执行日志。

        Args:
            exec_id: 执行 ID（必须是白名单格式，否则拒绝，防路径穿越）

        Returns:
            执行日志数据，None 表示不存在
        """
        if not _EXEC_ID_RE.match(exec_id):
            raise ValueError(f"Invalid exec_id: {exec_id!r}")
        log_file = self.session_dir / exec_id / "index.json"
        if not log_file.exists():
            return None
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load agent log {exec_id}: {e}")
            return None

    def list_agent_logs(self) -> list[dict]:
        """列出所有 Agent 执行日志（仅元数据，不含消息）。

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
                logger.warning(f"Failed to load agent log {log_file}: {e}")
        # 按时间排序
        logs.sort(key=lambda x: x.get("metadata", {}).get("started_at", ""), reverse=False)
        return logs

    def delete_agent_log(self, exec_id: str) -> bool:
        """删除单个 Agent 执行目录（含 index.json 和工具输出文件）。

        Args:
            exec_id: 执行 ID（必须是白名单格式，否则拒绝，防路径穿越）

        Returns:
            True 表示成功删除
        """
        if not _EXEC_ID_RE.match(exec_id):
            raise ValueError(f"Invalid exec_id: {exec_id!r}")
        import shutil
        exec_dir = self.session_dir / exec_id
        if exec_dir.exists() and exec_dir.is_dir():
            shutil.rmtree(exec_dir)
            return True
        return False

    # ========== 持久化 ==========

    def save(self) -> None:
        """持久化：追加新消息到 messages.jsonl + 原子重写 index.json/meta.json。"""
        lock = _get_session_lock(self.session_dir)
        with lock:
            self._persist()

    def _persist(self) -> None:
        """对账 self.messages → 三件套文件（调用方必须持有 _get_session_lock）。

        派生 commits：invalid 消息跳过（jsonl 保留）、summary 消息内嵌摘要、
        普通消息按 seq 引用 jsonl；仅追加无 seq 的新消息到 jsonl。
        next_seq = max(磁盘, 内存最大 seq+1) 崩溃自愈。
        """
        index_file = self.session_dir / INDEX_FILE
        disk_next_seq = (load_json_or_backup(index_file, {}) or {}).get("next_seq", 0) or 0
        next_seq = disk_next_seq
        max_seq = -1
        append_lines: list[dict] = []
        commits: list[dict] = []

        for msg in self.messages:
            if not isinstance(msg, dict):
                continue
            meta = msg.get("_meta") or {}
            if meta.get("summary"):
                commits.append(_summary_commit(msg))
                continue  # 摘要只进提交视图，不进 jsonl（UI 不显示摘要）
            seq = meta.get("seq")
            if seq is None:
                seq = next_seq
                next_seq += 1
                if "_meta" not in msg:
                    msg["_meta"] = {}
                msg["_meta"]["seq"] = seq
                if "id" not in msg["_meta"]:
                    msg["_meta"]["id"] = generate_short_id()
                append_lines.append(_jsonl_line(msg, seq))
            max_seq = max(max_seq, seq)
            if meta.get("valid") is False:
                continue  # 无效消息仍写 jsonl（UI 历史保留），但不进提交视图
            commits.append(_commit_for_message(msg, seq))

        next_seq = max(next_seq, max_seq + 1)

        if append_lines:
            append_jsonl(self.session_dir / MESSAGES_FILE, append_lines)
        atomic_write_json(index_file, {
            "schema_version": SCHEMA_VERSION,
            "next_seq": next_seq,
            "commits": commits,
        })
        atomic_write_json(self.session_dir / META_FILE, self._meta_payload())

    def _meta_payload(self) -> dict:
        """meta.json 内容。"""
        return {
            "session_id": self.session_id or self.session_dir.name,
            "name": self.name,
            "metadata": self.metadata,
        }

    def load(self) -> bool:
        """从 session 目录加载会话（新 3 文件布局；旧格式自动迁移）。

        返回 True 表示成功，False 表示文件不存在。
        """
        session_dir = self.session_dir
        if not (session_dir / INDEX_FILE).exists():
            return False

        try:
            ensure_new_format(session_dir)
        except Exception as e:
            logger.error(f"Failed to migrate session {self.session_id}: {e}")
            return False

        if not (session_dir / INDEX_FILE).exists():
            return False

        self.messages = build_model_messages(session_dir)
        meta = read_meta(session_dir)
        self.name = meta.get("name", "New Session")
        self.metadata = meta.get("metadata", self.metadata)
        self._messages_dirty = True
        return True

    def delete(self) -> None:
        """删除会话目录（包括 index.json、exec 子目录和工具输出文件）。"""
        import shutil
        if self.session_dir.exists():
            shutil.rmtree(self.session_dir)
        _drop_session_lock(self.session_dir)

    def revert_to_message(self, msg_id: str) -> int:
        """撤销到指定消息：删除该消息及之后所有消息（截断 jsonl + commits）。

        Args:
            msg_id: 目标消息 _meta.id

        Returns:
            删除的消息数

        Raises:
            ValueError: 未找到指定消息
        """
        lock = _get_session_lock(self.session_dir)
        with lock:
            target_idx = None
            for i, msg in enumerate(self.messages):
                if msg.get("_meta", {}).get("id") == msg_id:
                    target_idx = i
                    break
            if target_idx is None:
                raise ValueError("未找到指定的消息")
            deleted_count = len(self.messages) - target_idx

            target_seq = self.messages[target_idx].get("_meta", {}).get("seq")
            if target_seq is None:
                # 摘要消息无 seq（jsonl 中不存在），仅从内存消息截断
                del self.messages[target_idx:]
                self._persist()
                return deleted_count

            # 目标消息本身也删除（与旧 API 一致：撤销到该消息之前），
            # jsonl/commits/内存三者同步截断，避免幽灵消息
            self._truncate_jsonl_to(target_seq)
            del self.messages[target_idx:]
            # 预置 next_seq，让 _persist 复用被截断的序号
            atomic_write_json(self.session_dir / INDEX_FILE, {
                "schema_version": SCHEMA_VERSION,
                "next_seq": target_seq,
                "commits": [],
            })
            self._persist()
            return deleted_count

    def _truncate_jsonl_to(self, min_seq: int) -> None:
        """物理截断 messages.jsonl，只保留 seq < min_seq 的行（显式销毁）。"""
        jsonl_path = self.session_dir / MESSAGES_FILE
        if not jsonl_path.exists():
            return
        keep = [line for line in read_jsonl(jsonl_path) if line.get("seq", -1) < min_seq]
        write_jsonl(jsonl_path, keep)

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
            index_file = session_dir / INDEX_FILE
            if not index_file.exists():
                continue
            try:
                ensure_new_format(session_dir)
                meta = load_history_meta(session_dir)
                metadata = meta.get("metadata", {})

                sessions.append({
                    "session_id": meta.get("session_id", session_dir.name),
                    "name": meta.get("name", "Unnamed"),
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
        """获取使用量统计（返回副本，防止调用方原地修改不触发保存）。"""
        return copy.deepcopy(self.metadata.get("usage", {
            "input_tokens": 0,
            "output_tokens": 0,
            "api_calls": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }))

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
        """转换为字典格式（用于 API 返回，返回深拷贝防止调用方污染内部状态）。"""
        return {
            "session_id": self.session_id,
            "name": self.name,
            "messages": copy.deepcopy(self.messages),
            "metadata": copy.deepcopy(self.metadata),
        }
