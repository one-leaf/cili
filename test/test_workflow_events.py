"""工作流/会话/文件变更事件：只在真实写盘成功后广播。

回归背景：前端原先靠「回合结束」推断工作流是否创建成功，面板会话/结果会话
由服务端直接创建也不广播，导致侧栏、文件树不刷新。事件必须以服务层实际
变更结果为准。
"""

import queue
from types import SimpleNamespace

from core.default_sink import bind_default_sink
from core.event_bus import get_event_bus
from core.workflows import service, store
from core.workflows.sessions import ensure_results_session

WS = "test-workflow-events"


def _payload():
    return {
        "name": "事件工作流",
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    }


def _subscribe(workspace_uuid):
    q = queue.Queue()
    get_event_bus().subscribe(q, workspace_uuid)
    return q


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _has(events, event_type, **fields):
    return any(e.get("type") == event_type
               and all(e.get(k) == v for k, v in fields.items())
               for e in events)


def test_crud_publishes_workflow_changed():
    q = _subscribe(WS)
    try:
        wf = service.create_workflow(WS, _payload())
        assert _has(_drain(q), "workflow_changed",
                    workflow_id=wf.id, action="created")

        service.update_workflow(WS, wf.id, {**_payload(), "name": "改名"})
        assert _has(_drain(q), "workflow_changed",
                    workflow_id=wf.id, action="updated")

        service.set_hidden(WS, wf.id, True)
        assert _has(_drain(q), "workflow_changed",
                    workflow_id=wf.id, action="updated")

        service.delete_workflow(WS, wf.id)
        assert _has(_drain(q), "workflow_changed",
                    workflow_id=wf.id, action="deleted")
    finally:
        get_event_bus().unsubscribe(q)


def test_run_and_stop_publish_workflow_changed(monkeypatch):
    q = _subscribe(WS)
    try:
        wf = service.create_workflow(WS, _payload())
        _drain(q)

        monkeypatch.setattr(service.runner_mod, "is_workflow_running",
                            lambda *a, **k: False)
        monkeypatch.setattr(service.runner_mod, "start_workflow_runner",
                            lambda *a, **k: object())
        service.run_workflow(WS, wf.id)
        assert _has(_drain(q), "workflow_changed",
                    workflow_id=wf.id, action="run_started")

        monkeypatch.setattr(service.runner_mod, "stop_workflow_runner",
                            lambda *a, **k: True)
        assert service.stop_workflow(WS, wf.id) is True
        assert _has(_drain(q), "workflow_changed",
                    workflow_id=wf.id, action="stopped")
    finally:
        get_event_bus().unsubscribe(q)


def test_results_session_publishes_session_changed():
    wf = service.create_workflow(WS, _payload())
    q = _subscribe(WS)
    try:
        session = ensure_results_session(WS, wf, store.load_state(WS, wf.id))
        assert _has(_drain(q), "session_changed", session_id=session.session_id)
    finally:
        get_event_bus().unsubscribe(q)


def test_file_tool_publishes_files_changed():
    runner = SimpleNamespace()
    bind_default_sink(runner, WS, "sess-files")
    q = _subscribe(WS)
    try:
        runner.default_sink.on_tool_result("write", "ok", False, "t1")
        assert _has(_drain(q), "files_changed", session_id="sess-files")

        runner.default_sink.on_tool_result("read", "ok", False, "t2")
        assert not _has(_drain(q), "files_changed")
    finally:
        get_event_bus().unsubscribe(q)


def test_run_step_requires_previous_run():
    import pytest

    from core.workflows.model import WorkflowValidationError

    wf = service.create_workflow(WS, _payload())
    with pytest.raises(WorkflowValidationError):
        service.run_workflow_step(WS, wf.id, 0)


def test_run_step_publishes_and_resolves_by_name(monkeypatch):
    wf = service.create_workflow(WS, _payload())
    state = store.load_state(WS, wf.id)
    state.run_id = "abcd1234"
    store.save_state(WS, wf.id, state)

    monkeypatch.setattr(service.runner_mod, "is_workflow_running",
                        lambda *a, **k: False)
    started = []
    monkeypatch.setattr(service.runner_mod, "start_workflow_step_runner",
                        lambda ws, workflow, st, index: started.append(index) or object())

    q = _subscribe(WS)
    try:
        service.run_workflow_step(WS, wf.id, "一")
        assert started == [0]
        assert _has(_drain(q), "workflow_changed",
                    workflow_id=wf.id, action="step_started")
    finally:
        get_event_bus().unsubscribe(q)


def test_resolve_step_index_rejects_unknown():
    import pytest

    from core.workflows.model import WorkflowValidationError

    wf = service.create_workflow(WS, _payload())
    assert service.resolve_step_index(wf, 0) == 0
    assert service.resolve_step_index(wf, wf.steps[0].name) == 0
    with pytest.raises(WorkflowValidationError):
        service.resolve_step_index(wf, 5)
    with pytest.raises(WorkflowValidationError):
        service.resolve_step_index(wf, "不存在")
