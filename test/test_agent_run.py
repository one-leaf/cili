"""Tests for AgentRun records, continuation requests and fork support."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.agent_run import AgentRunManager, AgentRunRecord, AgentRunStore
from core.session_runner import SessionRunner
from test.conftest import make_dgx_config


def _worker(tmp_path, *, epoch=1, max_epochs=3, role="worker", depth=1):
    config = make_dgx_config("anthropic")
    with patch("core.session_runner.create_llm_client", return_value=MagicMock()), \
         patch("core.session_runner.create_tools", return_value=[]):
        runner = SessionRunner(
            config,
            role=role,
            task="task",
            workspace_uuid="test-workspace",
            session_dir=tmp_path / "exec_x",
            exec_id="exec_x",
            master_session_id="root",
            delegation_depth=depth,
            epoch=epoch,
            max_epochs=max_epochs,
        )
    runner.add_message("user", "task")
    return runner


def test_agent_run_store_roundtrip(tmp_path):
    store = AgentRunStore(tmp_path)
    rec = AgentRunRecord(
        exec_id="exec_x", root_session_id="root", role="worker",
        task="t", max_epochs=3,
    )
    store.create(rec)
    loaded = store.get("exec_x")
    assert loaded is not None
    assert loaded.status == "created"

    store.archive_epoch(
        "exec_x", epoch=1, status="completed", exit_reason="completed",
        truncated=False, iterations=2, max_iterations=200, summary="done",
        messages=[{"role": "assistant", "content": "ok"}],
    )
    loaded = store.get("exec_x")
    assert loaded is not None
    assert loaded.status == "completed"
    assert len(loaded.epochs) == 1
    assert loaded.epochs[0].epoch == 1
    assert (tmp_path / "exec_x" / "epochs" / "1.json").exists()
    data = json.loads((tmp_path / "exec_x" / "epochs" / "1.json").read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["ended_at"]


def test_agent_run_manager_delivery(tmp_path):
    mgr = AgentRunManager(tmp_path)
    mgr.create(
        exec_id="exec_y", root_session_id="root", role="worker",
        task="t", max_epochs=1,
    )
    mgr.start("exec_y", epoch=1)
    mgr.complete(
        "exec_y", status="completed", exit_reason="completed",
        truncated=False, summary="ok",
    )
    assert mgr.store.get("exec_y").delivery_state == "pending"
    mgr.delivered("exec_y")
    assert mgr.store.get("exec_y").delivery_state == "delivered"


def test_worker_budget_exhaustion_builds_continuation(tmp_path):
    runner = _worker(tmp_path)
    runner.runner.summarize_for_handoff = MagicMock(return_value="HANDOFF DOC")
    result = runner.build_continuation_request(200)
    assert result is not None
    assert result["status"] == "handoff"
    assert result["epoch"] == 1
    assert result["max_epochs"] == 3
    assert result["truncated"] is True
    assert result["continuation"]["document"] == "HANDOFF DOC"
    assert result["continuation"]["epoch"] == 1


def test_continuation_respects_epoch_limit(tmp_path):
    runner = _worker(tmp_path, epoch=3, max_epochs=3)
    runner.runner.summarize_for_handoff = MagicMock(return_value="HANDOFF DOC")
    assert runner.build_continuation_request(200) is None


def test_continuation_only_for_worker(tmp_path):
    runner = _worker(tmp_path, role="lite", max_epochs=3)
    runner.runner.summarize_for_handoff = MagicMock(return_value="HANDOFF DOC")
    assert runner.build_continuation_request(200) is None


def test_fork_session_snapshot(tmp_path):
    from core.session import SessionStore

    config = make_dgx_config("anthropic")
    store = SessionStore.create_new_session(tmp_path, "source")
    with patch("core.session_runner.create_llm_client", return_value=MagicMock()),          patch("core.session_runner.create_tools", return_value=[]):
        runner = SessionRunner(
            config, role="master", cwd=str(tmp_path),
            workspace_uuid="test-workspace", initial_session=store,
        )
    runner.add_message("user", "hello")
    runner.add_message("assistant", "world")
    result = runner.fork_session(label="branch")
    assert result["status"] == "idle"
    assert result["mode"] == "snapshot"
    new_id = result["session_id"]
    loaded = SessionStore.load_session(new_id, runner.sessions_dir)
    assert loaded is not None
    assert [m["role"] for m in loaded.messages] == ["user", "assistant"]
