"""workflow_agent 工具测试：参数校验与执行器委托。"""

from __future__ import annotations

from core.tools.base import ToolResult
from core.tools.workflow_agent import WorkflowAgentTool


def test_requires_step():
    tool = WorkflowAgentTool(cwd=".", workspace_uuid="ws", executor=lambda s: ToolResult("ok"))
    for step in (None, ""):
        result = tool.execute(step=step)
        assert result.error and "step 不能为空" in result.output


def test_without_executor_rejected():
    tool = WorkflowAgentTool(cwd=".", workspace_uuid="ws")
    result = tool.execute(step=0)
    assert result.error and "不在工作流运行中" in result.output


def test_delegates_to_executor():
    seen = []

    def executor(step):
        seen.append(step)
        return ToolResult(f"step {step} done")

    tool = WorkflowAgentTool(cwd=".", workspace_uuid="ws", executor=executor)
    assert tool.execute(step=2).output == "step 2 done"
    assert tool.execute(step="写方案").output == "step 写方案 done"
    assert seen == [2, "写方案"]


def test_schema_name_and_step_param():
    tool = WorkflowAgentTool(cwd=".", workspace_uuid="ws")
    schema = tool.to_schema()
    assert schema["name"] == "workflow_agent"
    assert "step" in schema["input_schema"]["properties"]
