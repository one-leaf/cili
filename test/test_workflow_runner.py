"""工作流编排器：业务无关监督者 + worker/lite 执行器 + 产物契约。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import core.workflows.runner as runner_mod
from core.workflows import store
from core.workflows.model import StepState, Workflow, WorkflowState
from core.workflows.runner import WorkflowRunner
from core.output_sink import OutputSink
from core.tools.base import ToolResult

WS = "test"


def make_workflow() -> Workflow:
    return Workflow.from_dict({
        "name": "测试工作流",
        "description": "总体目标",
        "steps": [
            {"name": "第一步", "requirement": "做事", "sources": [],
             "executor": "worker"},
            {"name": "第二步", "requirement": "接着做", "sources": [0],
             "executor": "lite"},
        ],
    })


class FakeSession:
    def __init__(self, session_id="step-session"):
        self.session_id = session_id
        self.messages = []


class FakeRunner:
    result_path: Path | None = None
    roles: list[str] = []

    def __init__(self, config=None, role="worker", initial_session=None, **kwargs):
        self.role = role
        FakeRunner.roles.append(role)
        self.session = initial_session or FakeSession()
        self.current_session_id = self.session.session_id
        self._stopped = False
        self._turn_iterations = 3
        self._tool_call_count = 5
        self.permission_store = None
        self.permission_rules = None
        self.max_iterations = 200
        self._event_publisher = None
        self.default_sink = OutputSink()

    def run(self, user_input=None, sink=None):
        self.session.messages.append({"role": "user", "content": user_input})
        text = "本步完成\n状态: 完成"
        self.session.messages.append({"role": "assistant", "content": text})
        if FakeRunner.result_path is not None:
            FakeRunner.result_path.parent.mkdir(parents=True, exist_ok=True)
            FakeRunner.result_path.write_text("结果\n", encoding="utf-8")
        return None

    def stop(self):
        self._stopped = True

    def close(self):
        pass


class FakeRegistry:
    def remove(self, *args, **kwargs):
        pass

    def register_hosted(self, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def patch_runner(monkeypatch):
    FakeRunner.roles = []
    monkeypatch.setattr(runner_mod, "SessionRunner", FakeRunner)
    monkeypatch.setattr(runner_mod, "registry", FakeRegistry())
    monkeypatch.setattr(runner_mod, "classify_pending", lambda *a, **k: None)
    monkeypatch.setattr("core.default_sink.bind_default_sink", lambda *a, **k: None)


def test_resolve_step_by_index_name_and_range():
    runner = WorkflowRunner(WS, make_workflow(), WorkflowState())
    assert runner._resolve_step(1) == 1
    assert runner._resolve_step("第二步") == 1
    assert runner._resolve_step("0") == 0
    with pytest.raises(ValueError):
        runner._resolve_step(9)


def test_conductor_prompt_is_business_agnostic():
    workflow = make_workflow()
    runner = WorkflowRunner(WS, workflow, WorkflowState())
    prompt = runner._build_conductor_prompt(1, workflow.steps[1])
    assert "第二步" in prompt
    assert "lite" in prompt
    assert "workflow_agent" in prompt
    assert "只传步骤标识" in prompt
    assert "接着做" not in prompt          # 不传业务要求
    assert "## 依赖" not in prompt


def test_step_prompt_uses_default_reads_and_outputs():
    workflow = make_workflow()
    runner = WorkflowRunner(WS, workflow, WorkflowState())
    runner._run_id = "abcd1234"
    prompt = runner._build_step_prompt(1, workflow.steps[1])
    assert "接着做" in prompt
    assert "steps/1/result.md" in prompt   # 默认读取上游产物
    assert "steps/2/result.md" in prompt   # 默认输出
    assert "workflows/" in prompt and ".cili/workflows" not in prompt
    assert "状态: 完成" in prompt


def test_step_prompt_prefers_declared_paths():
    workflow = Workflow.from_dict({
        "name": "x",
        "steps": [{
            "name": "a", "requirement": "b", "executor": "worker",
            "reads": ["data/in.json"], "outputs": ["out/result.md"],
        }],
    })
    runner = WorkflowRunner(WS, workflow, WorkflowState())
    runner._run_id = "abcd1234"
    prompt = runner._build_step_prompt(0, workflow.steps[0])
    assert "data/in.json" in prompt and "out/result.md" in prompt


def test_tool_execute_runs_worker_and_returns_short_receipt(tmp_path):
    workflow = make_workflow()
    runner = WorkflowRunner(WS, workflow, WorkflowState())
    runner._run_id = "abcd1234"
    runner._expected_index = 0
    FakeRunner.result_path = tmp_path / "steps" / "1" / "result.md"
    # 让默认产物路径落在 tmp_path 下
    runner._default_result_rel = lambda index: str(FakeRunner.result_path)

    result = runner._tool_execute(0)

    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert "第 1 步「第一步」完成" in result.output
    assert "本步完成" not in result.output     # 不把业务正文回传给监督者
    assert FakeRunner.roles == ["worker"]
    state_step = runner.state.items[workflow.steps[0].id]
    assert state_step.status == "done"
    assert state_step.executor == "worker"
    assert state_step.artifact == str(FakeRunner.result_path)
    assert state_step.stats["tool_call_count"] == 5

    again = runner._tool_execute(0)
    assert again.is_error is True and "已执行" in again.output


def test_tool_execute_uses_lite_executor(tmp_path):
    workflow = make_workflow()
    runner = WorkflowRunner(WS, workflow, WorkflowState())
    runner._run_id = "abcd1234"
    runner._expected_index = 1
    FakeRunner.result_path = tmp_path / "steps" / "2" / "result.md"
    runner._default_result_rel = lambda index: str(FakeRunner.result_path)

    result = runner._tool_execute(1)
    assert result.is_error is False
    assert FakeRunner.roles == ["lite"]


def test_tool_execute_rejects_out_of_order():
    runner = WorkflowRunner(WS, make_workflow(), WorkflowState())
    runner._expected_index = 1
    result = runner._tool_execute(0)
    assert result.is_error is True and "应先执行第 2 步" in result.output


def test_missing_declared_output_fails(tmp_path):
    workflow = Workflow.from_dict({
        "name": "x",
        "steps": [{
            "name": "a", "requirement": "b", "executor": "worker",
            "outputs": ["must/exist.md"],
        }],
    })
    runner = WorkflowRunner(WS, workflow, WorkflowState())
    runner._run_id = "abcd1234"
    runner._expected_index = 0
    FakeRunner.result_path = tmp_path / "steps" / "1" / "result.md"
    runner._default_result_rel = lambda index: str(FakeRunner.result_path)
    runner._build_step_prompt = lambda index, step: "x"

    result = runner._tool_execute(0)
    assert result.is_error is True and "缺少输出产物" in result.output


def test_timeout_is_failed():
    runner = WorkflowRunner(WS, make_workflow(), WorkflowState())
    runner._step_timed_out = True
    assert runner._assess_step(None, "", make_workflow().steps[0], 0)[0] == "failed"


class FakeResultsStore:
    def __init__(self, session_id="results"):
        self.session_id = session_id
        self.messages = []

    def add_message(self, role, content, meta=None):
        self.messages.append({"role": role, "content": content, "_meta": meta})

    def save(self, force=False):
        pass


class FakeConductor:
    def __init__(self, runner):
        self.runner = runner
        self.session = FakeResultsStore()
        self.runs = 0

    def run(self, user_input=None, sink=None):
        self.runs += 1
        idx = self.runner._expected_index
        if idx >= 0:
            self.runner._tool_execute(idx)
        self.session.messages.append({"role": "assistant", "content": "汇总"})


def test_loop_runs_steps_in_order_and_summarizes(monkeypatch, tmp_path):
    workflow = make_workflow()
    workflow = workflow.__class__.from_dict(workflow.to_dict())
    state = WorkflowState()
    FakeRunner.result_path = tmp_path / "step-result.md"

    monkeypatch.setattr(runner_mod, "ensure_results_session",
                        lambda ws, wf, st, fresh=False: FakeResultsStore())
    conductor = FakeConductor(None)

    def fake_make_conductor(self, results):
        conductor.runner = self
        self._conductor = conductor
        return conductor

    monkeypatch.setattr(WorkflowRunner, "_make_conductor", fake_make_conductor)
    monkeypatch.setattr(WorkflowRunner, "_default_result_rel",
                        lambda self, index: str(tmp_path / f"step-{index + 1}.md"))
    monkeypatch.setattr(WorkflowRunner, "_notify_maintenance", lambda self, text: None)
    monkeypatch.setattr(WorkflowRunner, "_missing_outputs", lambda self, step, index: "")
    monkeypatch.setattr(WorkflowRunner, "_write_visible_summary", lambda self: None)

    runner = WorkflowRunner(WS, workflow, state)
    runner._run()

    assert state.status == "completed"
    assert [s.status for s in state.items.values()] == ["done", "done"]
    assert conductor.runs == 3          # 两步 + 最终总结
    assert FakeRunner.roles == ["worker", "lite"]
    run_json = store.read_run_json(WS, workflow.id, state.run_id)
    assert run_json["status"] == "completed"
    assert run_json["items"][workflow.steps[0].id]["executor"] == "worker"
    events = store.read_events(WS, workflow.id, state.run_id)
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "run_finished"


def test_loop_fails_when_supervisor_does_not_call_tool(monkeypatch):
    workflow = make_workflow()
    state = WorkflowState()

    monkeypatch.setattr(runner_mod, "ensure_results_session",
                        lambda ws, wf, st, fresh=False: FakeResultsStore())

    class NoCallConductor:
        def __init__(self):
            self.session = FakeResultsStore()

        def run(self, user_input=None, sink=None):
            pass

    monkeypatch.setattr(WorkflowRunner, "_make_conductor",
                        lambda self, results: NoCallConductor())
    monkeypatch.setattr(WorkflowRunner, "_notify_maintenance", lambda self, text: None)
    monkeypatch.setattr(WorkflowRunner, "_write_visible_summary", lambda self: None)

    runner = WorkflowRunner(WS, workflow, state)
    runner._run()

    assert state.status == "failed"
    step_state = state.items[workflow.steps[0].id]
    assert "未调用 workflow_agent" in step_state.error
    notice = runner._conductor.session.messages[-1]["content"]
    assert "工作流终止" in notice and "第一步" in notice


def test_results_live_under_visible_workflows_dir(monkeypatch, tmp_path):
    workflow = make_workflow()
    state = WorkflowState()
    state.status = "completed"
    state.run_id = "abcd1234"
    state.started_at = "2026-11-20 10:00:00"
    state.finished_at = "2026-11-20 10:01:00"
    runner = WorkflowRunner(WS, workflow, state)
    runner._run_id = "abcd1234"
    monkeypatch.setattr(runner_mod, "workspace_cwd", lambda ws: str(tmp_path))
    root = runner._results_root()
    assert root.parent == tmp_path / "workflows"
    assert root.name.endswith(f"-{workflow.id}")
    rel = runner._default_result_rel(0)
    assert rel == f"workflows/{root.name}/20261120_100000_abcd1234/steps/1/result.md"
    runner._write_visible_summary()
    summary = (root / "20261120_100000_abcd1234" / "summary.md").read_text(encoding="utf-8")
    assert "# 测试工作流" in summary and "## 步骤产物" in summary


def test_step_prompt_injects_run_inputs(monkeypatch, tmp_path):
    workflow = Workflow.from_dict({
        "name": "带参数",
        "inputs": [{"key": "document_no", "label": "单据号", "required": True}],
        "steps": [{"name": "a", "requirement": "处理单据", "sources": []}],
    })
    runner = WorkflowRunner(WS, workflow, WorkflowState(),
                            run_inputs={"document_no": "PO-2026-1"})
    runner._run_id = "abcd1234"
    monkeypatch.setattr(runner_mod, "workspace_cwd", lambda ws: str(tmp_path))
    prompt = runner._build_step_prompt(0, workflow.steps[0])
    assert "运行参数（用户提供）" in prompt
    assert "单据号：PO-2026-1" in prompt
    runner._write_run_inputs_file()
    text = (tmp_path / "workflows" / f"带参数-{workflow.id}" / "abcd1234" / "inputs.md").read_text(
        encoding="utf-8")
    assert "PO-2026-1" in text


def test_run_json_contains_inputs_and_origin(monkeypatch, tmp_path):
    workflow = Workflow.from_dict({
        "name": "参数",
        "inputs": [{"key": "n", "label": "编号"}],
        "steps": [{"name": "a", "requirement": "b"}],
    })
    state = WorkflowState(run_inputs={"n": "x"}, origin_session_id="chat1")
    runner = WorkflowRunner(WS, workflow, state)
    runner._run_id = "abcd1234"
    monkeypatch.setattr(runner_mod.store, "write_run_json",
                        lambda ws, wid, rid, data: setattr(runner, "_saved", data))
    runner._write_run_json(initial=True)
    assert runner._saved["run_inputs"] == {"n": "x"}
    assert runner._saved["origin_session_id"] == "chat1"


def test_terminal_notification_prefers_origin_session(monkeypatch):
    workflow = make_workflow()
    state = WorkflowState(origin_session_id="chat1", panel_session_id="panel1")
    runner = WorkflowRunner(WS, workflow, state)
    seen = {}

    class FakeStore:
        def add_message(self, role, content, meta=None):
            seen["role"] = role
            seen["content"] = content

        def save(self, force=False):
            pass

    def fake_load(session_id, sessions_dir):
        seen["session_id"] = session_id
        return FakeStore()

    monkeypatch.setattr("core.session.SessionStore.load_session", staticmethod(fake_load))
    runner._notify_maintenance("done")
    assert seen["session_id"] == "chat1"
    assert "done" in seen["content"]


def test_step_publishes_card_events_and_result_meta(monkeypatch, tmp_path):
    workflow = make_workflow()
    runner = WorkflowRunner(WS, workflow, WorkflowState())
    runner._run_id = "abcd1234"
    runner._expected_index = 0
    FakeRunner.result_path = tmp_path / "steps" / "1" / "result.md"
    runner._default_result_rel = lambda index: str(FakeRunner.result_path)
    events = []
    monkeypatch.setattr(runner, "_publish_parent",
                        lambda ev, sid, **kw: events.append((ev, sid, kw)))

    result = runner._tool_execute(0)
    assert not result.is_error
    names = [e[0] for e in events]
    assert "session_start" in names
    assert "session_complete" in names
    state_step = runner.state.items[workflow.steps[0].id]
    assert result.meta["exec_id"] == state_step.session_id
    assert result.meta["status"] == "completed"
