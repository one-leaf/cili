"""Tests for handoff document/tool, user-input compaction and snapshot padding."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from core.handoff import build_handoff_document, default_branch_name, user_input_text
from core.llm import Message, ToolCallBlock, ToolResultBlock
from core.session_runner_runtime.runner import Runner


class TestHandoffDocument:
    def test_contains_next_steps_and_reason(self):
        doc = build_handoff_document("实现登录", "并行探索")
        assert "## 下一阶段任务（立即执行）" in doc
        assert "实现登录" in doc
        assert "并行探索" in doc

    def test_braces_are_not_formatted(self):
        doc = build_handoff_document("处理 {a} 与 {b}", "")
        assert "处理 {a} 与 {b}" in doc


class TestUserInputText:
    def test_string(self):
        assert user_input_text("  hello  ") == "hello"

    def test_blocks(self):
        blocks = [
            {"type": "text", "text": "第一段"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "第二段"},
        ]
        assert user_input_text(blocks) == "第一段\n第二段"

    def test_empty(self):
        assert user_input_text([]) == ""
        assert user_input_text(None) == ""


class TestDefaultBranchName:
    def test_uses_label(self):
        assert default_branch_name("原会话", "实验分支") == "实验分支"

    def test_dedupe(self):
        existing = ["实验分支", "实验分支-2"]
        assert default_branch_name("原会话", "实验分支", existing) == "实验分支-3"

    def test_defaults_to_parent_name(self):
        assert default_branch_name("原会话", None) == "原会话·分支"


class TestPadDanglingInMessages:
    def test_pads_and_excludes(self):
        messages = [
            Message(role="assistant", content=[
                ToolCallBlock(id="keep", name="x", arguments="{}"),
                ToolCallBlock(id="excluded", name="y", arguments="{}"),
            ])
        ]
        Runner.pad_dangling_in_messages(messages, exclude_ids={"excluded"})
        assert len(messages) == 2
        result = messages[1]
        assert result.role == "user"
        ids = [
            block.tool_use_id for block in result.content
            if isinstance(block, ToolResultBlock)
        ]
        assert ids == ["keep"]


class TestSummarizeForHandoff:
    def test_instruction_includes_next_steps(self):
        response = MagicMock()
        response.usage = None
        response.get_text.return_value = "HANDOFF"

        outer = SimpleNamespace(
            _mode="interactive",
            _system_prompt=None,
            client=MagicMock(),
            tool_schemas=[],
            master_session_id="sid",
            _build_system_prompt=lambda: "system",
            _update_usage=MagicMock(),
        )
        outer.client.chat.return_value = response

        runner = Runner(outer)
        runner._prepare_messages_for_llm = lambda **_: []
        out = runner.summarize_for_handoff("下一阶段", "原因")

        assert out == "HANDOFF"
        call = outer.client.chat.call_args
        instruction = call.kwargs["messages"][0].content
        assert "下一阶段" in instruction
        assert "原因" in instruction
        assert call.kwargs["system"] == "system"
        assert call.kwargs["session_id"] == "sid"


class TestMaybeFullCompactForUserInput:
    def _runner(self, total, max_context=10_000):
        outer = MagicMock()
        outer._mode = "interactive"
        outer.get_valid_messages.return_value = [{"role": "user", "content": "x"}]
        outer.messages = []
        outer._get_messages_with_header.return_value = []
        outer._count_messages_tokens.return_value = total
        outer.model.max_context_tokens = max_context
        outer.cache_state = MagicMock()
        runner = Runner(outer)
        runner._perform_full_compact = MagicMock()
        return runner, outer

    def test_below_threshold_no_compact(self):
        runner, _ = self._runner(total=100)
        runner.maybe_full_compact_for_user_input("继续")
        runner._perform_full_compact.assert_not_called()

    def test_above_threshold_uses_input_as_next_steps(self):
        runner, outer = self._runner(total=9_000)
        runner.maybe_full_compact_for_user_input("实现登录")

        runner._perform_full_compact.assert_called_once()
        assert (
            runner._perform_full_compact.call_args.kwargs["next_steps"]
            == "实现登录"
        )
        assert (
            runner._perform_full_compact.call_args.kwargs["reason"]
            == "用户新输入触发的全压缩"
        )
        outer.cache_state.reset_after_compact.assert_called_once()
        outer.cache_state.on_compression.assert_called_once_with(2)


def test_all_roles_have_no_handoff():
    from core.role_config import load_role

    for role in ("master", "worker", "lite"):
        assert "handoff" not in load_role(role).tools


def _endpoint_module():
    from web import routes_chat

    return routes_chat


def _make_session_dir(tmp_path):
    ws_dir = tmp_path / ".cili"
    session_dir = ws_dir / "sessions" / "sid"
    session_dir.mkdir(parents=True)
    (session_dir / "index.json").write_text("{}", encoding="utf-8")
    return ws_dir


class TestForkSessionEndpoint:
    def test_success(self, tmp_path, monkeypatch):
        routes = _endpoint_module()
        ws_dir = _make_session_dir(tmp_path)
        runner = MagicMock()
        runner.fork_session.return_value = {
            "status": "running", "session_id": "new", "name": "分支",
        }

        async def fake_get(_workspace_uuid, _session_id):
            return runner

        monkeypatch.setattr(routes, "_get_or_create_runner", fake_get)
        monkeypatch.setattr(routes, "_claim_session_run", lambda _key: True)
        monkeypatch.setattr(routes, "_release_session_run", lambda _key: None)

        req = routes.ForkSessionRequest(next_steps="继续")
        result = asyncio.run(
            routes.fork_session("ws", "sid", req, ws_dir)
        )

        assert result["session_id"] == "new"
        assert result["name"] == "分支"
        runner.set_turn_interface.assert_any_call("web")
        runner.set_turn_interface.assert_any_call(None)
        runner.fork_session.assert_called_once()
        call = runner.fork_session.call_args
        assert call.kwargs["next_steps"] == "继续"
        assert result.get("mode") == "snapshot"

    def test_missing_session(self, tmp_path):
        routes = _endpoint_module()
        req = routes.ForkSessionRequest()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                routes.fork_session("ws", "sid", req, tmp_path / ".cili")
            )
        assert exc.value.status_code == 404

    def test_running_session(self, tmp_path, monkeypatch):
        routes = _endpoint_module()
        ws_dir = _make_session_dir(tmp_path)
        runner = MagicMock()

        async def fake_get(_workspace_uuid, _session_id):
            return runner

        monkeypatch.setattr(routes, "_get_or_create_runner", fake_get)
        monkeypatch.setattr(routes, "_claim_session_run", lambda _key: False)

        req = routes.ForkSessionRequest()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                routes.fork_session("ws", "sid", req, ws_dir)
            )
        assert exc.value.status_code == 409

    def test_handoff_error(self, tmp_path, monkeypatch):
        routes = _endpoint_module()
        ws_dir = _make_session_dir(tmp_path)
        runner = MagicMock()
        runner.fork_session.return_value = {
            "status": "error", "message": "boom",
        }

        async def fake_get(_workspace_uuid, _session_id):
            return runner

        monkeypatch.setattr(routes, "_get_or_create_runner", fake_get)
        monkeypatch.setattr(routes, "_claim_session_run", lambda _key: True)
        monkeypatch.setattr(routes, "_release_session_run", lambda _key: None)

        req = routes.ForkSessionRequest()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                routes.fork_session("ws", "sid", req, ws_dir)
            )
        assert exc.value.status_code == 500
        assert "boom" in exc.value.detail


class TestRegisterHosted:
    def test_register_and_claim(self, monkeypatch):
        import core.session_registry as reg_mod
        from core.session_registry import SessionRegistry

        fake_teen = MagicMock()
        fake_teen.guard_workspace.return_value = None
        monkeypatch.setattr(reg_mod, "get_teen_mode", lambda: fake_teen)

        reg = SessionRegistry(max_runners=2)
        runner = MagicMock()
        runner.is_running.return_value = False

        key = reg.register_hosted("ws", "sid", runner, claim=True)
        assert key == "ws:sid"
        assert reg.get(key) is runner
        assert not reg.is_idle(key)

        reg.release(key)
        assert reg.is_idle(key)


class TestFullCompactInstruction:
    def test_next_steps_used_in_summary(self, tmp_path):
        from core.base_session_runner import BaseSessionRunner
        from test.conftest import make_dgx_config

        runner = BaseSessionRunner(
            config=make_dgx_config("anthropic"), session_dir=tmp_path
        )
        runner.messages = [
            {"role": "user", "content": "question 1"},
            {"role": "assistant", "content": "answer 1"},
            {"role": "user", "content": "recent question"},
        ]
        captured = []

        def fake_summarize(split_idx=0, instruction=None, **kwargs):
            captured.append(instruction)
            return "摘要"

        with patch.object(
            runner.runner, "_summarize_messages", side_effect=fake_summarize
        ):
            runner._perform_full_compact(
                keep_user_messages=1, next_steps="继续登录"
            )

        assert captured and captured[0]
        assert "继续登录" in captured[0]
        assert "## 下一阶段任务（立即执行）" in captured[0]


class TestMasterInitialSession:
    def test_initial_session_attached(self, tmp_path):
        from core.session import SessionStore
        from core.session_runner import SessionRunner
        from test.conftest import make_dgx_config

        store = SessionStore.create_new_session(tmp_path, "Branch")
        config = make_dgx_config("anthropic")
        with patch("core.session_runner.create_llm_client", return_value=MagicMock()), \
             patch("core.session_runner.create_tools", return_value=[]):
            runner = SessionRunner(
                config,
                role="master",
                cwd=str(tmp_path),
                workspace_uuid="test-workspace",
                initial_session=store,
            )

        assert runner.session is store
        assert runner.current_session_id == store.session_id
