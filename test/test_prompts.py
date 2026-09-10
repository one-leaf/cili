"""System prompt building tests + shared SkillTool tests."""

import os
import json
import pytest
from datetime import datetime


# ─── prompts.py tests ────────────────────────────────────────────────────────


class TestPrompts:
    """配置化 prompt 构建测试：system prompt 块拼装 + user 层注入/合并 + 环境上下文。"""

    def _make_fake_agent(self, blocks, tools=None, role="master"):
        """构造 build_system_prompt 所需的轻量 agent 替身。"""
        from types import SimpleNamespace
        return SimpleNamespace(
            role=role,
            tools=tools or [],
            role_cfg=SimpleNamespace(system_prompt={"blocks": blocks}),
        )

    # -- build_environment_context (context 层) --

    def test_environment_context_basic(self):
        """context 层包含 workspace/cwd 与当前日期。"""
        from core.prompts import build_environment_context
        ctx = build_environment_context("test-uuid-123", "/test/workspace")
        assert "test-uuid-123" in ctx
        assert "/test/workspace" in ctx
        assert datetime.now().strftime("%Y-%m-%d") in ctx

    def test_environment_context_contains_memory_dir(self):
        """context 层包含 memory/knowledge/skills 说明。"""
        from core.prompts import build_environment_context
        ctx = build_environment_context("test-uuid", "/cwd")
        assert "memory" in ctx.lower()
        assert "knowledge" in ctx.lower()
        assert "skills" in ctx.lower()
        assert "test-uuid" in ctx

    def test_environment_context_shell_table(self):
        """context 层含三个 shell 环境的区分表格。"""
        from core.prompts import build_environment_context
        ctx = build_environment_context("u", "/cwd")
        assert "bash" in ctx
        assert "pwsh" in ctx
        assert "python" in ctx

    # -- find_project_instructions / build_instructions_message (claude_md 层) --

    def test_find_instructions_none(self, tmp_path):
        """无指令文件时返回 None。"""
        from core.prompts import find_project_instructions
        assert find_project_instructions(str(tmp_path)) is None

    def test_find_instructions_priority(self, tmp_path):
        """agent.md 优先于 CLAUDE.md。"""
        from core.prompts import find_project_instructions
        (tmp_path / "CLAUDE.md").write_text("claude content", encoding="utf-8")
        (tmp_path / "agent.md").write_text("agent content", encoding="utf-8")
        assert find_project_instructions(str(tmp_path)) == "agent content"

    def test_build_instructions_message_wraps(self, tmp_path):
        """指令文件被 <system-reminder> 包装为 user 消息。"""
        from core.prompts import build_instructions_message
        (tmp_path / "CLAUDE.md").write_text("do the thing", encoding="utf-8")
        msg = build_instructions_message(str(tmp_path))
        assert msg is not None
        assert msg["role"] == "user"
        assert "<system-reminder>" in msg["content"]
        assert "do the thing" in msg["content"]

    def test_build_instructions_message_none(self, tmp_path):
        """无指令文件时返回 None。"""
        from core.prompts import build_instructions_message
        assert build_instructions_message(str(tmp_path)) is None

    # -- build_system_prompt (块拼装) --

    def test_system_prompt_text_block_only(self):
        """纯 text 块：按 content 原样输出。"""
        from core.prompt_builder import build_system_prompt
        agent = self._make_fake_agent([
            {"id": "role", "type": "text", "content": "你是通用助手。"},
        ])
        assert build_system_prompt(agent) == "你是通用助手。"

    def test_system_prompt_text_block_lines(self):
        """text 块 content 为行数组时按行拼装。"""
        from core.prompt_builder import build_system_prompt
        agent = self._make_fake_agent([
            {"id": "role", "type": "text", "content": ["line one", "line two"]},
        ])
        assert build_system_prompt(agent) == "line one\nline two"

    def test_system_prompt_disabled_block_skipped(self):
        """enabled=false 的块被跳过。"""
        from core.prompt_builder import build_system_prompt
        agent = self._make_fake_agent([
            {"id": "a", "type": "text", "content": "A", "enabled": False},
            {"id": "b", "type": "text", "content": "B"},
        ])
        assert build_system_prompt(agent) == "B"

    def test_system_prompt_joins_blocks(self):
        """多个启用块按顺序用空行拼接。"""
        from core.prompt_builder import build_system_prompt
        agent = self._make_fake_agent([
            {"id": "a", "type": "text", "content": "AAA"},
            {"id": "b", "type": "text", "content": "BBB"},
        ])
        assert build_system_prompt(agent) == "AAA\n\nBBB"

    def test_system_prompt_tools_block(self):
        """tools 块列出工具名与首行描述。"""
        from types import SimpleNamespace
        from core.prompt_builder import build_system_prompt
        tool1 = SimpleNamespace(name="read", description="read a file\nmultiline")
        tool2 = SimpleNamespace(name="bash", description="run commands")
        agent = self._make_fake_agent(
            [{"id": "tools", "type": "tools"}], tools=[tool1, tool2])
        prompt = build_system_prompt(agent)
        assert "## Tools" in prompt
        assert "- **read** — read a file" in prompt
        assert "- **bash** — run commands" in prompt

    def test_system_prompt_skills_block(self):
        """skills 块列出角色可见技能（master 可见 grilling）。"""
        from core.prompt_builder import build_system_prompt
        agent = self._make_fake_agent(
            [{"id": "skills", "type": "skills"}], role="master")
        prompt = build_system_prompt(agent)
        assert "## Available Skills" in prompt
        assert "grilling" in prompt

    def test_system_prompt_unknown_block_type_skipped(self):
        """未知块类型跳过，不影响其他块。"""
        from core.prompt_builder import build_system_prompt
        agent = self._make_fake_agent([
            {"id": "x", "type": "bogus", "content": "X"},
            {"id": "role", "type": "text", "content": "OK"},
        ])
        assert build_system_prompt(agent) == "OK"

    def test_system_prompt_master_has_no_placeholders(self):
        """master 固定文案不含动态占位符（动态内容走 context 层）。"""
        from core.prompt_builder import build_system_prompt
        from core.agent_config import load_agent_role
        from types import SimpleNamespace
        config = SimpleNamespace(model=SimpleNamespace(),
                                 system=SimpleNamespace(max_iterations=50))
        role_cfg = load_agent_role("master", config)
        agent = self._make_fake_agent(role_cfg.system_prompt["blocks"])
        prompt = build_system_prompt(agent)
        assert "{date}" not in prompt
        assert "{workspace_uuid}" not in prompt
        assert "{cwd}" not in prompt
        assert "{memory_dir}" not in prompt

    # -- assemble_context (user 层合并 + 防连续) --

    def test_assemble_no_inject_returns_original(self):
        """无注入消息时返回原列表（不修改入参）。"""
        from core.prompt_builder import assemble_context
        messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
        assert assemble_context(messages, []) == messages

    def test_assemble_inject_at_front(self):
        """注入消息排在最前（与首条 user 合并，注入内容在前）。"""
        from core.prompt_builder import assemble_context
        inject = [{"role": "user", "content": "INJECT"}]
        history = [{"role": "user", "content": "history"}]
        result = assemble_context(history, inject)
        assert result[0]["content"].startswith("INJECT")
        assert result[0]["content"] == "INJECT\n\nhistory"

    def test_assemble_merges_consecutive_user(self):
        """连续 user 消息合并成一条，保持角色交替。"""
        from core.prompt_builder import assemble_context
        inject = [{"role": "user", "content": "A"}]
        history = [
            {"role": "user", "content": "B"},
            {"role": "assistant", "content": "X"},
            {"role": "user", "content": "C"},
        ]
        result = assemble_context(history, inject)
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "A\n\nB"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "user"
        assert result[2]["content"] == "C"
        assert len(result) == 3

    def test_assemble_preserves_meta(self):
        """合并保留第一条消息的 _meta。"""
        from core.prompt_builder import assemble_context
        inject = [{"role": "user", "content": "A", "_meta": {"id": "x"}}]
        history = [{"role": "user", "content": "B"}]
        result = assemble_context(history, inject)
        assert result[0]["_meta"] == {"id": "x"}

    def test_assemble_does_not_mutate_input(self):
        """合并不修改入参列表与消息。"""
        from core.prompt_builder import assemble_context
        inject = [{"role": "user", "content": "A"}]
        history = [{"role": "user", "content": "B"}]
        assemble_context(history, inject)
        assert history == [{"role": "user", "content": "B"}]
        assert inject == [{"role": "user", "content": "A"}]


# ─── shared SkillTool tests ──────────────────────────────────────────────────


class TestSkillTool:
    """Tests for core/tools/skill.py — 统一 SkillTool + frontmatter roles 过滤。"""

    def test_list_skills_master(self):
        """master 可见全部 master-only skills 与 file-processing（三角色可见）。"""
        from core.tools.skill import list_skills
        ids = {s["id"] for s in list_skills("master")}
        assert "grilling" in ids
        assert "code-review" in ids
        assert "file-processing" in ids
        assert "context-bounded-processing" not in ids  # worker-only

    def test_list_skills_worker(self):
        """worker 可见 worker-only 与 file-processing，看不到 master-only。"""
        from core.tools.skill import list_skills
        ids = {s["id"] for s in list_skills("worker")}
        assert "context-bounded-processing" in ids
        assert "file-processing" in ids
        assert "grilling" not in ids

    def test_list_skills_lite(self):
        """lite 只见 file-processing（roles 含 lite），其余均不可见。"""
        from core.tools.skill import list_skills
        ids = {s["id"] for s in list_skills("lite")}
        assert "file-processing" in ids
        assert "grilling" not in ids
        assert "context-bounded-processing" not in ids

    def test_read_skill_master_skill(self):
        """read_skill(role, id) 返回 skill.md 全文。"""
        from core.tools.skill import read_skill
        content = read_skill("master", "grilling")
        assert content is not None
        assert "---" in content

    def test_read_skill_nonexistent(self):
        from core.tools.skill import read_skill
        assert read_skill("master", "nonexistent-skill-xyz") is None

    def test_skill_tool_list_action(self):
        """SkillTool execute(list) 返回格式化技能列表。"""
        from core.tools.skill import SkillTool
        tool = SkillTool(role="master", cwd=".", workspace_uuid="test")
        result = tool.execute(action="list")
        assert not result.error
        assert "Available skills for master" in result.output

    def test_skill_tool_read_action(self):
        """SkillTool execute(read) 返回完整 skill 内容。"""
        from core.tools.skill import SkillTool
        tool = SkillTool(role="master", cwd=".", workspace_uuid="test")
        result = tool.execute(action="read", skill_id="grilling")
        assert not result.error
        assert "---" in result.output

    def test_skill_tool_read_missing_skill(self):
        from core.tools.skill import SkillTool
        tool = SkillTool(role="master", cwd=".", workspace_uuid="test")
        result = tool.execute(action="read", skill_id="does-not-exist")
        assert result.error
        assert "not found" in result.output

    def test_skill_tool_read_invalid_id(self):
        from core.tools.skill import SkillTool
        tool = SkillTool(role="master", cwd=".", workspace_uuid="test")
        result = tool.execute(action="read", skill_id="../etc/passwd")
        assert result.error  # 非法 id 直接视为不存在

    def test_skill_tool_read_requires_skill_id(self):
        from core.tools.skill import SkillTool
        tool = SkillTool(role="master", cwd=".", workspace_uuid="test")
        result = tool.execute(action="read")
        assert result.error

    def test_skill_tool_unknown_action(self):
        from core.tools.skill import SkillTool
        tool = SkillTool(role="master", cwd=".", workspace_uuid="test")
        result = tool.execute(action="delete")
        assert result.error

    def test_skill_tool_role_visibility(self):
        """worker 角色看不到 master-only 技能，能读 worker 技能。"""
        from core.tools.skill import SkillTool
        tool = SkillTool(role="worker", cwd=".", workspace_uuid="test")
        ok = tool.execute(action="read", skill_id="context-bounded-processing")
        assert not ok.error
        denied = tool.execute(action="read", skill_id="grilling")
        assert denied.error


class TestParseSkillFrontmatter:
    """Tests for _parse_skill_frontmatter (shared parser)."""

    def test_basic_frontmatter(self):
        from core.tools.skill import _parse_skill_frontmatter
        content = '---\nname: Test Skill\ndescription: A test\n---\nBody'
        result = _parse_skill_frontmatter(content)
        assert result["name"] == "Test Skill"
        assert result["description"] == "A test"

    def test_quoted_values(self):
        from core.tools.skill import _parse_skill_frontmatter
        content = '---\nname: "Quoted Name"\ntitle: \'Single Quoted\'\n---\n'
        result = _parse_skill_frontmatter(content)
        assert result["name"] == "Quoted Name"
        assert result["title"] == "Single Quoted"

    def test_array_values(self):
        """tags: [a, b, c] should be parsed as a list."""
        from core.tools.skill import _parse_skill_frontmatter
        content = '---\ntags: [python, async, networking]\n---\n'
        result = _parse_skill_frontmatter(content)
        assert result["tags"] == ["python", "async", "networking"]

    def test_no_frontmatter(self):
        from core.tools.skill import _parse_skill_frontmatter
        assert _parse_skill_frontmatter("no frontmatter here") == {}

    def test_unclosed_frontmatter(self):
        from core.tools.skill import _parse_skill_frontmatter
        assert _parse_skill_frontmatter("---\nname: broken") == {}
