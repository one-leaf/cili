"""工作流定义写入守卫：禁止工具绕过 workflow 工具直接改 .cili/workflows。"""

from __future__ import annotations

from pathlib import Path

from core.fs_utils import is_path_within


def workflow_definition_violation(cwd: str, resolved: str) -> str | None:
    """直接写工作流定义时返回拒绝文案；否则 None。"""
    root = str(Path(cwd, ".cili", "workflows").resolve())
    target = str(Path(resolved).resolve())
    if not is_path_within(target, root):
        return None
    return (
        "Error: 工作流定义不能直接写文件。请改用 workflow 工具"
        "（create / update / update_inputs / delete）；直接编辑 .cili/workflows "
        "会绕过校验与面板绑定，界面不会刷新。"
    )
