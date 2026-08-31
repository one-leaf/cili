"""System prompt building tests + shared SkillTool tests."""

import os
import json
import pytest
from datetime import datetime


# ─── prompts.py tests ────────────────────────────────────────────────────────


class TestPrompts:
    """System prompt building tests."""

    # -- build_root_prompt (no-arg API) --

    def test_build_root_prompt_is_static(self):
        """build_root_prompt() returns the same result on repeated calls."""
        from core.prompts import build_root_prompt
        assert build_root_prompt() == build_root_prompt()

    def test_build_root_prompt_contains_tools(self):
        """Agent prompt dynamically includes all tool names."""
        from core.prompts import build_root_prompt
        prompt = build_root_prompt()
        for name in ["read", "write", "edit", "bash", "python", "grep",
                      "find", "browser", "memory", "web_search", "skill", "subagent"]:
            assert name in prompt, f"Tool '{name}' not in agent prompt"

    def test_root_prompt_describes_general_ai_assistant(self):
        from core.prompts import build_root_prompt
        prompt = build_root_prompt()
        assert "AI assistant" in prompt
        assert "not merely a programming assistant" in prompt
        assert "internal implementation details" in prompt

    def test_build_root_prompt_contains_rules(self):
        from core.prompts import build_root_prompt
        prompt = build_root_prompt()
        assert "Never" in prompt or "never" in prompt
        assert "python" in prompt.lower()
        assert "bash" in prompt.lower()

    def test_build_root_prompt_workspace_instructions(self):
        from core.prompts import build_root_prompt
        prompt = build_root_prompt()
        assert "CWD" in prompt or "cwd" in prompt

    def test_build_root_prompt_memory_instructions(self):
        from core.prompts import build_root_prompt
        prompt = build_root_prompt()
        assert "knowledge" in prompt
        assert "skills" in prompt
        assert "user" in prompt

    # test_build_root_prompt_no_dynamic_variables 已移除：
    # 动态环境变量（日期、workspace）已合并到 system prompt，这是预期行为。
    # 静态模板的无占位符检查由 test_root_prompt_template_no_placeholders 覆盖。

    def test_build_root_prompt_contains_url_format(self):
        from core.prompts import build_root_prompt
        prompt = build_root_prompt()
        assert "/api/workspace/files/" in prompt
        assert "plot.png" in prompt
        assert "![plot]" in prompt

    def test_root_prompt_python_bash_rules(self):
        from core.prompts import build_root_prompt
        prompt = build_root_prompt()
        assert "Never" in prompt or "never" in prompt
        assert "bash" in prompt.lower()
        assert "python" in prompt.lower()

    def test_root_prompt_communication_style(self):
        from core.prompts import build_root_prompt
        prompt = build_root_prompt()
        assert "concise" in prompt.lower() or "brief" in prompt.lower()

    # -- build_sub_prompt (no-arg API) --

    def test_build_sub_prompt_basic(self):
        from core.prompts import build_sub_prompt
        prompt = build_sub_prompt()
        assert "autonomous" in prompt.lower() or "autonomously" in prompt.lower()
        assert "task" in prompt.lower()

    def test_build_sub_prompt_with_tools(self):
        from core.prompts import build_sub_prompt
        prompt = build_sub_prompt()
        for name in ["read", "write", "edit", "bash", "python", "grep", "find"]:
            assert name in prompt, f"Tool '{name}' not in runtime prompt"

    def test_build_sub_prompt_is_static(self):
        from core.prompts import build_sub_prompt
        assert build_sub_prompt() == build_sub_prompt()

    # -- build_root_context --

    def test_build_root_context_basic(self):
        from core.prompts import build_root_context
        ctx = build_root_context("test-uuid-123", "/test/workspace")
        assert "test-uuid-123" in ctx
        assert "/test/workspace" in ctx
        assert datetime.now().strftime("%Y-%m-%d") in ctx

    def test_build_root_context_contains_memory_dir(self):
        from core.prompts import build_root_context
        ctx = build_root_context("test-uuid", "/cwd")
        assert "memory" in ctx
        assert "test-uuid" in ctx
        assert "skills" in ctx
        assert "knowledge" in ctx

    # -- build_sub_context --

    def test_build_sub_context_basic(self):
        from core.prompts import build_sub_context
        ctx = build_sub_context("test-uuid", "/test/path")
        assert "test-uuid" in ctx
        assert "/test/path" in ctx
        assert datetime.now().strftime("%Y-%m-%d") in ctx

    # -- build_llm_tool_system_prompt --

    def test_build_llm_tool_system_prompt(self):
        from core.prompts import build_llm_tool_system_prompt
        prompt = build_llm_tool_system_prompt()
        assert "text processing" in prompt.lower() or "process" in prompt.lower()
        assert "tool" not in prompt.lower()

    # -- static template has no placeholders --

    def test_root_prompt_template_no_placeholders(self):
        from core.prompts import ROOT_PROMPT_TEMPLATE
        assert "{date}" not in ROOT_PROMPT_TEMPLATE
        assert "{workspace_uuid}" not in ROOT_PROMPT_TEMPLATE
        assert "{cwd}" not in ROOT_PROMPT_TEMPLATE
        assert "{memory_dir}" not in ROOT_PROMPT_TEMPLATE


# ─── shared SkillTool tests ──────────────────────────────────────────────────


class TestSkillTool:
    """Tests for core/tools/shared/skill.py — the unified SkillTool."""

    def test_list_skills_root_dir(self):
        """list_skills() on root dir returns root + shared skills."""
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import list_skills
        skills = list_skills(str(PROJECT_ROOT / "core" / "skills" / "root"))
        ids = [s["id"] for s in skills]
        # root skills (no prefix)
        assert any(not s["id"].startswith("shared/") for s in skills)
        # shared skills (prefixed)
        assert any(s["id"].startswith("shared/") for s in skills)

    def test_list_skills_sub_dir(self):
        """list_skills() on sub dir returns sub + shared skills."""
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import list_skills
        skills = list_skills(str(PROJECT_ROOT / "core" / "skills" / "sub"))
        assert any(not s["id"].startswith("shared/") for s in skills)
        assert any(s["id"].startswith("shared/") for s in skills)

    def test_list_skills_root_and_sub_differ(self):
        """Root and sub should see different role-specific skills."""
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import list_skills
        root_skills = list_skills(str(PROJECT_ROOT / "core" / "skills" / "root"))
        sub_skills = list_skills(str(PROJECT_ROOT / "core" / "skills" / "sub"))
        root_ids = {s["id"] for s in root_skills if not s["id"].startswith("shared/")}
        sub_ids = {s["id"] for s in sub_skills if not s["id"].startswith("shared/")}
        # They scan different dirs so should have different role-specific ids
        assert root_ids != sub_ids

    def test_list_skills_shared_overlap(self):
        """Both root and sub share the same shared/ skills."""
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import list_skills
        root_shared = {s["id"] for s in list_skills(str(PROJECT_ROOT / "core" / "skills" / "root"))
                        if s["id"].startswith("shared/")}
        sub_shared = {s["id"] for s in list_skills(str(PROJECT_ROOT / "core" / "skills" / "sub"))
                          if s["id"].startswith("shared/")}
        assert root_shared == sub_shared

    def test_read_skill_root_skill(self):
        """read_skill() returns content for a known root skill."""
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import read_skill, list_skills
        skills_dir = str(PROJECT_ROOT / "core" / "skills" / "root")
        skills = list_skills(skills_dir)
        # Pick the first role-specific skill
        role_skill = next(s for s in skills if not s["id"].startswith("shared/"))
        content = read_skill(skills_dir, role_skill["id"])
        assert content is not None
        assert "---" in content  # has frontmatter

    def test_read_skill_shared_skill(self):
        """read_skill() returns content for a shared/ prefixed skill."""
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import read_skill, list_skills
        skills_dir = str(PROJECT_ROOT / "core" / "skills" / "root")
        skills = list_skills(skills_dir)
        shared_skill = next((s for s in skills if s["id"].startswith("shared/")), None)
        if shared_skill is None:
            pytest.skip("No shared skills present")
        content = read_skill(skills_dir, shared_skill["id"])
        assert content is not None

    def test_read_skill_nonexistent(self):
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import read_skill
        result = read_skill(str(PROJECT_ROOT / "core" / "skills" / "root"), "nonexistent-skill-xyz")
        assert result is None

    def test_skill_tool_list_action(self):
        """SkillTool execute(list) returns a formatted string."""
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import SkillTool
        tool = SkillTool(
            skills_dir=str(PROJECT_ROOT / "core" / "skills" / "root"),
            role_label="built-in",
            cwd=".", workspace_uuid="test",
        )
        result = tool.execute(action="list")
        assert not result.error
        assert "Available built-in skills" in result.output

    def test_skill_tool_read_action(self):
        """SkillTool execute(read) returns full skill content."""
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import SkillTool, list_skills
        skills_dir = str(PROJECT_ROOT / "core" / "skills" / "root")
        skill_id = next(s["id"] for s in list_skills(skills_dir)
                        if not s["id"].startswith("shared/"))
        tool = SkillTool(skills_dir=skills_dir, role_label="built-in",
                         cwd=".", workspace_uuid="test")
        result = tool.execute(action="read", skill_id=skill_id)
        assert not result.error
        assert "---" in result.output

    def test_skill_tool_read_missing_skill(self):
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import SkillTool
        tool = SkillTool(
            skills_dir=str(PROJECT_ROOT / "core" / "skills" / "root"),
            role_label="built-in",
            cwd=".", workspace_uuid="test",
        )
        result = tool.execute(action="read", skill_id="does-not-exist")
        assert result.error
        assert "not found" in result.output

    def test_skill_tool_read_invalid_id(self):
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import SkillTool
        tool = SkillTool(
            skills_dir=str(PROJECT_ROOT / "core" / "skills" / "root"),
            role_label="built-in",
            cwd=".", workspace_uuid="test",
        )
        result = tool.execute(action="read", skill_id="../etc/passwd")
        assert result.error
        assert "invalid" in result.output.lower()

    def test_skill_tool_read_requires_skill_id(self):
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import SkillTool
        tool = SkillTool(
            skills_dir=str(PROJECT_ROOT / "core" / "skills" / "root"),
            role_label="built-in",
            cwd=".", workspace_uuid="test",
        )
        result = tool.execute(action="read")
        assert result.error

    def test_skill_tool_unknown_action(self):
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import SkillTool
        tool = SkillTool(
            skills_dir=str(PROJECT_ROOT / "core" / "skills" / "root"),
            role_label="built-in",
            cwd=".", workspace_uuid="test",
        )
        result = tool.execute(action="delete")
        assert result.error

    def test_skill_tool_runtime_label(self):
        """Runtime-role label flows through to output."""
        from core.config import PROJECT_ROOT
        from core.tools.shared.skill import SkillTool
        tool = SkillTool(
            skills_dir=str(PROJECT_ROOT / "core" / "skills" / "sub"),
            role_label="runtime",
            cwd=".", workspace_uuid="test",
        )
        result = tool.execute(action="list")
        assert "Available runtime skills" in result.output


# ─── frontmatter parser tests ────────────────────────────────────────────────


class TestParseSkillFrontmatter:
    """Tests for _parse_skill_frontmatter (shared parser)."""

    def test_basic_frontmatter(self):
        from core.tools.shared.skill import _parse_skill_frontmatter
        content = '---\nname: Test Skill\ndescription: A test\n---\nBody'
        result = _parse_skill_frontmatter(content)
        assert result["name"] == "Test Skill"
        assert result["description"] == "A test"

    def test_quoted_values(self):
        from core.tools.shared.skill import _parse_skill_frontmatter
        content = '---\nname: "Quoted Name"\ntitle: \'Single Quoted\'\n---\n'
        result = _parse_skill_frontmatter(content)
        assert result["name"] == "Quoted Name"
        assert result["title"] == "Single Quoted"

    def test_array_values(self):
        """tags: [a, b, c] should be parsed as a list."""
        from core.tools.shared.skill import _parse_skill_frontmatter
        content = '---\ntags: [python, async, networking]\n---\n'
        result = _parse_skill_frontmatter(content)
        assert result["tags"] == ["python", "async", "networking"]

    def test_no_frontmatter(self):
        from core.tools.shared.skill import _parse_skill_frontmatter
        assert _parse_skill_frontmatter("no frontmatter here") == {}

    def test_unclosed_frontmatter(self):
        from core.tools.shared.skill import _parse_skill_frontmatter
        assert _parse_skill_frontmatter("---\nname: broken") == {}
