"""Parse cw-q entries from 错题本.md and merge quiz-results.jsonl.

Usage:
    python parse_mistakes.py <错题本.md> [quiz-results.jsonl] --out mistakes.json

Output: JSON array of normalized entries. Classification is left to the agent:
each entry has ``error_type`` (may be empty) and ``inferred`` (false until the
agent fills a type).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ENTRY_RE = re.compile(r"<!-- cw-q:begin (\{.*?\}) -->(.*?)<!-- cw-q:end -->", re.S)
FIELD_RE = re.compile(r"^-[ \t]*([^：:\n]+)[：:][ \t]*(.*)$", re.M)

FIELD_MAP = {
    "题目": "question",
    "选项": "options_text",
    "我的答案": "my_answer",
    "正确答案": "correct_answer",
    "解析": "analysis",
    "正确思路": "analysis",
    "错因": "error_type",
    "同类变式": "variant",
    "来源": "source",
    "复习日期": "review_date",
}


def parse_book(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    entries: list[dict[str, Any]] = []
    for match in ENTRY_RE.finditer(text):
        try:
            meta = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        body = match.group(2)
        fields: dict[str, str] = {}
        for label, value in FIELD_RE.findall(body):
            key = FIELD_MAP.get(label.strip())
            if key:
                fields[key] = value.strip()
        entries.append({
            "id": str(meta.get("id") or ""),
            "date": str(meta.get("date") or ""),
            "courseware": str(meta.get("courseware") or ""),
            "question_id": str(meta.get("qid") or ""),
            "error_type": str(meta.get("error_type") or fields.get("error_type") or ""),
            "question": fields.get("question", ""),
            "options_text": fields.get("options_text", ""),
            "my_answer": fields.get("my_answer", ""),
            "correct_answer": fields.get("correct_answer", ""),
            "analysis": fields.get("analysis", ""),
            "variant": fields.get("variant", ""),
            "source": fields.get("source", ""),
            "review_date": fields.get("review_date", ""),
            "inferred": False,
        })
    return entries


def merge_events(entries: list[dict[str, Any]], events_path: Path) -> int:
    if not events_path.is_file():
        return 0
    by_key: dict[str, dict[str, Any]] = {}
    for entry in entries:
        by_key[f"{entry['courseware']}#{entry['question_id']}"] = entry
    merged = 0
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict) or ev.get("is_correct"):
            continue
        key = f"{ev.get('courseware', '')}#{ev.get('question_id', '')}"
        entry = by_key.get(key)
        if entry is None:
            continue
        if not entry.get("question") and ev.get("question"):
            entry["question"] = ev.get("question")
        if not entry.get("my_answer") and ev.get("selected") is not None:
            selected = ev.get("selected")
            entry["my_answer"] = "、".join(selected) if isinstance(selected, list) else str(selected)
        if not entry.get("correct_answer") and ev.get("answer") is not None:
            answer = ev.get("answer")
            entry["correct_answer"] = "、".join(answer) if isinstance(answer, list) else str(answer)
        merged += 1
    return merged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("book")
    parser.add_argument("events", nargs="?")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    book = Path(args.book).resolve()
    entries = parse_book(book)
    merged = merge_events(entries, Path(args.events).resolve()) if args.events else 0
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "success": True,
        "entries": len(entries),
        "merged_events": merged,
        "output": str(out),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
