"""工作流路由：直接调用端点函数（仓库约定，不用 TestClient）。"""

import asyncio
from pathlib import Path

import pytest

from core.config import get_workspace_data_dir
from core.workflows import service, store
from web import routes_workflows

WS = "test"


def ws_dir():
    return Path(get_workspace_data_dir(WS))


def call(coro):
    return asyncio.run(coro)


def test_crud_endpoints():
    created = call(routes_workflows.create_workflow(WS, {
        "name": "路由工作流",
        "steps": [
            {"name": "一", "requirement": "做事", "sources": []},
            {"name": "二", "requirement": "接着做", "sources": [0]},
        ],
    }, ws_dir=ws_dir()))
    workflow_id = created["workflow"]["id"]

    detail = call(routes_workflows.get_workflow(WS, workflow_id, ws_dir=ws_dir()))
    assert detail["workflow"]["name"] == "路由工作流"
    assert detail["state"]["status"] == "idle"

    updated = call(routes_workflows.update_workflow(WS, workflow_id, {
        "name": "改名",
        "steps": [
            {"name": "一", "requirement": "做事", "sources": []},
        ],
    }, ws_dir=ws_dir()))
    assert updated["workflow"]["name"] == "改名"

    listed = call(routes_workflows.list_workflows(WS, ws_dir=ws_dir()))
    assert any(item["workflow"]["id"] == workflow_id for item in listed["workflows"])

    deleted = call(routes_workflows.delete_workflow(WS, workflow_id, ws_dir=ws_dir()))
    assert deleted["ok"] is True


def test_panel_and_results_sessions():
    workflow = service.create_workflow(WS, {
        "name": "会话工作流",
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    })
    panel = call(routes_workflows.panel_session(WS, {"workflow_id": workflow.id}, ws_dir=ws_dir()))
    assert panel["session_id"]
    again = call(routes_workflows.panel_session(WS, {"workflow_id": workflow.id}, ws_dir=ws_dir()))
    assert again["session_id"] == panel["session_id"]

    binding = call(routes_workflows.panel_session_binding(
        WS, panel["session_id"], ws_dir=ws_dir()))
    assert binding["workflow_id"] == workflow.id

    results = call(routes_workflows.results_session(WS, workflow.id, ws_dir=ws_dir()))
    assert results["session_id"]
    state = service.get_state(WS, workflow.id)
    assert state.results_session_id == results["session_id"]


def test_runs_endpoints_are_empty_before_first_run():
    workflow = service.create_workflow(WS, {
        "name": "无运行",
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    })
    runs = call(routes_workflows.list_runs(WS, workflow.id, ws_dir=ws_dir()))
    assert runs["runs"] == []


def test_step_execution_fallback_slices_step_prompt():
    from core.session import SessionStore
    from web.routes_workspace import _step_session_execution
    sessions_dir = get_workspace_data_dir(WS) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    step = SessionStore.create_new_session(
        sessions_dir, "[工作流] 步骤", metadata={"hidden": True,
                                              "workflow": {"workflow_id": "x", "subworkflow_id": "y"}})
    step.add_message("user", "继承来的上游上下文")
    step.add_message("user", "# 工作流「示例」· 第 1/2 步：收集数据\n\n## 本步要求\n做事")
    step.add_message("assistant", "完成，产出 report.csv")
    step.save(force=True)

    payload = _step_session_execution(step.session_id, sessions_dir)
    assert payload is not None
    assert payload["metadata"]["step_session_id"] == step.session_id
    assert payload["messages"][0]["content"].startswith("# 工作流「")
    assert "继承来的上游上下文" not in str(payload["messages"])

    normal = SessionStore.create_new_session(sessions_dir, "普通会话")
    normal.add_message("user", "你好")
    normal.save(force=True)
    assert _step_session_execution(normal.session_id, sessions_dir) is None


def test_results_session_fresh_per_run():
    from core.workflows.sessions import ensure_results_session
    workflow = service.create_workflow(WS, {
        "name": "最新结果",
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    })
    state = store.load_state(WS, workflow.id)
    state.run_id = "aaaa1111"
    store.save_state(WS, workflow.id, state)
    first = ensure_results_session(WS, workflow, state, fresh=True)
    # 空的结果会话（还没跑过）会被下一次运行复用
    again = ensure_results_session(WS, workflow, store.load_state(WS, workflow.id), fresh=True)
    assert again.session_id == first.session_id

    first.add_message("assistant", "上一次运行的事件")
    first.save(force=True)

    state2 = store.load_state(WS, workflow.id)
    state2.run_id = "bbbb2222"
    store.save_state(WS, workflow.id, state2)
    second = ensure_results_session(WS, workflow, state2, fresh=True)

    assert first.session_id != second.session_id
    assert store.load_state(WS, workflow.id).results_session_id == second.session_id
    latest = ensure_results_session(WS, workflow, store.load_state(WS, workflow.id))
    assert latest.session_id == second.session_id


def test_run_requires_declared_inputs():
    from fastapi import HTTPException
    workflow = service.create_workflow(WS, {
        "name": "需要单据号",
        "inputs": [{"key": "document_no", "label": "单据号", "required": True}],
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    })
    with pytest.raises(HTTPException) as exc:
        call(routes_workflows.run_workflow(
            WS, workflow.id, {"inputs": {}}, ws_dir=ws_dir()))
    assert exc.value.status_code == 400
    assert "单据号" in str(exc.value.detail)


def test_service_run_validates_and_passes_inputs(monkeypatch):
    from types import SimpleNamespace

    from core.workflows import runner as runner_mod

    workflow = service.create_workflow(WS, {
        "name": "运行参数",
        "inputs": [{"key": "document_no", "label": "单据号", "required": True}],
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    })
    captured = {}

    def fake_start(workspace_uuid, wf, state, inputs=None, origin_session_id=""):
        captured.update(inputs=inputs, origin=origin_session_id)
        return SimpleNamespace()

    monkeypatch.setattr(runner_mod, "is_workflow_running", lambda *a, **k: False)
    monkeypatch.setattr(runner_mod, "start_workflow_runner", fake_start)
    state = service.run_workflow(WS, workflow.id,
                                 inputs={"document_no": "PO-9"},
                                 origin_session_id="chat1")
    assert captured == {"inputs": {"document_no": "PO-9"}, "origin": "chat1"}
    assert state.origin_session_id == "chat1"


def test_hidden_flag_roundtrip_and_update_preserves():
    created = call(routes_workflows.create_workflow(WS, {
        "name": "隐藏工作流",
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    }, ws_dir=ws_dir()))
    workflow_id = created["workflow"]["id"]
    assert created["workflow"]["hidden"] is False

    hidden = call(routes_workflows.set_workflow_hidden(
        WS, workflow_id, {"hidden": True}, ws_dir=ws_dir()))
    assert hidden == {"ok": True, "hidden": True}

    listed = call(routes_workflows.list_workflows(WS, ws_dir=ws_dir()))
    item = next(i for i in listed["workflows"] if i["workflow"]["id"] == workflow_id)
    assert item["workflow"]["hidden"] is True

    # 面板编辑（只提交定义字段，不带 hidden）不应重置隐藏状态
    call(routes_workflows.update_workflow(WS, workflow_id, {
        "name": "隐藏工作流改",
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    }, ws_dir=ws_dir()))
    detail = call(routes_workflows.get_workflow(WS, workflow_id, ws_dir=ws_dir()))
    assert detail["workflow"]["hidden"] is True
    assert detail["workflow"]["name"] == "隐藏工作流改"

    # 取消隐藏
    call(routes_workflows.set_workflow_hidden(
        WS, workflow_id, {"hidden": False}, ws_dir=ws_dir()))
    detail = call(routes_workflows.get_workflow(WS, workflow_id, ws_dir=ws_dir()))
    assert detail["workflow"]["hidden"] is False
