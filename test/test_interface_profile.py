"""接口能力档案（core/interface_profile）测试。

覆盖：
- 注册表与解析规则（未声明不猜 / autonomous → agent / 未知名字告警）
- 能力段生成（各接口的事实行、否定式声明、无 web-based UI 残留）
- send_file 描述随档案变化 + Web 回合可见性
- 出站文件 web 落地（drain_outbox_events：file 帧 + file_card 消息）
- 技能按接口过滤（frontmatter interfaces）
- 角色 JSON 的 interface_caps 块声明与静态区 Web 假设清除
- 终端样例档案（interfaces/tui）
"""

from __future__ import annotations

import json
import logging
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

# 注册副作用：三份接入端档案 + 终端样例
import interfaces.qq.profile  # noqa: F401
import interfaces.tui.profile  # noqa: F401
import interfaces.wechat.profile  # noqa: F401
import web.profile  # noqa: F401
from core.interface_profile import (
    AGENT_PROFILE,
    InterfaceProfile,
    get_profile,
    render_capability_section,
    resolve_profile,
)

_AGENTS_DIR = Path(__file__).resolve().parent.parent / "core" / "agents"


class _FakeRunner:
    """resolve_profile 只读 _turn_interface 与 _mode 两个属性。"""

    def __init__(self, interface=None, mode="interactive"):
        self._turn_interface = interface
        self._mode = mode


# ─── 注册表与解析 ─────────────────────────────────────────────────────


class TestProfileRegistry:
    def test_three_interfaces_registered(self):
        for name in ("web", "qq", "wechat"):
            profile = get_profile(name)
            assert profile is not None
            assert profile.name == name

    def test_unknown_name_returns_none_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="core.interface_profile"):
            assert get_profile("no-such-iface") is None
        assert any("no-such-iface" in r.message for r in caplog.records)

    def test_none_name_returns_none_without_warning(self):
        assert get_profile(None) is None

    def test_agent_profile_is_core_owned(self):
        assert AGENT_PROFILE.name == "agent"
        assert get_profile("agent") is AGENT_PROFILE


class TestResolveProfile:
    def test_declared_interface_wins(self):
        assert resolve_profile(_FakeRunner("qq")).name == "qq"

    def test_interactive_undeclared_is_none(self):
        """交互态未声明：不猜——未声明就不注入。"""
        assert resolve_profile(_FakeRunner(None, "interactive")) is None

    def test_autonomous_undeclared_is_agent(self):
        assert resolve_profile(_FakeRunner(None, "autonomous")) is AGENT_PROFILE

    def test_autonomous_with_interface_uses_interface(self):
        assert resolve_profile(_FakeRunner("web", "autonomous")).name == "web"


# ─── 能力段内容 ───────────────────────────────────────────────────────


class TestRenderedSections:
    def test_qq_states_limit_and_not_rendered(self):
        text = render_capability_section(get_profile("qq"))
        assert "4000" in text
        assert "NOT rendered" in text
        assert "send_file" in text
        assert "200MB" in text
        assert "PNG" in text

    def test_wechat_states_1800_and_sanitization(self):
        text = render_capability_section(get_profile("wechat"))
        assert "1800" in text
        assert "NOT rendered" in text
        assert "stripped" in text

    def test_web_no_char_limit_and_bans_rasterizing(self):
        text = render_capability_section(get_profile("web"))
        assert "4000" not in text
        assert "1800" not in text
        assert "mermaid" in text and "SVG" in text
        assert "rasterize" in text.lower()
        # 原静态区迁来的规则：相对路径引用文件
        assert "workspace-relative path" in text

    def test_agent_guidance_bans_ui_artifacts(self):
        text = render_capability_section(AGENT_PROFILE)
        assert "calling agent" in text
        assert "send_file" in text

    def test_no_section_claims_web_ui(self):
        """回归：能力段不得再引入 web-based UI 断言（静态区已清除）。"""
        for name in ("web", "qq", "wechat"):
            assert "web-based UI" not in render_capability_section(get_profile(name))


# ─── send_file 描述随档案变化 ─────────────────────────────────────────


class TestSendFileDescription:
    @staticmethod
    def _tool():
        from core.tools.send_file import SendFileTool
        return SendFileTool(cwd=".", workspace_uuid="ws", session=None)

    def test_base_description_without_profile(self):
        tool = self._tool()
        assert "workspace" in tool.description
        # 接口未声明时不带 QQ 专属上限（200MB 由档案追加）
        assert "200MB" not in tool.description

    def test_qq_profile_adds_limits(self):
        tool = self._tool()
        tool.set_interface_profile(get_profile("qq"))
        assert "200MB" in tool.description
        assert "PNG" in tool.description

    def test_link_delivery_mentioned_for_web(self):
        """Web 档案是 link 交付：描述提及 download link。"""
        tool = self._tool()
        tool.set_interface_profile(get_profile("web"))
        assert "download link" in tool.description
        assert "200MB" in tool.description  # 与 send_file.MAX_FILE_BYTES 对齐

    def test_link_delivery_mentioned_synthetic(self):
        profile = InterfaceProfile(
            name="fake", label="fake", can_send_file=True, file_delivery="link",
        )
        tool = self._tool()
        tool.set_interface_profile(profile)
        assert "download link" in tool.description


# ─── 角色 JSON 的块声明 ───────────────────────────────────────────────


def _blocks(role: str) -> list[dict]:
    cfg = json.loads((_AGENTS_DIR / f"{role}.json").read_text(encoding="utf-8"))
    return cfg["system_prompt"]["blocks"]


class TestRoleJsonBlocks:
    @pytest.mark.parametrize("role", ["master", "worker", "lite"])
    def test_interface_caps_block_declared(self, role):
        """三个角色都在动态区声明 interface_caps 块。"""
        blocks = _blocks(role)
        past_boundary = False
        caps = False
        for block in blocks:
            if block.get("type") == "dynamic_boundary":
                past_boundary = True
            if block.get("type") == "interface_caps":
                assert past_boundary, "interface_caps 必须在动态区（每轮可能变化）"
                assert block.get("enabled", True)
                caps = True
        assert caps, f"{role} 缺少 interface_caps 块"

    def test_master_static_region_has_no_web_assumption(self):
        """静态区不再写死 Web 假设（原第 12/94/109 行）。"""
        static_texts: list[str] = []
        for block in _blocks("master"):
            if block.get("type") == "dynamic_boundary":
                break
            if block.get("type") == "text":
                content = block.get("content", "")
                static_texts.append("\n".join(content) if isinstance(content, list) else content)
        joined = "\n".join(static_texts)
        assert "web-based UI" not in joined
        assert "Cili's web interface" not in joined
        assert "The web UI automatically resolves" not in joined

    def test_master_hint_blocks_folded_into_profiles(self):
        """两块 interface_hint 散文已删除（内容并入档案 guidance）。"""
        hint_blocks = [b for b in _blocks("master") if b.get("type") == "interface_hint"]
        assert hint_blocks == []


# ─── Web 的 send_file 落地（第二阶段） ────────────────────────────────


class TestWebSendFileVisibility:
    def test_web_turn_has_send_file(self, runner):
        """set_turn_interface("web") 后 send_file 进入 tool_schemas。"""
        runner.set_turn_interface("web")
        assert "send_file" in {s["name"] for s in runner.tool_schemas}
        runner.set_turn_interface(None)
        assert "send_file" not in {s["name"] for s in runner.tool_schemas}


class TestDrainOutboxEvents:
    """web/sse.drain_outbox_events：file 帧 + file_card 消息 + 队列清空。"""

    @staticmethod
    def _fake_runner(tmp_path):
        added: list[tuple] = []
        session = SimpleNamespace(
            add_message=lambda role, content, meta=None, flush=False: added.append(
                (role, content, meta)
            )
        )
        runner = SimpleNamespace(
            workspace_uuid="ws-drain",
            current_session_id="s-drain",
            cwd=str(tmp_path),
            session=session,
        )
        return runner, added

    def test_drain_emits_frame_and_persists_card(self, tmp_path):
        from core import outbox
        from web.sse import drain_outbox_events

        (tmp_path / "report.pdf").write_bytes(b"x")
        assert outbox.enqueue_file("ws-drain:s-drain", str(tmp_path / "report.pdf"), "季度报告")

        runner, added = self._fake_runner(tmp_path)
        q: queue.Queue = queue.Queue()
        drain_outbox_events(runner, q)

        frame = json.loads(q.get_nowait().removeprefix("data: ").strip())
        assert frame["type"] == "file"
        assert frame["name"] == "report.pdf"
        assert frame["path"] == "report.pdf"
        assert frame["note"] == "季度报告"
        assert frame["size"] == 1

        role, content, meta = added[0]
        assert role == "assistant"
        assert "report.pdf" in content and "report.pdf" in meta["file_card"]["path"]
        # 与 error_notice 同一机制：UI 可见、不进 LLM 上下文
        assert "file_card" in meta

        assert outbox.drain_files("ws-drain:s-drain") == []

    def test_space_in_filename_url_encoded(self, tmp_path):
        from core import outbox
        from web.sse import drain_outbox_events

        (tmp_path / "my report.pdf").write_bytes(b"x")
        outbox.enqueue_file("ws-drain:s-drain", str(tmp_path / "my report.pdf"))

        runner, _ = self._fake_runner(tmp_path)
        q: queue.Queue = queue.Queue()
        drain_outbox_events(runner, q)

        frame = json.loads(q.get_nowait().removeprefix("data: ").strip())
        assert frame["path"] == "my%20report.pdf"

    def test_empty_outbox_is_noop(self, tmp_path):
        from web.sse import drain_outbox_events

        runner, added = self._fake_runner(tmp_path)
        q: queue.Queue = queue.Queue()
        drain_outbox_events(runner, q)
        assert q.empty()
        assert added == []


# ─── 技能按接口过滤（第三阶段） ───────────────────────────────────────


class TestSkillInterfaceFilter:
    @staticmethod
    def _make_skills_dir(tmp_path):
        """两个技能：web-only 与无接口限制。返回目录路径。"""
        web_only = tmp_path / "web-only"
        web_only.mkdir()
        (web_only / "skill.md").write_text(
            "---\nname: Web Only\ndescription: only for web\n"
            "roles: [master]\ninterfaces: [web]\n---\n\nbody\n",
            encoding="utf-8",
        )
        universal = tmp_path / "universal"
        universal.mkdir()
        (universal / "skill.md").write_text(
            "---\nname: Universal\ndescription: for everyone\n"
            "roles: [master]\n---\n\nbody\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_visible_for_matrix(self):
        from core.tools.skill import _visible_for

        meta = {"roles": "master", "interfaces": "web"}
        assert _visible_for(meta, "master", "web")
        assert not _visible_for(meta, "master", "qq")
        assert not _visible_for(meta, "master", None)   # 未声明 → 不可见（宁缺勿猜）
        assert not _visible_for(meta, "worker", "web")  # roles 不匹配

        free = {"roles": "master"}
        assert _visible_for(free, "master", None)
        assert _visible_for(free, "master", "qq")

    def test_list_skills_filters_by_interface(self, tmp_path, monkeypatch):
        import core.tools.skill as skill_mod

        monkeypatch.setattr(skill_mod, "_SKILLS_DIR", str(self._make_skills_dir(tmp_path)))
        assert [s["id"] for s in skill_mod.list_skills("master", "web")] == ["universal", "web-only"]
        assert [s["id"] for s in skill_mod.list_skills("master", "qq")] == ["universal"]
        assert [s["id"] for s in skill_mod.list_skills("master", None)] == ["universal"]

    def test_read_skill_respects_interface(self, tmp_path, monkeypatch):
        import core.tools.skill as skill_mod

        monkeypatch.setattr(skill_mod, "_SKILLS_DIR", str(self._make_skills_dir(tmp_path)))
        assert skill_mod.read_skill("master", "web-only", "web") is not None
        assert skill_mod.read_skill("master", "web-only", "qq") is None
        assert skill_mod.read_skill("master", "web-only", None) is None

    def test_skill_tool_consumes_profile(self, tmp_path, monkeypatch):
        import core.tools.skill as skill_mod
        from core.tools.skill import SkillTool

        monkeypatch.setattr(skill_mod, "_SKILLS_DIR", str(self._make_skills_dir(tmp_path)))
        tool = SkillTool(role="master", cwd=".", workspace_uuid="ws", session=None)

        tool.set_interface_profile(get_profile("web"))
        assert "web-only" in tool._list_skills().output
        tool.set_interface_profile(get_profile("qq"))
        assert "web-only" not in tool._list_skills().output
        tool.set_interface_profile(None)
        assert "web-only" not in tool._list_skills().output


# ─── 终端样例档案（第三阶段） ─────────────────────────────────────────


class TestTuiSampleProfile:
    def test_tui_profile_registered(self):
        profile = get_profile("tui")
        assert profile is not None
        assert "markdown" in profile.renders
        assert profile.image_protocol in ("none", "sixel", "kitty", "iterm2")
        assert not profile.can_send_file
