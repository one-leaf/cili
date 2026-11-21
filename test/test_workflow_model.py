"""工作流模型：校验、id 生成与来源解析。"""

import re

import pytest

from core.workflows.model import (
    MAX_REQ,
    MAX_STEPS,
    Workflow,
    WorkflowState,
    WorkflowValidationError,
    is_short_id,
    new_short_id,
)


def payload(**overrides):
    data = {
        "name": "季度财报分析",
        "description": "读取原始财报，清洗后产出报告。",
        "steps": [
            {"name": "收集财报数据", "requirement": "读取 data/raw，输出 report.csv", "sources": []},
            {"name": "清洗数据", "requirement": "输出 clean.csv", "sources": [0]},
            {"name": "撰写报告", "requirement": "输出 report.md", "sources": [0, 1]},
        ],
    }
    data.update(overrides)
    return data


def test_valid_workflow_and_positions_mapped_to_ids():
    workflow = Workflow.from_dict(payload())
    assert re.match(r"^[0-9a-f]{8}$", workflow.id)
    ids = [step.id for step in workflow.steps]
    assert all(is_short_id(i) for i in ids)
    assert len(set(ids)) == 3
    assert workflow.steps[1].sources == [ids[0]]
    assert workflow.steps[2].sources == [ids[0], ids[1]]


def test_explicit_ids_are_preserved():
    data = payload()
    data["steps"][0]["id"] = "1a2b3c4d"
    data["steps"][1]["id"] = "5e6f7a8b"
    data["steps"][2]["id"] = "9c0d1e2f"
    data["steps"][1]["sources"] = ["1a2b3c4d"]
    data["steps"][2]["sources"] = ["1a2b3c4d", "5e6f7a8b"]
    workflow = Workflow.from_dict(data)
    assert [s.id for s in workflow.steps] == ["1a2b3c4d", "5e6f7a8b", "9c0d1e2f"]


@pytest.mark.parametrize("sources", [[1], [2], [0, 0], [9], ["deadbeef"], [True]])
def test_invalid_sources_rejected(sources):
    data = payload()
    data["steps"][1]["sources"] = sources
    with pytest.raises(WorkflowValidationError):
        Workflow.from_dict(data)


def test_duplicate_ids_rejected():
    data = payload()
    data["steps"][0]["id"] = "1a2b3c4d"
    data["steps"][1]["id"] = "1a2b3c4d"
    with pytest.raises(WorkflowValidationError):
        Workflow.from_dict(data)


def test_length_limits():
    with pytest.raises(WorkflowValidationError):
        Workflow.from_dict(payload(name=""))
    with pytest.raises(WorkflowValidationError):
        Workflow.from_dict(payload(name="x" * 51))
    data = payload()
    data["steps"][0]["requirement"] = "x" * (MAX_REQ + 1)
    with pytest.raises(WorkflowValidationError):
        Workflow.from_dict(data)


def test_step_count_limits():
    with pytest.raises(WorkflowValidationError):
        Workflow.from_dict(payload(steps=[]))
    many = [
        {"name": f"步骤{i}", "requirement": "做点事", "sources": []}
        for i in range(MAX_STEPS + 1)
    ]
    with pytest.raises(WorkflowValidationError):
        Workflow.from_dict(payload(steps=many))


def test_new_short_id_format_matches_workspace():
    assert re.match(r"^[0-9a-f]{8}$", new_short_id())


def test_state_roundtrip_keeps_unknown_status_as_idle():
    state = WorkflowState.from_dict({"status": "weird", "items": {"1a2b3c4d": {"status": "done"}}})
    assert state.status == "idle"
    assert state.items["1a2b3c4d"].status == "done"
    data = state.to_dict()
    again = WorkflowState.from_dict(data)
    assert again.items["1a2b3c4d"].status == "done"


def test_state_history_capped():
    state = WorkflowState()
    for i in range(25):
        state.push_history({"run_id": str(i)})
    assert len(state.history) == 20
    assert state.history[0]["run_id"] == "24"


def test_step_executor_and_io_contract_roundtrip():
    workflow = Workflow.from_dict(payload(steps=[{
        "name": "轻量步骤", "requirement": "读文件", "executor": "lite",
        "reads": ["a.md"], "outputs": ["b.md"],
    }]))
    step = workflow.steps[0]
    assert step.executor == "lite"
    assert step.reads == ["a.md"] and step.outputs == ["b.md"]
    again = Workflow.from_dict(workflow.to_dict())
    assert again.steps[0].executor == "lite"
    assert again.steps[0].outputs == ["b.md"]


def test_step_executor_defaults_to_worker():
    workflow = Workflow.from_dict(payload(steps=[
        {"name": "x", "requirement": "y"},
    ]))
    assert workflow.steps[0].executor == "worker"
    assert workflow.steps[0].reads == []


def test_invalid_executor_rejected():
    with pytest.raises(WorkflowValidationError):
        Workflow.from_dict(payload(steps=[
            {"name": "x", "requirement": "y", "executor": "master"},
        ]))


def test_step_state_keeps_executor_and_artifact():
    state = WorkflowState.from_dict({
        "items": {"1a2b3c4d": {
            "status": "done", "executor": "lite", "artifact": "steps/1/result.md",
        }}
    })
    item = state.items["1a2b3c4d"]
    assert item.executor == "lite"
    assert item.artifact == "steps/1/result.md"
    assert WorkflowState.from_dict(state.to_dict()).items["1a2b3c4d"].artifact == "steps/1/result.md"


def test_inputs_definition_and_run_validation():
    from core.workflows.model import WorkflowInputRequiredError, validate_run_inputs
    workflow = Workflow.from_dict(payload(
        inputs=[
            {"key": "document_no", "label": "单据号", "required": True,
             "description": "例如 PO-1"},
            {"key": "remark", "label": "备注", "type": "textarea"},
        ],
        steps=[{"name": "x", "requirement": "y"}],
    ))
    assert [i.key for i in workflow.inputs] == ["document_no", "remark"]
    assert workflow.to_dict()["inputs"][0]["required"] is True
    with pytest.raises(WorkflowInputRequiredError) as exc:
        validate_run_inputs(workflow, {"remark": "hi"})
    assert exc.value.missing[0]["key"] == "document_no"
    assert validate_run_inputs(workflow, {"document_no": " PO-1 ", "x": "ignored"}) == {
        "document_no": "PO-1"}


def test_invalid_inputs_rejected():
    with pytest.raises(WorkflowValidationError):
        Workflow.from_dict(payload(inputs=[{"key": "1bad", "label": "x"}],
                                   steps=[{"name": "x", "requirement": "y"}]))
    with pytest.raises(WorkflowValidationError):
        Workflow.from_dict(payload(inputs=[{"key": "a", "label": "x", "type": "select"}],
                                   steps=[{"name": "x", "requirement": "y"}]))
    with pytest.raises(WorkflowValidationError):
        Workflow.from_dict(payload(
            inputs=[{"key": "a", "label": "A"}, {"key": "a", "label": "B"}],
            steps=[{"name": "x", "requirement": "y"}]))
