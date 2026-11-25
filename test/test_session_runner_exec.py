"""Step 3 契约测试：TurnExecutor 执行层抽取后的转发与语义。

覆盖：_call_llm 转发 → run_round（压缩先于 LLM 调用）、
静态转发与 TurnExecutor 同源、_prepare_messages_for_llm 参数透传、
resolve/strip 经 agent 与经 runner 产物一致、_execute_tool 经转发正常。
"""

from unittest.mock import patch

from core.base_session_runner import BaseSessionRunner
from core.session_runner_runtime.executor import TurnExecutor
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
        self.model = "fake-model"  # assistant 消息 _meta.model 与 usage 分桶需要

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
        assert list(BaseSessionRunner.iter_content_blocks(messages)) == list(TurnExecutor.iter_content_blocks(messages))

    def test_safe_output_filename(self):
        assert BaseSessionRunner._safe_output_filename("toolu_01ab") == TurnExecutor._safe_output_filename("toolu_01ab")
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


class TestContextPressure:
    """回合中途压力：100% 收尾提示 / 150% 强制全压缩。"""

    def test_warn_at_100_percent_once(self, runner, monkeypatch):
        monkeypatch.setattr(runner.model, "max_context_tokens", 1000)
        # 英文约 4 字符/token → 4800 字符 ≈ 1200 tokens（120%）
        runner.add_message("user", "x" * 4800)

        with patch.object(runner.runner, "_perform_full_compact") as compact:
            runner._check_and_compress()
            runner._check_and_compress()

        compact.assert_not_called()
        warnings = [
            m for m in runner.messages
            if (m.get("_meta") or {}).get("context_warning")
        ]
        assert len(warnings) == 1
        assert "尽快收尾" in warnings[0]["content"]
        assert "120%" in warnings[0]["content"]

    def test_force_compact_at_150_percent(self, runner, monkeypatch):
        monkeypatch.setattr(runner.model, "max_context_tokens", 1000)
        runner.add_message("user", "旧任务")
        runner.add_message("assistant", "旧答复")
        last_input = "x" * 7000  # ≈ 1750 tokens（175%）
        runner.add_message("user", last_input)

        with patch.object(
            runner.runner, "_perform_full_compact", return_value=(100, 50)
        ) as compact:
            runner._check_and_compress()

        compact.assert_called_once()
        kwargs = compact.call_args.kwargs
        # 以当前轮初始输入为切分点：只压缩它之前的 2 条消息
        assert kwargs["split_idx"] == 2
        assert kwargs["insert_before"] == 2
        assert "前 2 条消息" in kwargs["instruction"]
        assert last_input in kwargs["instruction"]
        assert "150%" in kwargs["reason"]
        # 150% 分支直接压缩，不再追加 100% 收尾提示
        assert not any(
            (m.get("_meta") or {}).get("context_warning")
            for m in runner.messages
        )

    def test_force_compact_keeps_current_round(self, runner, monkeypatch):
        """150% 只压缩当前轮之前；本轮初始输入与中途工具结果保留，不重跑。"""
        monkeypatch.setattr(runner.model, "max_context_tokens", 1000)
        runner.add_message("user", "旧任务")
        runner.add_message("assistant", "旧答复")
        current = "x" * 4000  # 本轮初始用户输入
        runner.add_message("user", current)
        runner.add_message("assistant", [{
            "type": "tool_use", "id": "t1", "name": "bash", "input": {},
        }])
        runner.add_message("user", [{
            "type": "tool_result", "tool_use_id": "t1", "content": "y" * 4000,
        }])

        with patch.object(
            runner.runner, "_perform_full_compact", return_value=(100, 50)
        ) as compact:
            runner._check_and_compress()

        kwargs = compact.call_args.kwargs
        assert kwargs["split_idx"] == 2
        assert kwargs["insert_before"] == 2
        assert current in kwargs["instruction"]
        # 当前轮中途工具结果仍在有效上下文里，没有被压力处理剔除
        assert any(
            isinstance(m.get("content"), list)
            and any(b.get("type") == "tool_result" for b in m["content"])
            for m in runner.get_valid_messages()
        )

    def test_last_user_input_skips_tool_results_and_synthetic(self, runner):
        runner.add_message("user", "真实输入")
        runner.add_message("user", [{
            "type": "tool_result", "tool_use_id": "t1", "content": "工具结果",
        }])
        runner.add_message("user", "循环警告", meta={"loop_warning": True})
        assert runner.runner._last_user_input_text() == "真实输入"

    def test_force_compact_failure_falls_back_to_warning(self, runner, monkeypatch):
        """150% 压缩未生效（消息不足/摘要失败）时，至少注入 100% 收尾提示。"""
        monkeypatch.setattr(runner.model, "max_context_tokens", 1000)
        runner.add_message("user", "旧任务")
        runner.add_message("assistant", "旧答复")
        runner.add_message("user", "x" * 7000)

        with patch.object(
            runner.runner, "_perform_full_compact", return_value=(100, 100)
        ) as compact:
            runner._check_and_compress()

        compact.assert_called_once()
        warnings = [
            m for m in runner.messages
            if (m.get("_meta") or {}).get("context_warning")
        ]
        assert len(warnings) == 1
