"""Exec log stats / epoch merge tests."""

import json
from types import SimpleNamespace

from core.agent_run import AgentRunManager, AgentRunStore
from core.agent_stats import AgentStats
from core.session import ExecLogStore


def _exec_store(tmp_path):
    return ExecLogStore(SimpleNamespace(session_dir=tmp_path, session_id="s1"))


def _write_index(tmp_path, messages, stats, status="running"):
    exec_dir = tmp_path / "exec_x"
    exec_dir.mkdir(exist_ok=True)
    (exec_dir / "index.json").write_text(json.dumps({
        "exec_id": "exec_x",
        "session_id": "s1",
        "task": "task",
        "summary": "",
        "metadata": {"status": status, "stats": stats.to_dict()},
        "messages": messages,
    }), encoding="utf-8")


def test_agent_stats_add_and_roundtrip():
    total = AgentStats(1, 2, 3) + AgentStats(2, 3, 4)
    assert total.to_dict() == {
        "iterations": 3,
        "message_count": 5,
        "tool_call_count": 7,
    }
    assert AgentStats.from_dict({"iterations": "5", "message_count": None}) == AgentStats(5, 0, 0)
    assert AgentStats.from_dict(None) == AgentStats()


def test_load_execution_merges_running_epoch(tmp_path):
    mgr = AgentRunManager(tmp_path)
    mgr.create(exec_id="exec_x", root_session_id="s1", role="worker", task="t", max_epochs=2)
    mgr.start("exec_x", epoch=1)
    mgr.archive_epoch(
        "exec_x", epoch=1, status="handoff", exit_reason="max_iterations",
        truncated=True, max_iterations=10, summary="h",
        stats=AgentStats(iterations=2, message_count=1, tool_call_count=3),
        messages=[{"role": "assistant", "content": "a1"}],
    )
    mgr.start("exec_x", epoch=2)
    _write_index(
        tmp_path,
        [{"role": "assistant", "content": "a2"}],
        AgentStats(iterations=3, message_count=2, tool_call_count=4),
    )

    payload = _exec_store(tmp_path).load_execution("exec_x")

    assert [m["_meta"]["epoch"] for m in payload["messages"]] == [1, 2]
    assert payload["stats"] == {
        "iterations": 3,
        "message_count": 2,
        "tool_call_count": 4,
    }
    assert [e["epoch"] for e in payload["epochs"]] == [1, 2]


def test_load_execution_does_not_duplicate_archived_epoch(tmp_path):
    mgr = AgentRunManager(tmp_path)
    mgr.create(exec_id="exec_x", root_session_id="s1", role="worker", task="t")
    mgr.start("exec_x", epoch=1)
    mgr.archive_epoch(
        "exec_x", epoch=1, status="completed", exit_reason="completed",
        truncated=False, max_iterations=10, summary="done",
        stats=AgentStats(iterations=2, message_count=1, tool_call_count=3),
        messages=[{"role": "assistant", "content": "a1"}],
    )
    _write_index(
        tmp_path,
        [{"role": "assistant", "content": "a1"}],
        AgentStats(iterations=2, message_count=1, tool_call_count=3),
        status="completed",
    )

    payload = _exec_store(tmp_path).load_execution("exec_x")

    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["_meta"]["epoch"] == 1
    assert payload["status"] == "completed"


def test_agent_run_store_ignores_unreadable_run_json(tmp_path):
    exec_dir = tmp_path / "exec_x"
    exec_dir.mkdir()
    (exec_dir / "run.json").write_text(
        json.dumps({"exec_id": "exec_x", "epochs": [{"epoch": 1, "iterations": 2}]}),
        encoding="utf-8",
    )
    assert AgentRunStore(tmp_path).get("exec_x") is None
