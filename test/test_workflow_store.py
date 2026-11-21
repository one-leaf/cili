"""工作流存储：工作流/状态读写、运行日志与保留策略。"""

import time

from core.workflows import store
from core.workflows.model import StepState, WorkflowState

WS = "test"


def sample_payload():
    return {
        "name": "测试工作流",
        "description": "desc",
        "steps": [
            {"name": "第一步", "requirement": "做事", "sources": []},
            {"name": "第二步", "requirement": "接着做", "sources": [0]},
        ],
    }


def test_create_load_list_and_delete():
    workflow = store.create_workflow(WS, sample_payload())
    assert store.is_valid_id(workflow.id)
    loaded = store.load_workflow(WS, workflow.id)
    assert loaded is not None and loaded.name == "测试工作流"
    assert loaded.steps[1].sources == [loaded.steps[0].id]

    items = store.list_workflows(WS)
    assert any(item["workflow"]["id"] == workflow.id for item in items)

    assert store.delete_workflow(WS, workflow.id) is True
    assert store.load_workflow(WS, workflow.id) is None


def test_update_preserves_created_at():
    workflow = store.create_workflow(WS, sample_payload())
    time.sleep(0.01)
    updated = store.update_workflow(WS, workflow.id, {**sample_payload(), "name": "改名"})
    assert updated.name == "改名"
    assert updated.created_at == workflow.created_at


def test_state_roundtrip():
    workflow = store.create_workflow(WS, sample_payload())
    state = store.load_state(WS, workflow.id)
    state.status = "running"
    state.items["abc"] = StepState(status="running", session_id="s1",
                                   phase="running", current_tool="bash")
    store.save_state(WS, workflow.id, state)
    again = store.load_state(WS, workflow.id)
    assert again.status == "running"
    assert again.items["abc"].current_tool == "bash"


def test_invalid_workflow_id_is_rejected():
    assert store.load_workflow(WS, "../etc") is None
    assert store.load_workflow(WS, "no-such") is None


def test_run_events_append_and_offset():
    workflow = store.create_workflow(WS, sample_payload())
    run_id = store.new_run_id(WS, workflow.id)
    store.ensure_run_dir(WS, workflow.id, run_id)
    store.append_event(WS, workflow.id, run_id, {"type": "run_started", "run_id": run_id})
    store.append_event(WS, workflow.id, run_id, {"type": "step_started", "step_id": "x"})
    store.append_event(WS, workflow.id, run_id, {"type": "run_finished", "status": "completed"},
                       fsync=True)
    events = store.read_events(WS, workflow.id, run_id)
    assert [e["type"] for e in events] == ["run_started", "step_started", "run_finished"]
    tail = store.read_events(WS, workflow.id, run_id, offset=1)
    assert [e["type"] for e in tail] == ["step_started", "run_finished"]

    store.write_run_json(WS, workflow.id, run_id, {"run_id": run_id, "status": "completed",
                                               "started_at": "2026-11-19 10:00:00"})
    data = store.read_run_json(WS, workflow.id, run_id)
    assert data["status"] == "completed"
    runs = store.list_runs(WS, workflow.id)
    assert runs and runs[0]["run_id"] == run_id


def test_prune_keeps_latest_runs():
    workflow = store.create_workflow(WS, sample_payload())
    for i in range(3):
        run_id = store.new_run_id(WS, workflow.id)
        store.ensure_run_dir(WS, workflow.id, run_id)
        store.write_run_json(WS, workflow.id, run_id, {
            "run_id": run_id, "status": "completed",
            "started_at": f"2026-11-19 10:0{i}:00",
        })
    store.prune_runs(WS, workflow.id, keep=2)
    runs = store.list_runs(WS, workflow.id)
    assert len(runs) == 2
    assert all(r["started_at"] != "2026-11-19 10:00:00" for r in runs)
