"""课件技能：渲染、记录与错题解析的最小闭环测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "core" / "skills" / "courseware" / "scripts"
sys.path.insert(0, str(ROOT))

from core.courseware_records import record_quiz_result  # noqa: E402


def _minimal_plan() -> dict:
    return {
        "schema_version": "1.0",
        "template": "parchment-v1",
        "mode": "chapter",
        "meta": {"title": "测试课件", "subject": "数学", "grade": "五年级"},
        "scenes": [
            {"id": "s1", "layout": "cover", "title": "测试课件", "subtitle": "副标题"},
            {
                "id": "s2",
                "layout": "concept",
                "title": "概念",
                "blocks": [
                    {"type": "text", "style": "lead", "text": "把单位「1」平均分。"},
                    {"type": "formula", "latex": "\\frac{3}{4}", "display": True},
                    {"type": "callout", "tone": "key", "text": "分母是分成的份数。"},
                ],
            },
            {
                "id": "s3",
                "layout": "quiz",
                "title": "练习",
                "questions": [
                    {
                        "id": "q1",
                        "type": "single",
                        "stem": "1/5 表示什么？",
                        "options": ["五份中的一份", "五份", "一份中的五份"],
                        "answer": "A",
                        "explain": "平均分成五份，取一份。",
                    }
                ],
            },
        ],
    }


def test_render_minimal_courseware(tmp_path: Path):
    plan = tmp_path / "课件.plan.json"
    out = tmp_path / "课件.html"
    plan.write_text(json.dumps(_minimal_plan(), ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_html.py"), str(plan), str(out)],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    html = out.read_text(encoding="utf-8")
    import re
    assert re.search(r"\{\{[A-Z_]+\}\}", html) is None
    assert 'class="cw-scene' in html
    assert "data-tex=" in html
    assert re.search(r"""(?:src|href)\s*=\s*["']https?://|url\(\s*["']?https?://""", html, re.I) is None
    assert "cw-quiz" in html


def test_render_rejects_unknown_layout(tmp_path: Path):
    plan = _minimal_plan()
    plan["scenes"][1]["layout"] = "poster"
    plan_path = tmp_path / "bad.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_html.py"), str(plan_path), str(tmp_path / "bad.html")],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 2
    assert "layout" in result.stdout


def test_record_quiz_result_writes_jsonl_and_mistake_book(tmp_path: Path):
    (tmp_path / "学习资料").mkdir()
    payload = {
        "workspace_uuid": "test",
        "courseware": "测试课件",
        "courseware_path": str(tmp_path / "学习资料" / "课件" / "测试课件" / "课件.html"),
        "scene_id": "s3",
        "question_id": "q1",
        "question": "1/5 表示什么？",
        "options": ["五份中的一份", "五份", "一份中的五份"],
        "selected": "B",
        "answer": "A",
        "explain": "平均分成五份，取一份。",
        "attempt_id": "att-1",
        "is_correct": False,
        "ts": "2026-11-13T10:00:00+08:00",
    }
    receipt = record_quiz_result(tmp_path, payload)
    assert receipt["recorded"] is True

    events = (tmp_path / ".cili" / "courseware" / "quiz-results.jsonl").read_text(encoding="utf-8")
    assert "q1" in events
    book = (tmp_path / "错题本.md").read_text(encoding="utf-8")
    assert "cw-q:begin" in book
    assert "我的答案：B" in book
    assert "正确答案：A" in book
    assert "平均分成五份" in book
    assert (tmp_path / "学习资料" / "成绩记录.html").is_file()


def test_parse_mistakes_reads_fields(tmp_path: Path):
    book = tmp_path / "错题本.md"
    book.write_text(
        "# 错题本\n\n<!-- courseware:mistakes-start -->\n"
        '<!-- cw-q:begin {"id":"t#q1","date":"2026-11-13","courseware":"测试","qid":"q1","error_type":"概念不清"} -->\n'
        "### 2026-11-13 · 《测试》 q1\n"
        "- 题目：1/5 表示什么？\n"
        "- 选项：A. 一份　B. 五份\n"
        "- 我的答案：B\n"
        "- 正确答案：A\n"
        "- 解析：平均分。\n"
        "- 错因：概念不清\n"
        "- 来源：课件\n"
        "<!-- cw-q:end -->\n<!-- courseware:mistakes-end -->\n",
        encoding="utf-8",
    )
    out = tmp_path / "mistakes.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "parse_mistakes.py"), str(book), "--out", str(out)],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    entries = json.loads(out.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["question"].startswith("1/5")
    assert entries[0]["analysis"] == "平均分。"
    assert entries[0]["error_type"] == "概念不清"
