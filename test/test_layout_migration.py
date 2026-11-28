"""本地布局迁移：备份、旧→新路径、幂等。"""

from __future__ import annotations

import json
from pathlib import Path

import core.layout_migration as lm


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_migrate_data_dir_moves_runtime_and_config(tmp_path, monkeypatch):
    data = tmp_path / "ws" / ".cili"
    _write(data / "memory" / "MEMORY.md", "index")
    _write(data / "memory" / "summary.md", "summary")
    _write(data / "memory" / "journal.jsonl", json.dumps({"cursor": 1, "session_key": "s1", "key": "k"}) + "\n")
    _write(data / "workflows" / "wf1" / "workflow.json", "{}")
    _write(data / "workflows" / "wf1" / "state.json", "{}")
    _write(data / "workflows" / "wf1" / "runs" / "r1" / "run.json", "{}")
    _write(data / "cron" / "j1" / "job.json", "{}")
    _write(data / "cron" / "j1" / "state.json", "{}")
    _write(data / "tools" / "todo" / "s1.json", "{}")
    _write(data / "approval_rules.json", json.dumps({"rules": [{"id": "r-a", "kind": "command"}], "constraints": []}))
    _write(data / "sensitive_words.json", json.dumps({"enabled": True, "rules": [{"word": "x", "replacement": "y"}]}))
    _write(data / "courseware" / "quiz-results.jsonl", "{}\n")
    _write(data / "sessions" / "s1" / "index.json", "{}")
    _write(data / "sessions" / "s1" / "call_1.txt", "output")
    _write(data / "sessions" / "s1" / "attachments" / "a.png", "img")
    _write(data / "sessions" / "s1" / "goal.json", "{}")
    _write(data / "sessions" / "s1" / "index.json.legacy", "{}")
    _write(data / "tmp" / "downloads" / "d.txt", "download")

    monkeypatch.setattr(lm, "_backup", lambda p: tmp_path / "backup")

    assert lm.migrate_data_dir(data) is True

    assert (data / "runtime" / "workflows" / "wf1" / "state.json").is_file()
    assert (data / "runtime" / "workflows" / "wf1" / "runs" / "r1" / "run.json").is_file()
    assert (data / "runtime" / "cron" / "j1" / "state.json").is_file()
    assert (data / "runtime" / "todo" / "s1.json").is_file()
    assert (data / "runtime" / "memory" / "sessions" / "s1" / "journal.jsonl").is_file()
    assert (data / "cache" / "memory" / "MEMORY.md").is_file()
    assert (data / "config" / "approval_rules" / "r-a.json").is_file()
    assert (data / "config" / "sensitive_words" / "settings.json").is_file()
    assert (data / "cache" / "tool_output" / "s1" / "call_1.txt").is_file()
    assert (data / "cache" / "tool_output" / "s1" / "attachments" / "a.png").is_file()
    assert (tmp_path / "ws" / "downloads" / "d.txt").is_file()
    assert (data / "sessions" / "s1" / "goal.json").exists() is False
    assert (data / "sessions" / "s1" / "index.json.legacy").exists() is False
    assert (data / "runtime" / ".layout-migrated").is_file()
    assert lm.migrate_data_dir(data) is False
