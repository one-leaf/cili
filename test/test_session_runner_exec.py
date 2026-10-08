"""Step 3 契约测试：Runner 执行层抽取后的转发与语义。

覆盖：_call_llm 转发 → run_round（压缩先于 LLM 调用）、
静态转发与 Runner 同源、_prepare_messages_for_llm 参数透传、
resolve/strip 经 agent 与经 runner 产物一致、_execute_tool 经转发正常。
"""

from unittest.mock import patch

from core.session_runner_runtime.runner import Runner
from core.base_session_runner import BaseSessionRunner
from core.tools.base import ToolResult


class _FakeTool:
    name = "fake"

    def __init__(self):
        self.output_file = None
        self.on_output = None

    def coerce_input(self, data):
        return data

    def execute(self, **kw):
        return ToolResult("ok")

    def save_output_to_file(self, result):
        pass


class TestCallLlmForwardsToRunRound:
    def test_compress_before_llm_call(self, runner):
        """_call_llm 转发 → run_round：压缩先于 LLM 调用，参数透传。"""
        order = []

        def fake_call(**kw):
            order.append("call")
            return kw

        with patch.object(runner.runner, "_check_and_compress",
                          side_effect=lambda: order.append("compress")), \
             patch.object(runner.runner, "_call_llm", side_effect=fake_call):
            resp = runner._call_llm(streaming=True, system_prompt="sp")
        assert order == ["compress", "call"]
        assert resp == {"streaming": True, "system_prompt": "sp"}

    def test_loop_no_longer_calls_compress_explicitly(self, runner):
        """Loop 骨架不再显式调用 _check_and_compress（压缩收敛在 run_round）。

        若有人在 loop 里重新加回 self._check_and_compress()，本测试即失败。
        """
        with patch.object(runner, "_check_and_compress") as compress_spy, \
             patch.object(runner, "_call_llm", return_value=_FakeLLMResponse()) as call_mock:
            runner.loop.run_interactive()
        assert compress_spy.call_count == 0
        assert call_mock.call_count == 1


class _FakeLLMResponse:
    def __init__(self):
        self.usage = None

    def get_tool_calls(self):
        return []

    def content_as_dicts(self):
        return []


class TestStaticForwardsEquivalent:
    def test_iter_content_blocks(self):
        messages = [
            {"role": "user", "content": "text"},
            {"role": "assistant", "content": [{"type": "tool_use", "name": "bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "out"}]},
        ]
        assert list(BaseSessionRunner.iter_content_blocks(messages)) == list(Runner.iter_content_blocks(messages))

    def test_safe_output_filename(self):
        assert BaseSessionRunner._safe_output_filename("toolu_01ab") == Runner._safe_output_filename("toolu_01ab")
        assert BaseSessionRunner._safe_output_filename("") == ""
        assert BaseSessionRunner._safe_output_filename("../evil") != "../evil"

    def test_is_413_error(self):
        assert BaseSessionRunner._is_413_error(Exception("413 Request Entity Too Large")) is True
        assert BaseSessionRunner._is_413_error(Exception("other")) is False

    def test_find_split_equivalent(self):
        messages = [
            {"role": "user", "content": "q1", "_meta": {}},
            {"role": "assistant", "content": "a1", "_meta": {}},
            {"role": "user", "content": "q2", "_meta": {}},
            {"role": "assistant", "content": "a2", "_meta": {}},
        ]
        assert BaseSessionRunner._find_split_by_user_messages(messages, 1) == 2


class TestForwardsPassthrough:
    def test_prepare_messages_forwards_kwargs(self, runner):
        with patch.object(runner.runner, "_prepare_messages_for_llm", return_value=[]) as m:
            runner._prepare_messages_for_llm(pad_dangling=False, resolve_results=False)
        m.assert_called_once_with(pad_dangling=False, resolve_results=False)

    def test_execute_tool_forwards_to_runner(self, runner):
        tool = _FakeTool()
        runner.tools.append(tool)
        result = runner._execute_tool("fake", {}, "tu_1")
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "tu_1"
        assert result["content"] == "ok"
        assert result["is_error"] is False

    def test_resolve_tool_results_agent_matches_runner(self, runner, tmp_path):
        """resolve 经 agent 转发与经 runner 直调产物一致（行为中立）。"""
        runner.session_dir = tmp_path
        (tmp_path / "tu1.txt").write_text("data", encoding="utf-8")
        block = {
            "type": "tool_result",
            "tool_use_id": "tu1",
            "content": None,
            "_meta": {"output_path": "tu1.txt", "file_size": 4, "truncated": False},
        }
        m1 = [{"role": "user", "content": [dict(block)]}]
        m2 = [{"role": "user", "content": [dict(block)]}]
        runner._resolve_tool_results(m1)
        runner.runner._resolve_tool_results(m2)
        assert m1 == m2
        assert m1[0]["content"][0]["content"] == "data"
