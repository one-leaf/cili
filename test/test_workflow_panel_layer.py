"""workflow_panel 动态 user layer：只对维护会话渲染。"""

from types import SimpleNamespace

from core.prompt_builder import _gen_workflow_panel
from core.workflows import store

WS = "test"


def make_runner(session):
    session.metadata = getattr(session, "metadata", {})
    return SimpleNamespace(session=session, workspace_uuid=WS)


def test_unbound_panel_prompts_create():
    session = SimpleNamespace(metadata={"workflow_panel": {"workflow_id": ""}}, session_id="s")
    message = _gen_workflow_panel(make_runner(session))
    assert message is not None
    assert "workflow.create" in message["content"]


def test_bound_panel_renders_workflow():
    workflow = store.create_workflow(WS, {
        "name": "季度财报分析",
        "description": "总目标",
        "steps": [
            {"name": "收集数据", "requirement": "读 data/raw", "sources": []},
            {"name": "清洗数据", "requirement": "输出 clean.csv", "sources": [0]},
        ],
    })
    session = SimpleNamespace(metadata={"workflow_panel": {"workflow_id": workflow.id}},
                              session_id="s")
    message = _gen_workflow_panel(make_runner(session))
    content = message["content"]
    assert "季度财报分析" in content
    assert "1. 收集数据" in content
    assert "来源：1" in content  # 第二个步骤来源显示序号
    assert "运行状态：idle" in content


def test_other_sessions_get_no_layer():
    session = SimpleNamespace(metadata={}, session_id="s")
    assert _gen_workflow_panel(make_runner(session)) is None
