"""Migration utilities for session format upgrade.

Migrates old session format (block-level _valid, _compacted, etc.)
to new format (message-level _meta).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Internal meta fields (new format)
INTERNAL_META_FIELDS = {"valid", "compacted", "output_path", "file_size", "truncated", "tool_name", "multimodal"}


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
        if migrate_todos_from_metadata(metadata, session_file.parent.name):
            modified = True

    # Save if modified
    if modified:
        try:
            with open(session_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"Migrated session: {session_file}")
        except Exception as e:
            logger.error(f"Failed to save migrated session {session_file}: {e}")
            return False

    return modified


def migrate_message(msg: dict) -> bool:
    """Migrate a single message from old format to new format.

    Old format: block-level _valid, _compacted, _output_path, etc.
    New format: message-level _meta with these fields.

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
    meta_fields: dict[str, Any] = {}

    for block in content:
        if not isinstance(block, dict):
            continue

        # Check for block-level internal fields
        if "_valid" in block:
            needs_migration = True
            # Block-level _valid → message-level _meta.valid
            # If any block is invalid, the whole message is invalid
            if block.pop("_valid") is False:
                meta_fields["valid"] = False

        if "_compacted" in block:
            needs_migration = True
            if block.pop("_compacted"):
                meta_fields["compacted"] = True

        if "_output_path" in block:
            needs_migration = True
            meta_fields["output_path"] = block.pop("_output_path")

        if "_file_size" in block:
            needs_migration = True
            meta_fields["file_size"] = block.pop("_file_size")

        if "_truncated" in block:
            needs_migration = True
            meta_fields["truncated"] = block.pop("_truncated")

        # tool_name → _meta.tool_name
        if "tool_name" in block:
            needs_migration = True
            meta_fields["tool_name"] = block.pop("tool_name")

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

        # _meta.wait_for_user → _meta.completed (inverted semantics)
        if "_meta" in block and isinstance(block["_meta"], dict):
            block_meta = block["_meta"]
            if "wait_for_user" in block_meta:
                needs_migration = True
                wfu = block_meta.pop("wait_for_user")
                # wait_for_user=True → completed=False (waiting)
                # wait_for_user=False → completed=True (answered)
                meta_fields["completed"] = not wfu

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
                        # Remove _valid from sub-blocks (new format is message-level)
                        sub.pop("_valid", None)
                        sub.pop("_compacted", None)

    # Apply migrated meta fields to message
    if needs_migration or meta_fields:
        if "_meta" not in msg:
            msg["_meta"] = {}
        msg["_meta"].update(meta_fields)
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
        from core.tools.shared.todo import write_todos
        write_todos(session_id, todos)
        del metadata["todos"]
        logger.info(f"Migrated {len(todos)} todos from session {session_id} to independent file")
        return True
    except Exception as e:
        logger.warning(f"Failed to migrate todos for session {session_id}: {e}")
        return False


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
            except Exception as e:
                logger.warning(f"Failed to migrate session {session_file}: {e}")

    return migrated
