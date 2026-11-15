"""agent 执行日志的状态/阶段契约。

status 只表达生命周期（running + 终态集合），phase 表达当前阶段
（running/check/handoff/finalizing），阶段不得写进 status —— 否则前端会把
「检查阶段」误渲染成「任务已结束」。
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.session_runner import TERMINAL_STATUSES, SessionRunner


def _make_runner(tmp_path, **kwargs):
    """构造 worker runner，mock 掉工具与 LLM client。"""
    config = MagicMock()
    config.system.max_iterations = 20
    with patch("core.session_runner.create_tools") as mock_tools, \
         patch("core.session_runner.create_llm_client") as mock_client:
        mock_tools.return_value = []
        mock_client.return_value = MagicMock()
        runner = SessionRunner(config, role="worker", task="t", exec_id="exec_t", **kwargs)
    runner.session_dir = tmp_path
    runner.model = SimpleNamespace(name="mock-model")
    return runner


def _read_meta(tmp_path) -> dict:
    data = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    return data["metadata"]


class TestProgressSnapshot:
    def test_progress_writes_running_status_with_phase(self, tmp_path):
        """进度落盘：status 恒为 running，阶段只进 phase。"""
        runner = _make_runner(tmp_path)
        runner._save_progress(3, phase="check", force=True)

        meta = _read_meta(tmp_path)
        assert meta["status"] == "running"
        assert meta["phase"] == "check"

    def test_progress_event_carries_status_and_phase(self, tmp_path):
        """session_progress 事件同时带 status=running 与 phase。"""
        runner = _make_runner(tmp_path)
        events = []
        runner._event_publisher = lambda ev_type, **kw: events.append((ev_type, kw))

        runner._save_progress(2, phase="running", force=True)
        runner._save_progress(3, phase="check", force=True)

        assert [e[0] for e in events] == ["session_progress", "session_progress"]
        for _, kw in events:
            assert kw["status"] == "running"
        assert [kw["phase"] for _, kw in events] == ["running", "check"]

    def test_throttle_can_be_bypassed(self, tmp_path):
        """force=True 绕过 0.5s 节流，非 force 则被节流。"""
        runner = _make_runner(tmp_path)
        events = []
        runner._event_publisher = lambda ev_type, **kw: events.append(kw)

        runner._save_progress(1, phase="running")
        runner._save_progress(1, phase="check")          # 节流窗口内，丢弃
        runner._save_progress(1, phase="check", force=True)

        assert [e["phase"] for e in events] == ["running", "check"]


class TestFinalize:
    def test_handoff_is_not_terminal(self, tmp_path):
        """handoff 落盘为 running + phase=handoff + exit_reason=handoff。"""
        runner = _make_runner(tmp_path)
        runner._finalize("handoff", "迭代耗尽", 5)

        meta = _read_meta(tmp_path)
        assert meta["status"] == "running"
        assert meta["phase"] == "handoff"
        assert meta["exit_reason"] == "handoff"

    @pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
    def test_terminal_statuses_written_as_is(self, tmp_path, status):
        """终态原样写入 status，phase=done。"""
        runner = _make_runner(tmp_path)
        runner._finalize(status, "done", 5)

        meta = _read_meta(tmp_path)
        assert meta["status"] == status
        assert meta["phase"] == "done"
        assert meta["exit_reason"] == status

    def test_finalize_is_idempotent(self, tmp_path):
        """同一 runner 只写一次最终日志。"""
        runner = _make_runner(tmp_path)
        runner._finalize("completed", "first", 1)
        runner._finalize("failed", "second", 2)

        assert _read_meta(tmp_path)["status"] == "completed"
