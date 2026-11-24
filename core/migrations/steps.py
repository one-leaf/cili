"""已登记的迁移步骤（顺序即执行顺序；新增破坏性变更在此追加）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

_PLACEHOLDER_TOOLS = {"ask_user"}


def migrate_placeholder_meta(workspaces_json: Path, get_data_dir) -> int:
    """会话占位标记 ``completed`` → ``ask_user_completed``（旧会话一次性重写）。

    只改 messages.jsonl 中 tool_result 块的 _meta；幂等（已迁移的键存在即跳过）。
    """
    changed = 0
    try:
        entries = json.loads(Path(workspaces_json).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(entries, list):
        return 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        uuid = entry.get("uuid", "")
        sessions_dir = Path(get_data_dir(uuid)) / "sessions"
        if not sessions_dir.is_dir():
            continue
        for session_dir in sessions_dir.iterdir():
            jsonl = session_dir / "messages.jsonl"
            if not jsonl.is_file():
                continue
            try:
                lines = jsonl.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            out: list[str] = []
            touched = False
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    out.append(line)
                    continue
                if _rewrite_record(record):
                    touched = True
                    out.append(json.dumps(record, ensure_ascii=False))
                else:
                    out.append(line)
            if touched:
                atomic_write_text(jsonl, "\n".join(out) + "\n")
                changed += 1
    return changed


def _rewrite_record(record: dict) -> bool:
    content = record.get("content")
    if not isinstance(content, list):
        return False
    touched = False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        meta = block.get("_meta")
        if not isinstance(meta, dict) or "completed" not in meta:
            continue
        if "ask_user_completed" in meta:
            continue
        meta["ask_user_completed"] = bool(meta["completed"])
        block["_meta"] = meta
        touched = True
    return touched