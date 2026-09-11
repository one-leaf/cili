"""Migration utilities for session format upgrade.

Migrates old session format (block-level _valid, _compacted, etc.)
to new format: message-level _meta.valid + block-level _meta storage fields.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def migrate_session_file(session_file: Path) -> bool:
    """Migrate a single session file from old format to new format.

    Returns True if migration was performed, False otherwise.
    """
    try:
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load session file {session_file}: {e}")
        return False

    modified = False

    # Migrate messages
    messages = data.get("messages", [])
    for msg in messages:
        if migrate_message(msg):
            modified = True

    # Migrate todos from metadata to independent file
    metadata = data.get("metadata", {})
    if "todos" in metadata:
        session_id = data.get("session_id") or session_file.parent.name
        if migrate_todos_from_metadata(metadata, session_id):
            modified = True

    # Save if modified
    if modified:
        try:
            from core.fs_utils import atomic_write_json
            atomic_write_json(session_file, data)
            logger.info(f"Migrated session: {session_file}")
        except Exception as e:
            logger.error(f"Failed to save migrated session {session_file}: {e}")
            return False

    return modified


def migrate_message(msg: dict) -> bool:
    """Migrate a single message from old format to new format.

    Old format: block-level _valid, _compacted, _output_path, etc.
    New format:
    - 消息级 _meta.valid：整条消息有效性（session.get_valid_messages 只读消息级）
    - block 级 _meta.{compacted, output_path, file_size, truncated, tool_name,
      completed}：工具结果存储字段（_resolve_tool_results 只读 block 级）

    Returns True if migration was performed.
    """
    content = msg.get("content", "")
    if not isinstance(content, list):
        # String content: just check for message-level _valid
        if "_valid" in msg:
            if "_meta" not in msg:
                msg["_meta"] = {}
            msg["_meta"]["valid"] = msg.pop("_valid")
            return True
        return False

    # Check if any block has old-format fields
    needs_migration = False
    message_valid = None  # 任一 block 无效则整条消息无效

    for block in content:
        if not isinstance(block, dict):
            continue
        block_meta = block.get("_meta")

        def _ensure_block_meta() -> dict:
            nonlocal block_meta
            if block_meta is None:
                block_meta = {}
                block["_meta"] = block_meta
            return block_meta

        # Block-level _valid → message-level _meta.valid
        # If any block is invalid, the whole message is invalid
        if "_valid" in block:
            needs_migration = True
            if block.pop("_valid") is False:
                message_valid = False

        # 存储类字段（compacted/output_path/file_size/truncated/tool_name）是
        # block 级 _meta（运行时 _resolve_tool_results 只读 block 级），
        # 必须写回 block["_meta"] 而非消息级，否则外部存储工具结果丢失
        if "_compacted" in block:
            needs_migration = True
            if block.pop("_compacted"):
                _ensure_block_meta()["compacted"] = True

        if "_output_path" in block:
            needs_migration = True
            _ensure_block_meta()["output_path"] = block.pop("_output_path")

        if "_file_size" in block:
            needs_migration = True
            _ensure_block_meta()["file_size"] = block.pop("_file_size")

        if "_truncated" in block:
            needs_migration = True
            _ensure_block_meta()["truncated"] = block.pop("_truncated")

        # tool_name → block 级 _meta.tool_name
        if "tool_name" in block:
            needs_migration = True
            _ensure_block_meta()["tool_name"] = block.pop("tool_name")

        # _content (old microcompact storage) → remove (content already in external file)
        if "_content" in block:
            needs_migration = True
            block.pop("_content")

        # Convert old field names to Anthropic format
        # tool_call_id → tool_use_id
        if block.get("type") == "tool_result":
            if "tool_call_id" in block and "tool_use_id" not in block:
                block["tool_use_id"] = block.pop("tool_call_id")

        # type: reasoning → thinking
        if block.get("type") == "reasoning":
            block["type"] = "thinking"
            if "text" in block:
                block["thinking"] = block.pop("text")

        # type: tool_call → tool_use
        if block.get("type") == "tool_call":
            block["type"] = "tool_use"
            # arguments (string) → input (dict)
            if "arguments" in block:
                args = block.pop("arguments")
                try:
                    if isinstance(args, str):
                        block["input"] = json.loads(args) if args else {}
                    else:
                        block["input"] = args
                except json.JSONDecodeError:
                    block["input"] = {"_raw": args}

        # _meta.wait_for_user → block 级 _meta.completed (inverted semantics)
        if block_meta is not None and "wait_for_user" in block_meta:
            needs_migration = True
            wfu = block_meta.pop("wait_for_user")
            # wait_for_user=True → completed=False (waiting)
            # wait_for_user=False → completed=True (answered)
            block_meta["completed"] = not wfu

        # Recursively migrate tool_result sub-blocks
        if block.get("type") == "tool_result":
            rc = block.get("content", "")
            if isinstance(rc, list):
                for sub in rc:
                    if isinstance(sub, dict):
                        if sub.get("type") == "reasoning":
                            sub["type"] = "thinking"
                            if "text" in sub:
                                sub["thinking"] = sub.pop("text")
                        # 旧格式子块 _valid=False 表示加载失败（如坏图片），
                        # 置消息级 valid=False 而非无条件丢弃，避免"复活"失败内容
                        if sub.pop("_valid", None) is False:
                            message_valid = False
                        sub.pop("_compacted", None)

    # Apply migrated message-level meta
    if needs_migration or message_valid is not None:
        if message_valid is False:
            if "_meta" not in msg:
                msg["_meta"] = {}
            msg["_meta"]["valid"] = False
        return True

    return False


def migrate_todos_from_metadata(metadata: dict, session_id: str) -> bool:
    """Migrate todos from session metadata to independent file.

    Returns True if migration was performed.
    """
    todos = metadata.get("todos", [])
    if not todos:
        return False

    try:
        from core.tools.todo import write_todos
        write_todos(session_id, todos)
        del metadata["todos"]
        logger.info(f"Migrated {len(todos)} todos from session {session_id} to independent file")
        return True
    except Exception as e:
        logger.warning(f"Failed to migrate todos for session {session_id}: {e}")
        return False


def migrate_session_to_new_layout(session_dir: Path) -> bool:
    """Migrate a session dir from old single-file format to the 3-file JSONL layout.

    Old: {session_dir}/index.json 含完整 "messages"。
    New: messages.jsonl（完整历史，UI 数据源）+ index.json（commits 模型视图）
         + meta.json（会话属性）。

    先跑 migrate_session_file（block 级 → 消息级 _meta），再拆 3 文件；
    旧 index.json 备份为 index.json.legacy（copy 而非 rename，保证任意一步
    崩溃后会话仍可读）。已是新布局（含 commits）则直接返回 False。

    Returns:
        True 表示执行了迁移。
    """
    import shutil

    from core.session import (
        MESSAGES_FILE,
        META_FILE,
        SCHEMA_VERSION,
        _commit_for_message,
        _jsonl_line,
        _summary_commit,
        generate_short_id,
        write_jsonl,
    )
    from core.fs_utils import atomic_write_json

    session_dir = Path(session_dir)
    index_file = session_dir / "index.json"
    if not index_file.exists():
        return False

    # 先做 block 级 → 消息级 _meta 迁移，保证拆文件时字段已就位（幂等）
    migrate_session_file(index_file)

    try:
        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load session file {index_file}: {e}")
        return False

    if "commits" in data:
        return False  # 已是新布局

    messages = data.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    # 备份旧文件（copy 不 rename：rename 后若中途崩溃会短暂丢失 index.json）
    legacy_file = index_file.with_suffix(".json.legacy")
    if not legacy_file.exists():
        try:
            shutil.copy2(index_file, legacy_file)
        except Exception as e:
            logger.warning(f"Failed to backup legacy file {index_file}: {e}")

    next_seq = 0
    jsonl_lines: list[dict] = []
    commits: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if "_meta" not in msg:
            msg["_meta"] = {}
        meta = msg["_meta"]
        if "id" not in meta:
            meta["id"] = generate_short_id()
        seq = next_seq
        next_seq += 1
        meta["seq"] = seq
        jsonl_lines.append(_jsonl_line(msg, seq))
        # 无效消息从提交视图删除，但 jsonl 完整历史保留（UI 仍可见）
        if meta.get("valid") is False:
            continue
        if meta.get("summary"):
            commits.append(_summary_commit(msg))
        else:
            commits.append(_commit_for_message(msg, seq))

    write_jsonl(session_dir / MESSAGES_FILE, jsonl_lines)
    atomic_write_json(index_file, {
        "schema_version": SCHEMA_VERSION,
        "next_seq": next_seq,
        "commits": commits,
    })
    atomic_write_json(session_dir / META_FILE, {
        "session_id": data.get("session_id") or session_dir.name,
        "name": data.get("name", "New Session"),
        "metadata": data.get("metadata", {}),
    })

    logger.info(f"Migrated session to new layout: {session_dir}")
    return True


def migrate_all_sessions(agents_dir: Path) -> int:
    """Migrate all session files in the agents directory.

    Returns the number of sessions migrated.
    """
    migrated = 0

    # Find all workspace directories
    if not agents_dir.exists():
        return 0

    for workspace_dir in agents_dir.iterdir():
        if not workspace_dir.is_dir():
            continue

        sessions_dir = workspace_dir / "sessions"
        if not sessions_dir.exists():
            continue

        # Find all session directories
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue

            session_file = session_dir / "index.json"
            if not session_file.exists():
                continue

            try:
                if migrate_session_file(session_file):
                    migrated += 1
                if migrate_session_to_new_layout(session_dir):
                    migrated += 1
            except Exception as e:
                logger.warning(f"Failed to migrate session {session_file}: {e}")

    return migrated
