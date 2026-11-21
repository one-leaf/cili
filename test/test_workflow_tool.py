"""workflow 工具：面板会话绑定与 create/get/update/status。"""

import json
from types import SimpleNamespace

from core.workflows import store
from core.tools.workflow_tool import WorkflowTool

WS = "test"


class FakeSession:
    def __init__(self, metadata=None, session_id="panel1"):
        self.metadata = dict(metadata or {})
        self.session_id = session_id
        self.name = "面板会话"
        self.messages = []

    def rename(self, name):
        self.name = name

    def save(self, force=False):
        pass


def make_tool(session):
    return WorkflowTool(cwd=".", workspace_uuid=WS, session=session)


def test_tool_requires_panel_metadata():
    tool = make_tool(FakeSession())
    result = tool.execute(action="get")
    assert result.is_error


def test_create_binds_session_and_get_returns_definition():
    session = FakeSession({"workflow_panel": {"workflow_id": ""}})
    tool = make_tool(session)
    result = tool.execute(
        action="create",
        name="测试工作流",
        description="目标",
        steps=[
            {"name": "一", "requirement": "做事", "sources": []},
            {"name": "二", "requirement": "接着做", "sources": [0]},
        ],
    )
    assert not result.is_error
    data = json.loads(result.output)
    workflow_id = data["workflow_id"]
    assert session.metadata["workflow_panel"]["workflow_id"] == workflow_id
    assert session.name == "[工作流面板] 测试工作流"

    get_result = tool.execute(action="get")
    payload = json.loads(get_result.output)
    assert payload["workflow"]["name"] == "测试工作流"
    assert payload["workflow"]["steps"][1]["sources"] == [payload["workflow"]["steps"][0]["id"]]
    assert store.load_workflow(WS, workflow_id) is not None


def test_create_rejected_when_already_bound():
    workflow = store.create_workflow(WS, {
        "name": "已有工作流",
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    })
    session = FakeSession({"workflow_panel": {"workflow_id": workflow.id}})
    tool = make_tool(session)
    result = tool.execute(action="create", name="x",
                          steps=[{"name": "a", "requirement": "b", "sources": []}])
    assert result.is_error


def test_update_renames_session_and_keeps_ids(monkeypatch):
    workflow = store.create_workflow(WS, {
        "name": "旧名",
        "steps": [
            {"name": "一", "requirement": "做事", "sources": []},
            {"name": "二", "requirement": "接着做", "sources": [0]},
        ],
    })
    session = FakeSession({"workflow_panel": {"workflow_id": workflow.id}})
    tool = make_tool(session)

    payload = {
        "name": "新名",
        "steps": [
            {"id": workflow.steps[0].id, "name": "一改", "requirement": "做事 2",
             "sources": []},
            {"id": workflow.steps[1].id, "name": "二", "requirement": "接着做",
             "sources": [workflow.steps[0].id]},
        ],
    }
    result = tool.execute(action="update", **payload)
    assert not result.is_error
    updated = store.load_workflow(WS, workflow.id)
    assert updated.name == "新名"
    assert [s.id for s in updated.steps] == [s.id for s in workflow.steps]
    assert session.name == "[工作流面板] 新名"


def test_status_reports_steps():
    workflow = store.create_workflow(WS, {
        "name": "状态工作流",
        "steps": [
            {"name": "一", "requirement": "做事", "sources": []},
            {"name": "二", "requirement": "接着做", "sources": [0]},
        ],
    })
    state = store.load_state(WS, workflow.id)
    from core.workflows.model import StepState
    state.items[workflow.steps[0].id] = StepState(status="done", duration_ms=1000)
    store.save_state(WS, workflow.id, state)
    tool = make_tool(FakeSession({"workflow_panel": {"workflow_id": workflow.id}}))
    payload = json.loads(tool.execute(action="status").output)
    assert payload["items"][0]["status"] == "done"
    assert payload["items"][1]["status"] == "pending"


def test_normal_chat_list_and_missing_inputs_are_structured():
    workflow = store.create_workflow(WS, {
        "name": "聊天运行",
        "inputs": [{"key": "document_no", "label": "单据号", "required": True}],
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    })
    tool = make_tool(FakeSession({}, session_id="chat1"))

    listed = json.loads(tool.execute(action="list").output)
    assert any(item["id"] == workflow.id for item in listed["workflows"])

    result = tool.execute(action="run", workflow_id=workflow.id, inputs={})
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["error"] == "missing_inputs"
    assert payload["missing"][0]["key"] == "document_no"

    create = tool.execute(action="create", name="x",
                          steps=[{"name": "a", "requirement": "b"}])
    assert create.is_error


def test_panel_create_and_update_inputs_definition():
    session = FakeSession({"workflow_panel": {"workflow_id": ""}})
    tool = make_tool(session)
    result = tool.execute(
        action="create", name="带输入", steps=[{"name": "a", "requirement": "b"}],
        inputs=[{"key": "document_no", "label": "单据号", "required": True}],
    )
    assert not result.is_error
    workflow_id = json.loads(result.output)["workflow_id"]
    workflow = store.load_workflow(WS, workflow_id)
    assert workflow.inputs[0].key == "document_no"

    updated = tool.execute(action="update", inputs=[
        {"key": "document_no", "label": "单据号", "required": False},
    ])
    assert not updated.is_error
    assert store.load_workflow(WS, workflow_id).inputs[0].required is False


def test_normal_chat_run_passes_inputs_and_origin(monkeypatch):
    from types import SimpleNamespace
    from core.workflows import service

    captured = {}

    def fake_run(workspace_uuid, workflow_id, inputs=None, origin_session_id=""):
        captured.update(workspace=workspace_uuid, workflow=workflow_id,
                        inputs=inputs, origin=origin_session_id)
        return SimpleNamespace(run_id="abcd1234", results_session_id="res1")

    monkeypatch.setattr(service, "run_workflow", fake_run)
    tool = make_tool(FakeSession({}, session_id="chat1"))
    result = tool.execute(action="run", workflow_id="wf1",
                          inputs={"document_no": "PO-1"})
    assert not result.is_error
    assert captured == {"workspace": WS, "workflow": "wf1",
                        "inputs": {"document_no": "PO-1"},
                        "origin": "chat1"}


def test_update_inputs_action_replaces_definitions():
    workflow = store.create_workflow(WS, {
        "name": "输入维护",
        "inputs": [{"key": "document_no", "label": "单据号", "required": True}],
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    })
    tool = make_tool(FakeSession({"workflow_panel": {"workflow_id": workflow.id}}))
    result = tool.execute(action="update_inputs", input_defs=[
        {"key": "document_no", "label": "单据号", "required": False},
        {"key": "date", "label": "日期", "required": True, "default": "今天"},
    ])
    assert not result.is_error
    updated = store.load_workflow(WS, workflow.id)
    assert [i.key for i in updated.inputs] == ["document_no", "date"]
    assert updated.inputs[1].required is True


def test_runs_get_run_and_delete_actions():
    workflow = store.create_workflow(WS, {
        "name": "历史与删除",
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    })
    store.ensure_run_dir(WS, workflow.id, "abcd1234")
    store.write_run_json(WS, workflow.id, "abcd1234", {"run_id": "abcd1234", "status": "completed"})
    session = FakeSession({"workflow_panel": {"workflow_id": workflow.id}})
    tool = make_tool(session)

    runs = json.loads(tool.execute(action="runs").output)
    assert runs["runs"] and runs["runs"][0]["run_id"] == "abcd1234"
    detail = json.loads(tool.execute(action="get_run", run_id="abcd1234").output)
    assert detail["run"]["status"] == "completed"

    deleted = tool.execute(action="delete")
    assert not deleted.is_error
    assert store.load_workflow(WS, workflow.id) is None
    assert session.metadata["workflow_panel"]["workflow_id"] == ""


def test_send_to_user_enqueues_step_files(monkeypatch):
    from core.workflows import service
    import core.outbox as outbox
    from types import SimpleNamespace

    monkeypatch.setattr(service, "collect_result_files",
                        lambda ws, wid, rid=None: [
                            {"path": "C:/ws/steps/1/result.md", "note": "第 1 步"},
                        ])
    captured = {}

    def fake_enqueue(session_key, entries, limit=None):
        captured.update(key=session_key, entries=entries)
        return SimpleNamespace(accepted=["C:/ws/steps/1/result.md"],
                               duplicates=[], overflow=False)

    monkeypatch.setattr(outbox, "enqueue_files", fake_enqueue)
    tool = make_tool(FakeSession({}, session_id="chat1"))
    result = tool.execute(action="send_to_user", workflow_id="wf1")
    assert not result.is_error
    assert captured["key"] == f"{WS}:chat1"
    assert captured["entries"][0][0].endswith("result.md")


def test_results_session_binds_workflow_and_blocks_create():
    """结果会话可读/运行绑定的工作流，但不能 create/update"""
    workflow = store.create_workflow(WS, {
        "name": "结果会话工作流",
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    })
    session = FakeSession({"run_results": {"workflow_id": workflow.id}}, session_id="res1")
    tool = make_tool(session)

    got = tool.execute(action="get")
    assert not got.is_error
    assert json.loads(got.output)["workflow"]["id"] == workflow.id

    created = tool.execute(action="create", name="x",
                           steps=[{"name": "a", "requirement": "b", "sources": []}])
    assert created.is_error


def test_run_step_dispatches_with_bound_workflow(monkeypatch):
    from core.workflows import service

    workflow = store.create_workflow(WS, {
        "name": "按步重跑",
        "steps": [{"name": "一", "requirement": "做事", "sources": []}],
    })
    session = FakeSession({"run_results": {"workflow_id": workflow.id}}, session_id="res1")
    tool = make_tool(session)

    calls = []
    monkeypatch.setattr(
        service, "run_workflow_step",
        lambda ws, wid, step: calls.append((ws, wid, step)) or SimpleNamespace(
            status="running", run_id="abcd1234", results_session_id="res2"))

    result = tool.execute(action="run_step", step="一")
    assert not result.is_error
    assert calls == [(WS, workflow.id, "一")]


def test_results_session_gets_workflow_tool():
    """结果会话挂 workflow 工具（运行类动作），步骤会话仍然不可用"""
    from core.tools.registry import _workflow_tool_allowed

    assert _workflow_tool_allowed(
        SimpleNamespace(metadata={"run_results": {"workflow_id": "wf1"}}))
    assert not _workflow_tool_allowed(
        SimpleNamespace(metadata={"workflow": {"workflow_id": "wf1"}}))
