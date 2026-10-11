"""System prompt building tests + shared SkillTool tests."""

import os
import json
import re
import pytest
from datetime import datetime


# ─── prompts.py tests ────────────────────────────────────────────────────────


class TestPrompts:
    """配置化 prompt 构建测试：system prompt 块拼装 + user 层注入/合并 + 环境上下文。"""

    def _make_fake_runner(self, blocks, tools=None, role="master"):
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
        from core.prompt_sections import build_environment_context
        ctx = build_environment_context("test-uuid-123", "/test/workspace")
        assert "test-uuid-123" in ctx
        assert "/test/workspace" in ctx
        assert datetime.now().strftime("%Y-%m-%d") in ctx

    def test_environment_context_contains_memory_dir(self):
        """context 层包含 memory/knowledge/skills 说明。"""
        from core.prompt_sections import build_environment_context
        ctx = build_environment_context("test-uuid", "/cwd")
        assert "memory" in ctx.lower()
        assert "knowledge" in ctx.lower()
        assert "skills" in ctx.lower()
        assert "test-uuid" in ctx

    def test_environment_context_shell_table(self):
        """context 层含三个 shell 环境的区分表格。"""
        from core.prompt_sections import build_environment_context
        ctx = build_environment_context("u", "/cwd")
        assert "bash" in ctx
        assert "pwsh" in ctx
        assert "python" in ctx

    # -- find_project_instructions / build_instructions_message (claude_md 层) --

    def test_find_instructions_none(self, tmp_path):
        """无指令文件时返回 None。"""
        from core.prompt_sections import find_project_instructions
        assert find_project_instructions(str(tmp_path)) is None

    def test_find_instructions_priority(self, tmp_path):
        """agent.md 优先于 CLAUDE.md。"""
        from core.prompt_sections import find_project_instructions
        (tmp_path / "CLAUDE.md").write_text("claude content", encoding="utf-8")
        (tmp_path / "agent.md").write_text("agent content", encoding="utf-8")
        assert find_project_instructions(str(tmp_path)) == "agent content"

    def test_build_instructions_message_wraps(self, tmp_path):
        """指令文件被 <system-reminder> 包装为 user 消息。"""
        from core.prompt_sections import build_instructions_message
        (tmp_path / "CLAUDE.md").write_text("do the thing", encoding="utf-8")
        msg = build_instructions_message(str(tmp_path))
        assert msg is not None
        assert msg["role"] == "user"
        assert "<system-reminder>" in msg["content"]
        assert "do the thing" in msg["content"]

    def test_build_instructions_message_none(self, tmp_path):
        """无指令文件时返回 None。"""
        from core.prompt_sections import build_instructions_message
        assert build_instructions_message(str(tmp_path)) is None

    # -- build_system_prompt (块拼装) --

    def test_system_prompt_text_block_only(self):
        """纯 text 块：按 content 原样输出（返回 list[str]）。"""
        from core.prompt_builder import build_system_prompt
        runner = self._make_fake_runner([
            {"id": "role", "type": "text", "content": "你是通用助手。"},
        ])
        assert build_system_prompt(runner) == ["你是通用助手。"]

    def test_system_prompt_text_block_lines(self):
        """text 块 content 为行数组时按行拼装。"""
        from core.prompt_builder import build_system_prompt
        runner = self._make_fake_runner([
            {"id": "role", "type": "text", "content": ["line one", "line two"]},
        ])
        assert build_system_prompt(runner) == ["line one\nline two"]

    def test_system_prompt_disabled_block_skipped(self):
        """enabled=false 的块被跳过。"""
        from core.prompt_builder import build_system_prompt
        runner = self._make_fake_runner([
            {"id": "a", "type": "text", "content": "A", "enabled": False},
            {"id": "b", "type": "text", "content": "B"},
        ])
        assert build_system_prompt(runner) == ["B"]

    def test_system_prompt_joins_blocks(self):
        """多个启用块按顺序放入列表（不再拼接为单字符串）。"""
        from core.prompt_builder import build_system_prompt
        runner = self._make_fake_runner([
            {"id": "a", "type": "text", "content": "AAA"},
            {"id": "b", "type": "text", "content": "BBB"},
        ])
        assert build_system_prompt(runner) == ["AAA", "BBB"]

    def test_system_prompt_tools_block(self):
        """tools 块列出工具名与首行描述。"""
        from types import SimpleNamespace
        from core.prompt_builder import build_system_prompt
        tool1 = SimpleNamespace(name="read", description="read a file\nmultiline")
        tool2 = SimpleNamespace(name="bash", description="run commands")
        runner = self._make_fake_runner(
            [{"id": "tools", "type": "tools"}], tools=[tool1, tool2])
        parts = build_system_prompt(runner)
        prompt = "\n\n".join(parts)
        assert "## Tools" in prompt
        assert "- **read** — read a file" in prompt
        assert "- **bash** — run commands" in prompt

    def test_system_prompt_skills_block(self):
        """skills 块列出角色可见技能（master 可见 grilling）。"""
        from core.prompt_builder import build_system_prompt
        runner = self._make_fake_runner(
            [{"id": "skills", "type": "skills"}], role="master")
        parts = build_system_prompt(runner)
        prompt = "\n\n".join(parts)
        assert "## Available Skills" in prompt
        assert "grilling" in prompt

    def test_system_prompt_unknown_block_type_skipped(self):
        """未知块类型跳过，不影响其他块。"""
        from core.prompt_builder import build_system_prompt
        runner = self._make_fake_runner([
            {"id": "x", "type": "bogus", "content": "X"},
            {"id": "role", "type": "text", "content": "OK"},
        ])
        assert build_system_prompt(runner) == ["OK"]

    def test_system_prompt_master_has_no_placeholders(self):
        """master 固定文案不含动态占位符（动态内容走 context 层）。"""
        from core.prompt_builder import build_system_prompt
        from core.role_config import load_role
        from types import SimpleNamespace
        config = SimpleNamespace(model=SimpleNamespace(),
                                 system=SimpleNamespace(max_iterations=50))
        role_cfg = load_role("master", config)
        runner = self._make_fake_runner(role_cfg.system_prompt["blocks"])
        # context 块需要 workspace_uuid 和 cwd
        runner.workspace_uuid = "test-workspace"
        runner.cwd = "/test/cwd"
        parts = build_system_prompt(runner)
        prompt = "\n\n".join(parts)
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

    def test_skill_files_are_lowercase(self):
        """技能文件名必须是小写 ``skill.md``。

        加载器只找小写（``core/tools/skill.py`` 里的 ``os.path.join(dir, "skill.md")``）。
        在**大小写敏感**的文件系统上（Linux/macOS），`SKILL.md` 会被**静默跳过**——
        技能不出现在列表里，也不报任何错，极难排查。Windows 大小写不敏感所以看不出来，
        这个断言就是替代那条看不见的失败。
        """
        from core.tools.skill import _SKILLS_DIR

        offenders = []
        for entry in os.listdir(_SKILLS_DIR):
            d = os.path.join(_SKILLS_DIR, entry)
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if f.lower() == "skill.md" and f != "skill.md":
                    offenders.append(f"{entry}/{f}")
        assert not offenders, f"技能文件名必须是小写 skill.md：{offenders}"

    def test_diagram_skills_have_disjoint_triggers(self):
        """两个画图技能的触发词必须互斥。

        技能选择**没有优先级机制**——模型只看到 system prompt 里平铺的
        `- **名称** (id): 描述`，靠描述自己挑。两个技能若都声称「流程图/架构图」，
        模型可能挑中较重的那个（TikZ 要 LaTeX 工具链、输出 PDF），
        而 Mermaid 更轻、且前端直接渲染成 SVG。

        分工：mermaid = 通用流程图/架构图（默认路径）；tikz = 出版级/科学绘图。
        """
        from core.tools.skill import list_skills

        skills = {s["id"]: s["description"] for s in list_skills("master")}
        assert "mermaid-diagram" in skills and "tikz-diagram" in skills

        def triggers(desc: str) -> set[str]:
            m = re.search(r"Use when user mentions ([^。\n]+)", desc)
            return {t.strip() for t in re.split(r"[、,，]", m.group(1))} if m else set()

        mermaid = triggers(skills["mermaid-diagram"])
        tikz = triggers(skills["tikz-diagram"])

        assert not (mermaid & tikz), f"触发词重叠: {sorted(mermaid & tikz)}"
        # 通用流程图/架构图归 mermaid（唯一命中 → 必然出 SVG）
        assert "流程图" in mermaid and "流程图" not in tikz
        assert "架构图" in mermaid and "架构图" not in tikz
        # tikz 仍要能被它自己的领域发现
        assert "TikZ" in tikz and "PGFplots" in tikz

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
