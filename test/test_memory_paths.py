"""记忆索引/摘要旧路径与新 cache 路径的兼容读取。"""

from __future__ import annotations

from core.memory_paths import read_text, resolve_existing


def test_prefers_cache_when_present(tmp_path):
    memory = tmp_path / ".cili" / "memory"
    cache = tmp_path / ".cili" / "cache" / "memory"
    memory.mkdir(parents=True)
    cache.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("old", encoding="utf-8")
    (cache / "MEMORY.md").write_text("new", encoding="utf-8")

    assert resolve_existing(memory, "MEMORY.md") == cache / "MEMORY.md"
    assert read_text(memory, "MEMORY.md") == "new"


def test_falls_back_to_old(tmp_path):
    memory = tmp_path / ".cili" / "memory"
    memory.mkdir(parents=True)
    (memory / "summary.md").write_text("legacy", encoding="utf-8")

    assert resolve_existing(memory, "summary.md") == memory / "summary.md"
    assert read_text(memory, "summary.md") == "legacy"


def test_missing_file_is_empty(tmp_path):
    memory = tmp_path / ".cili" / "memory"
    memory.mkdir(parents=True)
    assert read_text(memory, "summary.md") == ""
