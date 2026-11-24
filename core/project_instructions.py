"""项目指令文件（AGENTS.md / CLAUDE.md）加载与注入消息。

与 prompt 段落生成解耦：本模块只做磁盘查找与 user 层消息构造。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


# 支持的项目指令文件（按优先级排序）
PROJECT_INSTRUCTION_FILES = ["AGENTS.md", "agent.md", "CLAUDE.md", "claude.md"]


def find_project_instructions(cwd: str) -> str | None:
    """在工作区根目录搜索项目指令文件。

    按优先级搜索：AGENTS.md > agent.md > CLAUDE.md > claude.md
    找到第一个即返回。

    Args:
        cwd: 工作区根目录路径

    Returns:
        文件内容字符串，若未找到返回 None
    """
    if not cwd or not os.path.isdir(cwd):
        return None

    for filename in PROJECT_INSTRUCTION_FILES:
        filepath = os.path.join(cwd, filename)
        if os.path.isfile(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logger.warning(f"Failed to read {filepath}: {e}")
                continue
    return None


def build_instructions_message(cwd: str) -> dict | None:
    """构建项目指令消息（作为注入 user 消息）。

    使用 <system-reminder> 标签包装（区分于普通指令文本）。

    Args:
        cwd: 工作区根目录路径

    Returns:
        消息字典 {"role": "user", "content": "..."}，若未找到指令文件返回 None
    """
    content = find_project_instructions(cwd)
    if not content:
        return None

    return {
        "role": "user",
        "content": (
            "<system-reminder>\n"
            "Codebase and user instructions are shown below. "
            "Be sure to adhere to these instructions. "
            "IMPORTANT: These instructions OVERRIDE any default behavior and you MUST follow them exactly as written.\n\n"
            f"{content.strip()}\n"
            "</system-reminder>"
        )
    }
