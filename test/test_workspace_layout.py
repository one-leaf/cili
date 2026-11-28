"""工作区 runtime/cache/config/tmp 路径助手。"""

from __future__ import annotations

from pathlib import Path

import core.workspace_layout as layout


def test_layout_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(
        layout, "get_workspace_data_dir", lambda uuid: tmp_path / uuid / ".cili"
    )
    base = tmp_path / "ws" / ".cili"

    assert layout.workspace_runtime_dir("ws") == base / "runtime"
    assert layout.workspace_cache_dir("ws") == base / "cache"
    assert layout.workspace_config_dir("ws") == base / "config"
    assert layout.workspace_tmp_dir("ws") == base / "tmp"
    assert layout.todo_dir("ws") == base / "runtime" / "todo"
    assert layout.memory_session_state_dir("ws", "s1") == (
        base / "runtime" / "memory" / "sessions" / "s1"
    )
    assert layout.tool_output_dir("ws", "call_1") == (
        base / "cache" / "tool_output" / "call_1"
    )


def test_segment_sanitizes_path_chars(tmp_path, monkeypatch):
    monkeypatch.setattr(
        layout, "get_workspace_data_dir", lambda uuid: tmp_path / uuid / ".cili"
    )
    assert layout.tool_output_dir("ws", "../a/b") == (
        tmp_path / "ws" / ".cili" / "cache" / "tool_output" / "a_b"
    )
    assert layout.memory_session_state_dir("ws", "") == (
        tmp_path / "ws" / ".cili" / "runtime" / "memory" / "sessions" / "unknown"
    )
