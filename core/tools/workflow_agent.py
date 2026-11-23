"""workflow_agent tool - 派发工作流中的一个步骤。

只在结果会话（工作流监督者会话）里动态注入，绑定到当前 WorkflowRunner。
监督者只传步骤序号；工具从步骤定义解析执行器（worker/lite）、输入产物
路径和输出契约。执行器本身没有该工具，因此委派最多一层。
"""

from __future__ import annotations

from typing import Any, Callable

from core.tools.base import Tool, ToolResult


class WorkflowAgentTool(Tool):
    name = "workflow_agent"
    description = (
        "Dispatch one step of the current workflow. This tool only exists in the "
        "workflow supervisor conversation (one delegation level; step executors "
        "cannot call it).\n\n"
        "Params:\n"
        "- step: 0-based step index or the step name (required).\n\n"
        "Only pass the step identifier. Do not pass requirement text, dependency "
        "results, or file contents: the tool resolves the step definition, "
        "executor (worker/lite), input artifact paths, and output contract. "
        "Call exactly one step per user instruction, in the requested order."
    )
    concurrency_safe = False

    def __init__(self, *args, executor: Callable[[Any], ToolResult] | None = None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.executor = executor

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "step": {
                    "type": ["integer", "string"],
                    "description": "0-based step index or step name.",
                },
            },
            "required": ["step"],
        }

    def execute(self, step: Any = None, **kwargs) -> ToolResult:
        if self.executor is None:
            return ToolResult("Error: 当前会话不在工作流运行中，无法执行步骤", error=True)
        if step is None or step == "":
            return ToolResult("Error: step 不能为空", error=True)
        return self.executor(step)
