"""系统来源：cron 终态与 workflow 终态写收件箱。"""

from __future__ import annotations

import pytest

from core.cron.engine import CronEngine, _Dispatch
from core.cron.model import Job, JobState, RunResult
from core.time_utils import now_str
from core.workflows.model import Workflow, WorkflowState
from core.workflows.runner import WorkflowRunner


def _cron_notify(store, *, status="ok", managed=False):
    engine = CronEngine()
    job = Job(
        id="job-1",
        name="每日新闻",
        workspace_uuid="test-uuid",
        managed=managed,
        origin={"session_id": "sess-1"},
    )
    state = JobState(last_run=now_str())
    result = RunResult(
        job_id="job-1",
        slot="2026-11-26T10:00",
        status=status,
        final_text="抓取完成" if status == "ok" else "",
        error="boom" if status == "error" else None,
    )
    dispatch = _Dispatch(job=job, state=state, slot="2026-11-26T10:00")
    engine._notify_inbox(
        job, state, result, dispatch, "test-uuid",
        {"session_id": "sess-1", "status": "delivered"},
    )
    return store.list(status="all")[0]


def test_cron_ok_writes_item(inbox_store):
    items = _cron_notify(inbox_store)
    assert len(items) == 1
    item = items[0]
    assert item.title == "定时任务「每日新闻」执行完成"
    assert item.level == "success"
    assert item.source["type"] == "cron"
    assert item.session_id == "sess-1"
    assert item.payload["status"] == "ok"


@pytest.mark.parametrize(
    "status,level,suffix",
    [
        ("error", "error", "执行失败"),
        ("timeout", "error", "执行超时"),
        ("interrupted", "warning", "执行中断"),
    ],
)
def test_cron_status_mapping(inbox_store, status, level, suffix):
    items = _cron_notify(inbox_store, status=status)
    assert items[0].level == level
    assert items[0].title.endswith(suffix)


def test_cron_skipped_and_managed_not_written(inbox_store):
    assert _cron_notify(inbox_store, status="skipped") == []
    assert _cron_notify(inbox_store, managed=True) == []


def test_cron_dedupe(inbox_store):
    _cron_notify(inbox_store)
    _cron_notify(inbox_store)
    assert len(inbox_store.list(status="all")[0]) == 1


def test_cron_interrupted_recovery_notifies(inbox_store, cron_env):
    from core.cron import store as cron_store

    ws = cron_env["workspace_uuid"]
    engine = CronEngine()
    job = Job(
        id="job-interrupted",
        name="中断任务",
        workspace_uuid=ws,
        schedule={"type": "interval", "minutes": 10},
        content={"prompt": "do"},
        origin={"session_id": "sess-1"},
    )
    state = JobState(running={"slot": "slot-1", "started_at": now_str()})
    cron_store.save_job(job)
    cron_store.save_state(ws, job.id, state)
    try:
        engine._load_all(recover_running=True)
    finally:
        cron_store.delete_job(ws, job.id)

    items = inbox_store.list(status="all")[0]
    assert any(item.title == "定时任务「中断任务」执行中断" for item in items)
    assert any(item.level == "warning" for item in items)


def _workflow_notify(store, *, status="completed"):
    state = WorkflowState(status=status, run_id="run-1", started_at=now_str())
    runner = WorkflowRunner("test-uuid", Workflow(id="wf-1", name="周报"), state)
    runner._run_id = "run-1"
    runner._final_summary_text = "三步全部完成"
    runner._notify_inbox_terminal()
    return store.list(status="all")[0]


@pytest.mark.parametrize(
    "status,level,suffix",
    [
        ("completed", "success", "运行完成"),
        ("failed", "error", "运行失败"),
        ("stopped", "warning", "已停止"),
        ("interrupted", "warning", "已中断"),
    ],
)
def test_workflow_terminal_status(inbox_store, status, level, suffix):
    items = _workflow_notify(inbox_store, status=status)
    assert len(items) == 1
    item = items[0]
    assert item.level == level
    assert item.title == f"工作流「周报」{suffix}"
    assert item.source["type"] == "workflow"
    assert "结果目录" in item.body


def test_workflow_dedupe(inbox_store):
    _workflow_notify(inbox_store)
    _workflow_notify(inbox_store)
    assert len(inbox_store.list(status="all")[0]) == 1


def test_workflow_interrupted_recovery_notifies(inbox_store):
    from core.workflows import service, store

    workflow = store.create_workflow(
        "test",
        {
            "name": "中断测试",
            "description": "",
            "steps": [{"name": "步骤", "requirement": "做", "sources": []}],
        },
    )
    state = store.load_state("test", workflow.id)
    state.status = "running"
    state.run_id = "run-interrupted"
    state.origin_session_id = "sess-1"
    store.save_state("test", workflow.id, state)

    service._mark_interrupted("test", workflow.id, state)

    items = inbox_store.list(status="all")[0]
    assert any(item.title == "工作流「中断测试」已中断" for item in items)
    assert any(item.session_id == "sess-1" for item in items)


def test_workflow_run_finally_notifies_on_exception(inbox_store, monkeypatch):
    state = WorkflowState(status="idle", run_id="run-1", started_at=now_str())
    runner = WorkflowRunner("test-uuid", Workflow(id="wf-1", name="周报"), state)
    runner._run_id = "run-1"

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_loop", boom)
    runner._run()

    assert state.status == "failed"
    items = inbox_store.list(status="all")[0]
    assert len(items) == 1
    assert items[0].title == "工作流「周报」运行失败"
