"""Courseware quiz records: JSONL events, mistake book, score history page.

Kept in ``core`` (not the web layer) so the web route and the skill scripts share
one implementation. All writes are workspace-scoped and append-only for events.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from core.fs_utils import atomic_write_text

_LOCK = threading.RLock()

MIST_START = "<!-- courseware:mistakes-start -->"
MIST_END = "<!-- courseware:mistakes-end -->"
ENTRY_RE = re.compile(r"<!-- cw-q:begin (\{.*?\}) -->(.*?)<!-- cw-q:end -->", re.S)

_MAX_TEXT = 2000
_MAX_SHORT = 200
_MAX_OPTIONS = 12


def _clean(value: Any, limit: int = _MAX_SHORT) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _answer_text(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(_clean(v, 120) for v in value)
    return _clean(value, 200)


def _entry_id(payload: dict[str, Any]) -> str:
    courseware = _clean(payload.get("courseware"), 60) or "courseware"
    slug = re.sub(r"[^\w一-鿿-]+", "-", courseware).strip("-") or "courseware"
    qid = _clean(payload.get("question_id"), 60) or "q"
    path = _clean(payload.get("courseware_path"), 500)
    digest = hashlib.sha1((path + "#" + qid).encode("utf-8")).hexdigest()[:6]
    return f"{slug[:24]}-{digest}#{qid}"


def output_root(workspace_root: Path) -> Path:
    workspace_root = workspace_root.resolve()
    for name in ("学习资料", "演示", "输出", "课件"):
        candidate = workspace_root / name
        if candidate.is_dir():
            return candidate
    return workspace_root


def scores_path(workspace_root: Path) -> Path:
    return output_root(workspace_root) / "成绩记录.html"


def mistakes_path(workspace_root: Path, courseware_path: Path | None) -> Path:
    workspace_root = workspace_root.resolve()
    if (workspace_root / "学习资料").is_dir() or (workspace_root / "错题本.md").is_file():
        return workspace_root / "错题本.md"
    if courseware_path is not None:
        return courseware_path.resolve().parent / "错题本.md"
    return workspace_root / "错题本.md"


def _events_path(workspace_root: Path) -> Path:
    return workspace_root.resolve() / ".cili" / "courseware" / "quiz-results.jsonl"


def _append_event(workspace_root: Path, event: dict[str, Any]) -> None:
    path = _events_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False)
    with _LOCK:
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")


def _format_entry(payload: dict[str, Any], entry_id: str) -> str:
    date = _clean(payload.get("date") or datetime.now().strftime("%Y-%m-%d"), 20)
    courseware = _clean(payload.get("courseware"), 120)
    qid = _clean(payload.get("question_id"), 60)
    question = _clean(payload.get("question"), _MAX_TEXT)
    options = [ _clean(o, 300) for o in (payload.get("options") or []) ][:_MAX_OPTIONS]
    letters = [chr(65 + i) for i in range(len(options))]
    options_text = "　".join(f"{letter}. {opt}" for letter, opt in zip(letters, options))
    selected = _answer_text(payload.get("selected"))
    answer = _answer_text(payload.get("answer"))
    explain = _clean(payload.get("explain"), _MAX_TEXT)
    error_type = _clean(payload.get("error_type"), 40) or "待分类"
    source = _clean(payload.get("source"), 300) or _clean(payload.get("courseware_path"), 300)
    meta = {
        "id": entry_id,
        "date": date,
        "courseware": courseware,
        "qid": qid,
        "error_type": error_type,
    }
    return (
        f'<!-- cw-q:begin {json.dumps(meta, ensure_ascii=False)} -->\n'
        f"### {date} · 《{courseware}》 {qid}\n"
        f"- 题目：{question}\n"
        f"- 选项：{options_text}\n"
        f"- 我的答案：{selected}\n"
        f"- 正确答案：{answer}\n"
        f"- 解析：{explain}\n"
        f"- 错因：{error_type}\n"
        f"- 同类变式：\n"
        f"- 来源：{source}\n"
        f"- 复习日期：\n"
        f"<!-- cw-q:end -->"
    )


def _update_mistake_book(path: Path, payload: dict[str, Any]) -> None:
    text = path.read_text(encoding="utf-8") if path.is_file() else "# 错题本\n"
    if MIST_START not in text or MIST_END not in text:
        text = text.rstrip() + (
            "\n\n## 课件测验错题\n\n" + MIST_START + "\n" + MIST_END + "\n"
        )
    entry_id = _entry_id(payload)
    entry = _format_entry(payload, entry_id)
    with _LOCK:
        found = False
        def repl(match: re.Match[str]) -> str:
            nonlocal found
            try:
                meta = json.loads(match.group(1))
            except json.JSONDecodeError:
                return match.group(0)
            if meta.get("id") == entry_id:
                found = True
                return entry
            return match.group(0)

        text = ENTRY_RE.sub(repl, text)
        if not found:
            text = text.replace(MIST_END, entry + "\n" + MIST_END, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text)


def _read_events(workspace_root: Path) -> list[dict[str, Any]]:
    path = _events_path(workspace_root)
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _score_page_html(workspace_root: Path, events: list[dict[str, Any]]) -> str:
    attempts: dict[str, dict[str, Any]] = {}
    for ev in events:
        attempt = _clean(ev.get("attempt_id"), 80) or "未命名尝试"
        bucket = attempts.setdefault(attempt, {
            "attempt": attempt,
            "ts": _clean(ev.get("ts"), 40),
            "courseware": _clean(ev.get("courseware"), 120),
            "questions": {},
        })
        ts = _clean(ev.get("ts"), 40)
        if ts > bucket["ts"]:
            bucket["ts"] = ts
        qid = _clean(ev.get("question_id"), 80) or f"q{len(bucket['questions']) + 1}"
        previous = bucket["questions"].get(qid)
        if previous is None or ts >= _clean(previous.get("ts"), 40):
            bucket["questions"][qid] = ev

    rows = []
    for bucket in attempts.values():
        questions = list(bucket["questions"].values())
        rows.append({
            "attempt": bucket["attempt"],
            "ts": bucket["ts"],
            "courseware": bucket["courseware"],
            "total": len(questions),
            "correct": sum(1 for ev in questions if bool(ev.get("is_correct"))),
        })
    rows.sort(key=lambda x: x["ts"], reverse=True)
    total_attempts = len(rows)
    total_questions = sum(r["total"] for r in rows)
    total_correct = sum(r["correct"] for r in rows)
    avg = (total_correct / total_questions * 100) if total_questions else 0.0
    total_wrong = total_questions - total_correct

    row_html = []
    for r in rows:
        ts = r["ts"].replace("T", " ")[:19] if r["ts"] else ""
        total = r["total"]
        correct = r["correct"]
        wrong = total - correct
        rate = (correct / total * 100) if total else 0.0
        row_html.append(
            "<tr>"
            f"<td>{html.escape(ts)}</td>"
            f"<td>{html.escape(r['courseware'])}</td>"
            f"<td>{total}</td><td>{correct}</td><td>{wrong}</td>"
            f"<td>{rate:.0f}%</td>"
            "</tr>"
        )
    if not row_html:
        row_html.append('<tr><td colspan="6" class="empty">还没有答题记录。打开课件做几道题吧。</td></tr>')

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>成绩记录</title>
<style>
:root{{--bg:#fbe8be;--paper:#fff6df;--ink:#4a3423;--soft:#7a6149;--line:#8b6b4a;--accent:#f2812f;--sun:#f7c548}}
*{{box-sizing:border-box}}body{{margin:0;padding:28px;background:var(--bg);color:var(--ink);
font-family:"Microsoft YaHei","PingFang SC",sans-serif}}
.wrap{{max-width:980px;margin:0 auto}}
h1{{font-family:"YouYuan","幼圆","Microsoft YaHei",sans-serif;font-size:34px;margin:0 0 6px}}
.sub{{color:var(--soft);margin:0 0 18px}}
.cards{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:18px}}
.card{{background:var(--sun);border:3px solid var(--line);border-radius:18px;padding:12px 18px;
box-shadow:5px 5px 0 rgba(74,52,35,.15);min-width:150px}}
.card b{{display:block;font-size:28px;font-family:"YouYuan","幼圆",sans-serif}}
.card span{{font-size:13px;color:var(--soft)}}
table{{width:100%;border-collapse:separate;border-spacing:0;background:var(--paper);
border:3px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:6px 6px 0 rgba(74,52,35,.14)}}
th,td{{padding:11px 14px;border-bottom:2px solid rgba(139,107,74,.28);text-align:left;font-size:15px}}
th{{background:#ffe7bd;font-family:"YouYuan","幼圆",sans-serif;font-size:15px}}
tr:last-child td{{border-bottom:none}}
.empty{{text-align:center;color:var(--soft);padding:30px}}
.note{{margin-top:14px;color:var(--soft);font-size:13px}}
</style></head>
<body><div class="wrap">
<h1>成绩记录</h1>
<p class="sub">来自 cili 课件的测验记录 · 更新于 {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
<div class="cards">
<div class="card"><b>{total_attempts}</b><span>答题次数</span></div>
<div class="card"><b>{total_questions}</b><span>累计作答</span></div>
<div class="card"><b>{avg:.0f}%</b><span>平均正确率</span></div>
<div class="card"><b>{total_wrong}</b><span>累计错题</span></div>
</div>
<table><thead><tr><th>时间</th><th>课件</th><th>题数</th><th>正确</th><th>错题</th><th>正确率</th></tr></thead>
<tbody>{''.join(row_html)}</tbody></table>
<p class="note">错题详情见工作区的「错题本.md」。本页每次答题后自动刷新。</p>
</div></body></html>"""


def regenerate_scores(workspace_root: Path) -> Path:
    path = scores_path(workspace_root)
    html_text = _score_page_html(workspace_root, _read_events(workspace_root))
    atomic_write_text(path, html_text)
    return path


def record_quiz_result(workspace_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist one quiz event. Returns a small receipt."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    question_id = _clean(payload.get("question_id"), 80)
    if not question_id:
        raise ValueError("question_id required")
    raw_correct = payload.get("is_correct")
    is_correct = raw_correct if isinstance(raw_correct, bool) else str(raw_correct).lower() == "true"
    event = {
        "workspace_uuid": _clean(payload.get("workspace_uuid"), 40),
        "courseware": _clean(payload.get("courseware"), 120),
        "courseware_path": _clean(payload.get("courseware_path"), 500),
        "scene_id": _clean(payload.get("scene_id"), 80),
        "question_id": question_id,
        "question": _clean(payload.get("question"), _MAX_TEXT),
        "options": [_clean(o, 300) for o in (payload.get("options") or [])][:_MAX_OPTIONS],
        "selected": payload.get("selected") if isinstance(payload.get("selected"), list) else _clean(payload.get("selected"), 200),
        "answer": payload.get("answer") if isinstance(payload.get("answer"), list) else _clean(payload.get("answer"), 200),
        "explain": _clean(payload.get("explain"), _MAX_TEXT),
        "error_type": _clean(payload.get("error_type"), 40),
        "source": _clean(payload.get("source"), 300),
        "attempt_id": _clean(payload.get("attempt_id"), 80),
        "is_correct": is_correct,
        "ts": _clean(payload.get("ts"), 40) or datetime.now().isoformat(timespec="seconds"),
        "entry_id": _entry_id(payload),
    }
    _append_event(workspace_root, event)
    if not is_correct:
        courseware_path = Path(event["courseware_path"]) if event["courseware_path"] else None
        _update_mistake_book(mistakes_path(workspace_root, courseware_path), event)
    score_path = regenerate_scores(workspace_root)
    return {
        "recorded": True,
        "entry_id": event["entry_id"],
        "scores_path": str(score_path),
    }
