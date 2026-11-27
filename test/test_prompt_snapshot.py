"""四角色 system prompt 快照与预算测试。

不启动真实会话：用角色白名单生成同名假工具，并预置环境段缓存，
避免测试依赖默认工作区的记忆/文件系统内容。
"""

from datetime import datetime
from types import SimpleNamespace

from core.prompt_builder import build_system_prompt
from core.role_config import load_role


def _runner(role: str):
    cfg = load_role(role)
    tools = [
        SimpleNamespace(name=name, description=f"{name} tool.")
        for name in cfg.tools
    ]
    deferred = [t for t in tools if t.name in cfg.deferred_tools]
    active = [t for t in tools if t.name not in cfg.deferred_tools]
    runner = SimpleNamespace(
        role=role,
        role_cfg=cfg,
        tools=tools,
        _active_tools=active,
        _deferred_tools=deferred,
        _turn_interface=None,
        workspace_uuid="snapshot-test",
        cwd="/tmp",
        session=None,
    )
    # 固定环境段，避免读取默认工作区的真实记忆
    runner._prompt_section_cache = {
        "env_context": (
            "## Workspace\n\nWorkspace ID: `snapshot-test`\n\nCWD: `/tmp`\n\n"
            "## Current Time\n\nFor the current date/time, call the `clock` tool (action='now')."
        )
    }
    return runner


def _prompt(role: str) -> str:
    return "\n\n".join(build_system_prompt(_runner(role)))


def test_current_time_not_injected():
    """环境段不再注入日期，只提示调用 clock。"""
    prompt = _prompt("master")
    assert "`clock` tool" in prompt
    assert f"**{datetime.now().strftime('%Y-%m-%d')}**" not in prompt


def test_master_prompt_shape():
    """master 保留关键行为，重复段落已移除。"""
    prompt = _prompt("master")
    assert "## Working Style" in prompt
    assert "## Autonomy and Approval" in prompt
    assert "**Delegation vs branch sessions**" in prompt
    assert "分支会话" in prompt
    assert "## Tool Rules" not in prompt  # 假工具没有 guidelines
    assert "## Asking the User" not in prompt
    assert "## Current Time" in prompt


def test_worker_and_lite_prompt_shape():
    """worker/lite 保持执行角色规则，不重复 master 的产品段落。"""
    worker = _prompt("worker")
    assert "new epoch automatically" in worker
    assert "## Budget Awareness" in worker
    assert "## Tool Routing" in worker
    assert "## Output" in worker

    lite = _prompt("lite")
    assert "no user-confirmation channel" in lite
    assert "## Execution" in lite
    assert "## Output" in lite
    assert "## Budget Awareness" not in lite


def test_workflow_does_not_name_unavailable_tools():
    """workflow 只使用本会话可见的工具，不再点名 write/edit/bash 等。"""
    prompt = _prompt("workflow")
    assert "write/edit/bash" not in prompt
    cfg = load_role("workflow")
    for name in ("write", "edit", "bash", "python", "browser", "agent"):
        assert name not in cfg.tools
    assert "workflow_agent(step=N)" in prompt


def test_prompt_budgets():
    """规则文本与工具/技能段保持在预算内。"""
    master = _prompt("master")
    assert len(master.split("## Tool Rules")[0]) < 20_000
    assert "## Available Skills" in master
    assert "## Tools" in master

    for role, limit in (("master", 4_000), ("worker", 3_500), ("lite", 1_500), ("workflow", 1_500)):
        cfg = load_role(role)
        block = next(
            b for b in cfg.system_prompt["blocks"]
            if b.get("type") == "text" and b.get("id") == "role"
        )
        content = block.get("content", [])
        text = "\n".join(content) if isinstance(content, list) else content
        assert len(text) < limit, f"{role} rule text too long: {len(text)}"