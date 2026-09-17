"""工具批执行器（core/agent_runtime/tool_batch.py）单元测试。

覆盖：
- partition_tool_batches：并发安全工具合批 / 非安全工具单批
- execute_tool_calls：并行保序、parallel=False 顺序、deferred 预激活
- config.system.parallel_tools 开关默认值
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from core.agent_runtime.tool_batch import (
    execute_tool_calls,
    is_concurrency_safe,
    partition_tool_batches,
)
from core.config import SystemConfig
from core.llm.types import ToolCallBlock
from core.tools.base import Tool


class _SafeTool(Tool):
    name = "safe"
    concurrency_safe = True


class _UnsafeTool(Tool):
    name = "unsafe"


def _call(name: str, id_: str) -> ToolCallBlock:
    return ToolCallBlock(id=id_, name=name, arguments='{"x": 1}')


def _tools() -> list:
    return [_SafeTool(cwd="."), _UnsafeTool(cwd=".")]


class TestPartitionToolBatches:
    """分批规则：连续安全合批，非安全各成单批。"""

    def test_all_safe_single_batch(self):
        batches = partition_tool_batches(_tools(), [_call("safe", "a"), _call("safe", "b")])
        assert batches == [["a", "b"]] or len(batches) == 1 and len(batches[0]) == 2

    def test_unsafe_breaks_runs(self):
        calls = [_call("safe", "a"), _call("safe", "b"), _call("unsafe", "c"),
                 _call("safe", "d"), _call("safe", "e")]
        batches = partition_tool_batches(_tools(), calls)
        names = [[tc.id for tc in b] for b in batches]
        assert names == [["a", "b"], ["c"], ["d", "e"]]

    def test_all_unsafe_singleton_batches(self):
        batches = partition_tool_batches(_tools(), [_call("unsafe", "a"), _call("unsafe", "b")])
        assert len(batches) == 2 and all(len(b) == 1 for b in batches)

    def test_unknown_tool_is_unsafe(self):
        assert is_concurrency_safe(_tools(), "nope") is False

    def test_safe_detection(self):
        assert is_concurrency_safe(_tools(), "safe") is True
        assert is_concurrency_safe(_tools(), "unsafe") is False


class TestExecuteToolCalls:
    """批执行：并行保序 / 顺序开关 / deferred 预激活。"""

    @staticmethod
    def _make_agent(tool_names: list, impl) -> MagicMock:
        agent = MagicMock()
        agent.tools = _tools()
        agent._deferred_names = set()
        agent._execute_tool.side_effect = impl
        return agent

    @staticmethod
    def _result(name: str, id_: str) -> dict:
        return {"type": "tool_result", "tool_use_id": id_, "name": name}

    def test_parallel_preserves_order(self):
        agent = self._make_agent(["safe"], lambda name, data, id_: self._result(name, id_))
        calls = [_call("safe", "a"), _call("safe", "b"), _call("safe", "c")]
        results = execute_tool_calls(agent, calls, parallel=True)
        assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]

    def test_parallel_executes_concurrently(self):
        agent = self._make_agent(
            ["safe"],
            lambda name, data, id_: (time.sleep(0.05), self._result(name, id_))[1],
        )
        calls = [_call("safe", "a"), _call("safe", "b"), _call("safe", "c")]
        start = time.perf_counter()
        execute_tool_calls(agent, calls, parallel=True)
        elapsed = time.perf_counter() - start
        # 3 × 50ms 并行应明显快于 150ms 串行
        assert elapsed < 0.12

    def test_sequential_when_parallel_disabled(self):
        agent = self._make_agent(
            ["safe"],
            lambda name, data, id_: (time.sleep(0.05), self._result(name, id_))[1],
        )
        calls = [_call("safe", "a"), _call("safe", "b"), _call("safe", "c")]
        start = time.perf_counter()
        results = execute_tool_calls(agent, calls, parallel=False)
        elapsed = time.perf_counter() - start
        assert elapsed >= 0.14
        assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]

    def test_unsafe_tools_stay_sequential(self):
        """非安全工具与安全工具混批：非安全单批执行，安全合批并行。"""
        calls = [_call("safe", "a"), _call("unsafe", "b"), _call("safe", "c")]
        seen = []

        def impl(name, data, id_):
            seen.append((id_, name))
            return self._result(name, id_)

        results = execute_tool_calls(self._make_agent(["safe", "unsafe"], impl), calls, parallel=True)
        assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]
        # unsafe 单独出现于中间
        assert seen == [("a", "safe"), ("b", "unsafe"), ("c", "safe")]

    def test_deferred_tools_pre_activated(self):
        agent = MagicMock()
        agent.tools = _tools()
        agent._deferred_names = {"safe"}
        agent._execute_tool.side_effect = lambda name, data, id_: self._result(name, id_)
        calls = [_call("safe", "a")]
        execute_tool_calls(agent, calls, parallel=True)
        agent._activate_tools.assert_called_once_with(["safe"])

    def test_empty_input(self):
        agent = self._make_agent(["safe"], lambda name, data, id_: self._result(name, id_))
        assert execute_tool_calls(agent, [], parallel=True) == []


class TestConfigParallelTools:
    """config.system.parallel_tools：默认开启，可显式关闭。"""

    def test_default_on(self):
        assert SystemConfig().parallel_tools is True

    def test_parse_default_on(self):
        assert SystemConfig.from_dict({}).parallel_tools is True

    def test_parse_off(self):
        assert SystemConfig.from_dict({"parallel_tools": False}).parallel_tools is False

    def test_roundtrip(self):
        cfg = SystemConfig.from_dict({"parallel_tools": False})
        assert cfg.to_dict()["parallel_tools"] is False
