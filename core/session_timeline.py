"""会话时间线：只读 messages.jsonl + meta.json，生成 Git 风格事件列表。

设计见 workspace/default/session-about-timeline-design-2026-11-14.md。
不读取 index.json，不兼容推断旧会话的压缩信息。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.session import MESSAGES_FILE, _message_from_line, read_jsonl, read_meta
from core.untrusted import UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END

_SNIPPET_MAX = 200
_SUMMARY_MAX = 8000

_UNTRUSTED_MARKERS = (UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END)

_COMPACTION_TITLES = {
    1: "上下文压缩 · L1 轻量压缩",
    2: "上下文压缩 · L2 完整压缩",
    3: "上下文压缩 · L3 紧急压缩",
}


def _normalize_text(text: str) -> str:
    text = str(text or "")
    for marker in _UNTRUSTED_MARKERS:
        text = text.replace(marker, "")
    return re.sub(r"\s+", " ", text).strip()


def _clip(text: str, limit: int = _SNIPPET_MAX) -> str:
    text = _normalize_text(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "image":
            parts.append("[图片]")
    return "\n".join(part for part in parts if part)


def _blocks(content: Any) -> list[dict[str, Any]]:
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _compaction_event(seq: int, ts: str, meta: dict[str, Any], content: Any) -> dict[str, Any]:
    event_meta = meta.get("compaction_event")
    if isinstance(event_meta, dict):
        layer = int(event_meta.get("layer") or 1)
        return {
            "id": f"msg:{seq}",
            "ts": ts,
            "type": "compaction",
            "title": _COMPACTION_TITLES.get(layer, f"上下文压缩 · L{layer}"),
            "snippet": _clip(_text_from_content(content), _SUMMARY_MAX),
            "meta": {
                "seq": seq,
                "compaction_layer": layer,
                **event_meta,
            },
        }

    compaction = meta.get("compaction") if isinstance(meta.get("compaction"), dict) else {}
    layer = int(compaction.get("layer") or 2)
    return {
        "id": f"msg:{seq}",
        "ts": ts,
        "type": "compaction",
        "title": _COMPACTION_TITLES.get(layer, f"上下文压缩 · L{layer}"),
        "snippet": _clip(_text_from_content(content), _SUMMARY_MAX),
        "meta": {
            "seq": seq,
            "compaction_layer": layer,
            **compaction,
        },
    }


def _tool_event(seq: int, ts: str, block: dict[str, Any]) -> dict[str, Any]:
    tool_id = str(block.get("id") or block.get("tool_use_id") or f"seq{seq}")
    name = str(block.get("name") or block.get("tool_name") or "tool")
    try:
        input_text = json.dumps(block.get("input") or {}, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        input_text = str(block.get("input") or "")
    return {
        "id": f"tool:{tool_id}",
        "ts": ts,
        "type": "tool",
        "title": f"工具调用 · {name}",
        "snippet": _clip(f"{name} {input_text}"),
        "meta": {
            "seq": seq,
            "tool_use_id": tool_id,
            "tool_name": name,
            "tool_status": "ok",
            "tool_result": "",
        },
    }


def _apply_tool_result(event: dict[str, Any], block: dict[str, Any]) -> None:
    result_text = _text_from_content(block.get("content"))
    if not result_text:
        result_text = "结果已保存到文件"
    event["meta"]["tool_result"] = _clip(result_text)
    event["meta"]["tool_status"] = "error" if block.get("is_error") else "ok"


def build_timeline(session_dir: Path | str) -> dict[str, Any]:
    """读取会话目录，返回 {session, events}（事件从旧到新，最新在底部）。"""
    session_dir = Path(session_dir)
    meta = read_meta(session_dir)
    metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    lines = read_jsonl(session_dir / MESSAGES_FILE)

    events: list[dict[str, Any]] = []
    tool_index: dict[str, dict[str, Any]] = {}

    for line in lines:
        seq = line.get("seq")
        if not isinstance(seq, int):
            seq = -1
        msg = _message_from_line(line, None, seq)
        msg_meta = msg.get("_meta") or {}
        content = msg.get("content")
        ts = str(msg_meta.get("created_at") or line.get("created_at") or "")

        if msg_meta.get("compaction_event"):
            events.append(_compaction_event(seq, ts, msg_meta, content))
            continue
        if msg_meta.get("compaction") and msg_meta.get("summary"):
            events.append(_compaction_event(seq, ts, msg_meta, content))
            continue
        if msg_meta.get("summary"):
            continue  # 完整压缩的用户占位消息，不单独出节点

        role = msg.get("role")
        blocks = _blocks(content)

        if role == "assistant":
            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            text = _text_from_content(content)
            if text:
                events.append({
                    "id": f"msg:{seq}",
                    "ts": ts,
                    "type": "assistant",
                    "title": "助手回复",
                    "snippet": _clip(text),
                    "meta": {"seq": seq, "role": "assistant"},
                })
            for block in tool_uses:
                event = _tool_event(seq, ts, block)
                tool_index[event["meta"]["tool_use_id"]] = event
                events.append(event)
            continue

        if role == "user":
            text = _text_from_content(content)
            if text:
                events.append({
                    "id": f"msg:{seq}",
                    "ts": ts,
                    "type": "user",
                    "title": "用户输入",
                    "snippet": _clip(text),
                    "meta": {"seq": seq, "role": "user"},
                })
            for block in blocks:
                if block.get("type") != "tool_result":
                    continue
                tool_id = str(block.get("tool_use_id") or block.get("id") or "")
                event = tool_index.get(tool_id)
                if event is None:
                    fallback_id = tool_id or f"seq{seq}"
                    event = {
                        "id": f"tool:{fallback_id}",
                        "ts": ts,
                        "type": "tool",
                        "title": "工具结果",
                        "snippet": "",
                        "meta": {
                            "seq": seq,
                            "tool_use_id": tool_id,
                            "tool_name": "",
                            "tool_status": "ok",
                            "tool_result": "",
                        },
                    }
                    tool_index[tool_id] = event
                    events.append(event)
                _apply_tool_result(event, block)
            continue

    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    return {
        "session": {
            "session_id": meta.get("session_id") or session_dir.name,
            "name": meta.get("name") or "New Session",
            "created_at": metadata.get("created_at") or "",
            "updated_at": metadata.get("updated_at") or "",
            "hidden": bool(metadata.get("hidden")),
            "model": metadata.get("model_ref") or "",
            "message_count": int(meta.get("message_count") or len(lines)),
            "usage": usage,
            "parent_session_id": metadata.get("parent_session_id") or None,
        },
        "events": events,
    }
