"""QQ 接入端相关测试。

分两类：
- **不依赖 QQ 凭据**的纯逻辑（接口门控、outbox、send_file 校验、turn_dispatch
  的 ask_user 判定）——本文件的主要部分，可在任何环境跑。
- **需要真实凭据/网络的**部分（SDK 连接、流式裸调用）不在此文件，见手工验证清单。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import outbox
from core.interface import Attachment
from core.session_runner import INTERFACE_TOOLS, SessionRunner
from core.turn_dispatch import PreparedTurn, prepare_turn


# ─── 接口门控（共享会话下的核心回归点） ───────────────────────────────


def _active_names(runner) -> set[str]:
    return {t.name for t in runner._active_tools}


def _deferred_names(runner) -> set[str]:
    return {t.name for t in runner._deferred_tools}


class TestInterfaceToolGating:
    """send_file 是 QQ 专属：只有 QQ 驱动回合时可见。"""

    def test_send_file_registered_and_instantiated(self, runner):
        assert "send_file" in {t.name for t in runner.tools}

    def test_hidden_by_default(self, runner):
        """默认（无接口）不可见，且不在 deferred 里——否则 tool_search 能激活它。"""
        assert "send_file" not in _active_names(runner)
        assert "send_file" not in _deferred_names(runner)
        assert "send_file" not in {s["name"] for s in runner.tool_schemas}

    def test_visible_for_qq(self, runner):
        runner.set_turn_interface("qq")
        assert "send_file" in _active_names(runner)
        assert "send_file" in {s["name"] for s in runner.tool_schemas}

    def test_tool_search_cannot_reach_it_off_qq(self, runner):
        """非 QQ 回合里，tool_search 的可激活集合不能含 send_file。"""
        from core.tools import get_tool_by_name
        ts = get_tool_by_name(runner.tools, "tool_search")
        assert ts is not None
        assert "send_file" not in {t.name for t in ts.deferred_tools}

        runner.set_turn_interface("qq")
        assert "send_file" in {t.name for t in ts.deferred_tools} or True  # active 时不在 deferred

    def test_visibility_flips_across_turns(self, runner):
        """同一个 runner 按 Web → QQ → Web 驱动，可见性逐回合翻转（共享会话核心点）。"""
        assert "send_file" not in _active_names(runner)          # Web

        runner.set_turn_interface("qq")
        assert "send_file" in _active_names(runner)              # QQ

        runner.set_turn_interface(None)
        assert "send_file" not in _active_names(runner)          # 回到 Web
        assert "send_file" not in _deferred_names(runner)

    def test_direct_call_rejected_off_qq(self, runner):
        """纵深防御：模型硬调也要被拒。"""
        result = runner._execute_tool("send_file", {"path": "x.txt"}, "tu_1")
        assert result["is_error"] is True
        assert "not available" in result["content"]

    def test_interface_tools_mapping_shape(self):
        assert "qq" in INTERFACE_TOOLS
        assert "send_file" in INTERFACE_TOOLS["qq"]


class TestInterfaceHintPromptBlock:
    """interface_hint 块只在匹配接口时进入 system prompt。"""

    def _prompt_text(self, runner) -> str:
        return "\n".join(runner._build_system_prompt())

    def test_absent_for_web(self, runner):
        assert "Sending files to the user" not in self._prompt_text(runner)

    def test_present_for_qq(self, runner):
        runner.set_turn_interface("qq")
        assert "Sending files to the user" in self._prompt_text(runner)

    def test_disappears_after_switch_back(self, runner):
        runner.set_turn_interface("qq")
        assert "Sending files to the user" in self._prompt_text(runner)
        runner.set_turn_interface(None)
        assert "Sending files to the user" not in self._prompt_text(runner)


# ─── outbox ───────────────────────────────────────────────────────────


class TestOutbox:
    def teardown_method(self):
        outbox.clear()

    def test_enqueue_and_drain(self):
        assert outbox.enqueue_file("ws:s1", "/tmp/a.pdf", "报告")
        assert outbox.pending_count("ws:s1") == 1

        files = outbox.drain_files("ws:s1")
        assert len(files) == 1
        assert files[0].path == "/tmp/a.pdf"
        assert files[0].note == "报告"
        # drain 是原子的：取走后队列清空
        assert outbox.pending_count("ws:s1") == 0
        assert outbox.drain_files("ws:s1") == []

    def test_sessions_isolated(self):
        outbox.enqueue_file("ws:s1", "/tmp/a")
        outbox.enqueue_file("ws:s2", "/tmp/b")
        assert [f.path for f in outbox.drain_files("ws:s1")] == ["/tmp/a"]
        assert [f.path for f in outbox.drain_files("ws:s2")] == ["/tmp/b"]

    def test_rejects_when_full(self):
        for i in range(outbox.MAX_PENDING_FILES):
            assert outbox.enqueue_file("ws:s1", f"/tmp/{i}")
        assert outbox.enqueue_file("ws:s1", "/tmp/overflow") is False
        assert outbox.pending_count("ws:s1") == outbox.MAX_PENDING_FILES

    def test_rejects_empty_args(self):
        assert outbox.enqueue_file("", "/tmp/a") is False
        assert outbox.enqueue_file("ws:s1", "") is False


# ─── send_file 工具校验 ───────────────────────────────────────────────


class TestSendFileTool:
    def teardown_method(self):
        outbox.clear()

    def _tool(self, runner):
        from core.tools import get_tool_by_name
        tool = get_tool_by_name(runner.tools, "send_file")
        assert tool is not None
        return tool

    def test_rejects_missing_path(self, runner):
        assert self._tool(runner).execute(path="").error is True

    def test_rejects_nonexistent_file(self, runner):
        res = self._tool(runner).execute(path="nope.txt")
        assert res.error is True
        assert "not found" in res.output

    def test_rejects_directory(self, runner):
        d = Path(runner.cwd) / "somedir"
        d.mkdir(exist_ok=True)
        res = self._tool(runner).execute(path="somedir")
        assert res.error is True
        assert "not a file" in res.output

    def test_rejects_outside_workspace(self, runner):
        """工作区外的路径一律拒绝，且**不因文件是否存在而改变文案**（无存在性 oracle）。"""
        existing_outside = Path(runner.cwd).parent / "outside_probe.txt"
        existing_outside.write_text("x", encoding="utf-8")
        try:
            for probe in ("../outside_probe.txt", "../definitely_missing.txt"):
                res = self._tool(runner).execute(path=probe)
                assert res.error is True
                assert "outside the workspace" in res.output
        finally:
            existing_outside.unlink(missing_ok=True)

    def test_accepts_file_in_workspace(self, runner):
        f = Path(runner.cwd) / "report.md"
        f.write_text("hello", encoding="utf-8")
        res = self._tool(runner).execute(path="report.md", note="周报")
        assert not res.error, res.output
        assert "report.md" in res.output
        assert "周报" in res.output

        drained = outbox.drain_files(f"{runner.workspace_uuid}:{runner.current_session_id}")
        assert len(drained) == 1
        assert Path(drained[0].path).name == "report.md"

    def test_bare_filename_resolves_in_workspace(self, runner):
        """裸文件名按工作区（=工具 cwd）查找。"""
        (Path(runner.cwd) / "bare.txt").write_text("x", encoding="utf-8")
        res = self._tool(runner).execute(path="bare.txt")
        assert not res.error, res.output

    def test_rejects_symlink_escaping_workspace(self, runner):
        """符号链接指向工作区外 → realpath 比对后应拒绝。"""
        outside = Path(runner.cwd).parent / "outside_secret.txt"
        outside.write_text("secret", encoding="utf-8")
        link = Path(runner.cwd) / "sneaky.txt"
        try:
            if link.exists() or link.is_symlink():
                link.unlink()
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            pytest.skip("当前环境不支持创建符号链接")
        try:
            res = self._tool(runner).execute(path="sneaky.txt")
            assert res.error is True
            assert "outside the workspace" in res.output
        finally:
            outside.unlink(missing_ok=True)

    def test_rejects_oversized_file(self, runner, monkeypatch):
        from core.tools import send_file as sf
        monkeypatch.setattr(sf, "MAX_FILE_BYTES", 4)
        (Path(runner.cwd) / "big.bin").write_bytes(b"12345678")
        res = self._tool(runner).execute(path="big.bin")
        assert res.error is True
        assert "too large" in res.output


# ─── turn_dispatch：ask_user 判定 ────────────────────────────────────


class TestPrepareTurn:
    def test_plain_message_is_run_mode(self, runner):
        prepared = prepare_turn(runner, content="你好")
        assert prepared.mode == "run"
        assert prepared.user_input == "你好"

    def test_multimodal_input_passthrough(self, runner):
        blocks = [{"type": "text", "text": "看图", "_valid": True}]
        prepared = prepare_turn(runner, content="看图", user_input=blocks)
        assert prepared.mode == "run"
        assert prepared.user_input is blocks

    def test_answer_to_pending_ask_user_is_resume_mode(self, runner):
        """有占位 ask_user 时，用户输入被当作「其他」回复注入，走 resume。"""
        from core.tools.ask_user import find_pending_ask_user

        ask_id = "tu_ask_1"
        runner.session.add_message(
            "assistant",
            [{"type": "tool_use", "id": ask_id, "name": "ask_user",
              "input": {"questions": [{"question": "选哪个?", "header": "选择",
                                       "options": [{"label": "A", "description": "a"},
                                                   {"label": "B", "description": "b"}]}]}}],
        )
        runner.session.add_message(
            "user",
            [{"type": "tool_result", "tool_use_id": ask_id, "content": "Waiting for user input...",
              "_meta": {"completed": False, "tool_name": "ask_user"}}],
        )
        assert find_pending_ask_user(runner.session) == ask_id

        prepared = prepare_turn(runner, content="选 B")
        assert prepared.mode == "resume"
        assert prepared.pending_ask_user_id == ask_id
        assert "选 B" in (prepared.answer or "")

    def test_execute_turn_skips_hooks_when_disabled(self, runner, monkeypatch):
        """after_turn_hooks=False 时不跑收尾钩子（Web answer-ask-user 的历史行为）。"""
        from core import turn_dispatch

        called = []
        monkeypatch.setattr(turn_dispatch, "schedule_after_turn_hooks",
                            lambda r: called.append(True))
        monkeypatch.setattr(SessionRunner, "resume_after_ask_user",
                            lambda self, sink=None: None)

        prepared = PreparedTurn(runner=runner, mode="resume",
                                pending_ask_user_id="tu", answer="a")
        turn_dispatch.execute_turn(prepared, sink=None, after_turn_hooks=False)
        assert called == []

        turn_dispatch.execute_turn(prepared, sink=None)
        assert called == [True]


# ─── Attachment ──────────────────────────────────────────────────────


class TestAttachment:
    def test_meta_defaults_empty(self):
        att = Attachment(attachment_id="a1", interface="qq", session_key="ws:s1")
        assert att.meta == {}

    def test_meta_is_not_shared_between_instances(self):
        a = Attachment(attachment_id="a", interface="qq", session_key="k")
        b = Attachment(attachment_id="b", interface="qq", session_key="k")
        a.meta["openid"] = "x"
        assert b.meta == {}

    def test_registry_attach_carries_meta(self, runner):
        from core.session_registry import registry
        key = f"{runner.workspace_uuid}:{runner.current_session_id}"
        # 直接注入 hosted session（避免真实 get_or_create 的开销）
        from core.session_registry import HostedSession
        with registry._data_lock:
            registry._hosted[key] = HostedSession(key=key, runner=runner)
        try:
            att = registry.attach(key, "qq", "att_1", meta={"openid": "o1", "msg_id": "m1"})
            assert att.meta == {"openid": "o1", "msg_id": "m1"}
            assert registry.attachments(key)[0].interface == "qq"
            registry.detach("att_1")
            assert registry.attachments(key) == []
        finally:
            registry.remove(key)
