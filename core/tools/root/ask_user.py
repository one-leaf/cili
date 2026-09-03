"""AskUserQuestion tool — Agent 主动向用户提问，收集决策信息。

设计：工具返回 completed=False 的 ToolResult，Agent 循环退出。
前端检测到 ask_user 后渲染选择面板，用户提交后作为新 user message 继续循环。
"""

from __future__ import annotations

from typing import Any

from core.tools.shared.base import Tool, ToolResult


class AskUserTool(Tool):
    """主动向用户提问，支持多选和自由输入。"""

    name = "ask_user"
    description = (
        "Ask the user one or more multiple-choice questions to gather information, "
        "clarify ambiguity, understand preferences, or make decisions. "
        "Use this tool when you need user input before proceeding. "
        "Users can always choose 'Other' to provide custom text input."
    )
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "description": "Questions to ask the user (1-4 questions).",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The complete question text, ending with a question mark.",
                        },
                        "header": {
                            "type": "string",
                            "description": "Short label (max 12 chars) shown as a tag, e.g. 'Auth method', 'Library'.",
                        },
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 4,
                            "description": "Available choices (2-4). No 'Other' needed — added automatically.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Display text for the option (1-5 words).",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "Explanation of what this option means or its tradeoffs.",
                                    },
                                },
                                "required": ["label", "description"],
                            },
                        },
                        "multi_select": {
                            "type": "boolean",
                            "description": "Set to true to allow selecting multiple options. Default: false.",
                        },
                    },
                    "required": ["question", "header", "options"],
                },
            }
        },
        "required": ["questions"],
    }

    def execute(self, **kwargs: Any) -> ToolResult:
        questions = kwargs.get("questions", [])
        if not questions:
            return ToolResult("Error: at least one question is required", error=True)

        # Return with completed=False to exit agent loop
        # Frontend will render question card, user submits as new message
        return ToolResult(
            output="Waiting for user input...",
            completed=False,
        )
