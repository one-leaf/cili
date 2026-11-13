"""Tests for core/suggest_service.py — 输入框推荐输入生成（真实 runner + 轻量 fork）。

stub runner 只做只读 duck-typing：client / _prepare_messages_for_llm /
_build_system_prompt / tool_schemas / session.messages。
"""

import json
from types import SimpleNamespace

import pytest

from core.llm.types import (
    LLMResponse,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def _json_response(*items: str) -> LLMResponse:
    text = json.dumps({"suggestions": list(items)}, ensure_ascii=False)
    return LLMResponse(content=[TextBlock(text=text)])


class _StubClient:
    def __init__(self, responses):
        self.model_name = "stub-model"
        self.base_url = "https://stub.example"
        self._responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("unexpected extra chat call")
        return self._responses.pop(0)


class _StubRunner:
    def __init__(self, *, responses, prefix=None, session_messages=None,
                 interface=None, running=False, prepared=None, max_ctx=128000):
        self.client = _StubClient(responses)
        self._last_request_prefix = prefix
        self.session = SimpleNamespace(messages=list(session_messages or []))
        self.master_session_id = "sid"
        self.tool_schemas = [{"name": "bash", "description": "", "input_schema": {}}]
        self.model = SimpleNamespace(max_context_tokens=max_ctx)
        self._turn_interface = interface
        self._running = running
        self.interface_calls = []
        self._prepared = prepared or [
            Message(role="user", content=[TextBlock(text="hi")]),
            Message(role="assistant", content=[TextBlock(text="done")]),
        ]

    def _prepare_messages_for_llm(self, pad_dangling=True):
        return list(self._prepared)

    def _build_system_prompt(self):
        return ["static", "dynamic"]

    def set_turn_interface(self, interface):
        self.interface_calls.append(interface)
        self._turn_interface = interface
        self.tool_schemas = [{"name": "bash" if interface is None else "bash-web"}]


_USER_MSG = [{"role": "user", "content": "hi", "_meta": {}}]

_PREFIX = {
    "system": ["sys-static", "sys-dynamic"],
    "tools": [{"name": "bash", "description": "", "input_schema": {}}],
    "model": "stub-model",
    "base_url": "https://stub.example",
    "session_id": "master-sid",
    "cache_read": 1200,
    "cache_write": 0,
}


class TestRouteSelection:
    """prefixed / fresh 两条来源。"""

    def test_prefixed_reuses_captured_system_and_tools(self):
        from core.suggest_service import generate_suggestions

        runner = _StubRunner(
            responses=[_json_response("继续修复测试", "总结一下改动")],
            prefix=_PREFIX, session_messages=_USER_MSG,
        )
        result = generate_suggestions(runner)

        assert result["route"] == "prefixed"
        assert result["source"] == "context"
        call = runner.client.calls[0]
        assert call["system"] == _PREFIX["system"]
        assert call["tools"] == _PREFIX["tools"]
        assert call["session_id"] == "master-sid"
        # prepared 2 条 + 合成的最后一条（末尾是 assistant，直接追加）
        assert len(call["messages"]) == 3
        assert isinstance(call["messages"][-1], Message)
        assert call["messages"][-1].role == "user"

    def test_fresh_renders_and_restores_interface(self):
        from core.suggest_service import generate_suggestions

        runner = _StubRunner(
            responses=[_json_response("继续修复测试", "总结一下改动")],
            session_messages=_USER_MSG,
        )
        result = generate_suggestions(runner)

        assert result["route"] == "fresh"
        assert runner.interface_calls == ["web", None]
        assert runner._turn_interface is None
        assert runner.client.calls[0]["system"] == ["static", "dynamic"]

    def test_running_runner_not_mutated(self):
        from core.suggest_service import generate_suggestions

        runner = _StubRunner(
            responses=[_json_response("继续修复测试", "总结一下改动")],
            session_messages=_USER_MSG, running=True, interface="web",
        )
        generate_suggestions(runner)
        assert runner.interface_calls == []  # 运行中不改接口
        assert runner._turn_interface == "web"

    def test_model_switch_invalidates_captured_prefix(self):
        from core.suggest_service import generate_suggestions

        prefix = dict(_PREFIX, model="other-model")
        runner = _StubRunner(
            responses=[_json_response("继续修复测试", "总结一下改动")],
            prefix=prefix, session_messages=_USER_MSG,
        )
        result = generate_suggestions(runner)
        assert result["route"] == "fresh"


class TestNoSideEffects:
    """不写会话、不执行工具。"""

    def test_session_messages_untouched(self):
        from core.suggest_service import generate_suggestions

        runner = _StubRunner(
            responses=[_json_response("继续修复测试", "总结一下改动")],
            session_messages=_USER_MSG,
        )
        generate_suggestions(runner)
        assert runner.session.messages == _USER_MSG

    def test_prepared_messages_not_mutated(self):
        from core.suggest_service import generate_suggestions

        prepared = [Message(role="user", content=[TextBlock(text="hi")])]
        runner = _StubRunner(
            responses=[_json_response("继续修复测试", "总结一下改动")],
            session_messages=_USER_MSG, prepared=prepared,
        )
        generate_suggestions(runner)
        # 末尾是 user → 合并进副本的该条；原 Message 与入参列表都不变
        assert len(prepared) == 1
        assert len(prepared[0].content) == 1

    def test_instruction_merged_into_user_tail(self):
        """空会话只有注入的 user 消息：指令合并进去，避免连续同角色。"""
        from core.suggest_service import generate_suggestions

        prepared = [Message(role="user", content=[TextBlock(text="AGENTS.md ...")])]
        runner = _StubRunner(
            responses=[_json_response("帮我了解工作区", "给一些上手建议")],
            prepared=prepared,
        )
        generate_suggestions(runner)
        sent = runner.client.calls[0]["messages"]
        assert len(sent) == 1
        assert sent[0].role == "user"
        assert len(sent[0].content) == 2  # 原文 + 合成指令
        assert "SUGGESTION MODE" in sent[0].content[1].text


class TestEmptySession:
    """空会话：cold_start；失败静态兜底。"""

    def test_cold_start_source(self):
        from core.suggest_service import generate_suggestions

        runner = _StubRunner(responses=[_json_response("帮我了解工作区", "给一些上手建议")])
        result = generate_suggestions(runner)
        assert result["source"] == "cold_start"

    def test_failure_falls_back_to_static(self):
        from core.suggest_service import generate_suggestions

        runner = _StubRunner(responses=[
            LLMResponse(content=[TextBlock(text="not json")]),
            LLMResponse(content=[TextBlock(text="still not json")]),
        ])
        result = generate_suggestions(runner)
        assert result["source"] == "fallback"
        assert result["suggestions"] == [
            "帮我了解这个工作区有什么",
            "根据项目说明给我一些上手建议",
            "列出现有文件并总结",
        ]


class TestParsingAndRetry:
    """解析、tool_use deny、严格提醒重试。"""

    def test_text_json_wins_over_tool_use(self):
        from core.suggest_service import generate_suggestions

        first = LLMResponse(content=[
            ToolCallBlock(id="t1", name="bash", arguments="{}"),
            TextBlock(text=json.dumps(
                {"suggestions": ["继续修复测试", "总结一下改动"]}, ensure_ascii=False)),
        ])
        runner = _StubRunner(responses=[first], session_messages=_USER_MSG)
        generate_suggestions(runner)
        assert len(runner.client.calls) == 1  # 不发起 deny 轮

    def test_tool_use_deny_retry(self):
        from core.suggest_service import generate_suggestions

        first = LLMResponse(content=[
            ToolCallBlock(id="t1", name="bash", arguments="{}"),
            ToolCallBlock(id="t2", name="read", arguments="{}"),
        ])
        runner = _StubRunner(
            responses=[first, _json_response("继续修复测试", "总结一下改动")],
            session_messages=_USER_MSG,
        )
        result = generate_suggestions(runner)

        assert result["suggestions"] == ["继续修复测试", "总结一下改动"]
        retry_messages = runner.client.calls[1]["messages"]
        deny_msg = retry_messages[-1]
        assert deny_msg.role == "user"
        assert [b.tool_use_id for b in deny_msg.content] == ["t1", "t2"]
        assert all(isinstance(b, ToolResultBlock) and b.is_error for b in deny_msg.content)

    def test_second_tool_use_raises(self):
        from core.suggest_service import SuggestError, generate_suggestions

        tool_only = LLMResponse(content=[ToolCallBlock(id="t1", name="bash", arguments="{}")])
        runner = _StubRunner(responses=[tool_only, tool_only], session_messages=_USER_MSG)
        with pytest.raises(SuggestError):
            generate_suggestions(runner)
        assert len(runner.client.calls) == 2  # 硬上限

    def test_bad_json_retries_with_strict_reminder(self):
        from core.suggest_service import generate_suggestions

        runner = _StubRunner(
            responses=[
                LLMResponse(content=[TextBlock(text="好的，我建议你继续")]),
                _json_response("继续修复测试", "总结一下改动"),
            ],
            session_messages=_USER_MSG,
        )
        generate_suggestions(runner)
        retry_messages = runner.client.calls[1]["messages"]
        assert "严格" in retry_messages[-1].content[0].text


class TestOutputFilter:
    """输出后置过滤与条数下限。"""

    def test_filters_bad_items(self):
        from core.suggest_service import SuggestError, generate_suggestions

        runner = _StubRunner(
            responses=[_json_response(
                "这是问句吗？",          # 问句
                "让我来帮你修",          # 助手口吻
                "谢谢",                  # 评价语
                "继续修复测试",          # 与 exclude 重复
                "总结一下改动",          # 唯一有效
            )],
            session_messages=_USER_MSG,
        )
        with pytest.raises(SuggestError):
            generate_suggestions(runner, exclude=["继续修复测试"])

    def test_keeps_valid_items(self):
        from core.suggest_service import generate_suggestions

        runner = _StubRunner(
            responses=[_json_response("继续修复测试", "总结一下改动", "换个方向试试")],
            session_messages=_USER_MSG,
        )
        result = generate_suggestions(runner)
        assert result["suggestions"] == ["继续修复测试", "总结一下改动", "换个方向试试"]


class TestRefresh:
    """换一批：上批写进指令；全被去重时补一轮负例重试。"""

    def test_instruction_lists_previous_batch(self):
        from core.suggest_service import generate_suggestions

        runner = _StubRunner(
            responses=[_json_response("继续修复测试", "总结一下改动", "换个方向试试")],
            session_messages=_USER_MSG,
        )
        generate_suggestions(runner, refresh=True, exclude=["上一批建议甲", "上一批建议乙"])

        instruction = runner.client.calls[0]["messages"][-1].content[-1].text
        assert "上一批建议" in instruction
        assert "上一批建议甲" in instruction

    def test_duplicates_trigger_refresh_retry(self):
        from core.suggest_service import generate_suggestions

        old = ["继续修复测试", "总结一下改动"]
        runner = _StubRunner(
            responses=[
                _json_response(*old),                       # 与上一批完全相同
                _json_response("换个方向试试", "看下测试覆盖"),  # 换方向后
            ],
            session_messages=_USER_MSG,
        )
        result = generate_suggestions(runner, refresh=True, exclude=old)

        assert result["suggestions"] == ["换个方向试试", "看下测试覆盖"]
        assert len(runner.client.calls) == 2
        retry_messages = runner.client.calls[1]["messages"]
        assert retry_messages[-1].role == "user"
        assert "不同" in retry_messages[-1].content[0].text
        assert "继续修复测试" in retry_messages[-1].content[0].text  # 负例

    def test_still_duplicate_raises(self):
        from core.suggest_service import SuggestError, generate_suggestions

        old = ["继续修复测试", "总结一下改动"]
        runner = _StubRunner(
            responses=[_json_response(*old), _json_response(*old)],
            session_messages=_USER_MSG,
        )
        with pytest.raises(SuggestError):
            generate_suggestions(runner, refresh=True, exclude=old)
        assert len(runner.client.calls) == 2  # 刷新重试硬上限一轮


class TestRealRunnerIntegration:
    """真实 SessionRunner 装配（不联网，替换 client）。"""

    def test_empty_session_uses_runner_assembly(self, runner):
        from core.suggest_service import generate_suggestions

        calls = []

        class _Client:
            model_name = "stub-model"
            base_url = "https://stub.example"

            def chat(self, **kwargs):
                calls.append(kwargs)
                text = json.dumps(
                    {"suggestions": ["帮我了解工作区", "给一些上手建议"]},
                    ensure_ascii=False,
                )
                return LLMResponse(content=[TextBlock(text=text)])

        runner.client = _Client()
        before = list(runner.session.messages)

        result = generate_suggestions(runner)

        assert result["source"] == "cold_start"
        assert result["route"] == "fresh"
        assert len(calls) == 1
        assert calls[0]["session_id"] == runner.master_session_id
        assert isinstance(calls[0]["system"], list)
        assert isinstance(calls[0]["tools"], list)
        # 不写会话：消息数与内容不变
        assert runner.session.messages == before

    def test_tool_schemas_sorted(self, runner):
        """tools 顺序确定（缓存前缀字节一致性）。"""
        runner._apply_tool_visibility()
        names = [s["name"] for s in runner.tool_schemas]
        assert names == sorted(names)

    def test_capture_request_prefix(self, runner):
        """LLM 调用后记录 system/tools/model/base_url/session_id 与缓存统计。"""
        from core.llm.types import UsageData

        response = LLMResponse(
            content=[TextBlock(text="ok")],
            usage=UsageData(input_tokens=10, cache_read_tokens=5, cache_write_tokens=7),
        )
        runner.runner._capture_request_prefix(["sys"], response)

        prefix = runner._last_request_prefix
        assert prefix["system"] == ["sys"]
        assert prefix["tools"] == runner.tool_schemas
        assert prefix["model"] == runner.client.model_name
        assert prefix["base_url"] == runner.client.base_url
        assert prefix["session_id"] == runner.master_session_id
        assert prefix["cache_read"] == 5
        assert prefix["cache_write"] == 7


class TestMcpSorting:
    """MCP 工具顺序确定。"""

    def test_build_wrappers_sorted_by_name(self):
        from core.config import MCPConfig
        from core.tools.mcp import MCPProvider

        defs = [
            SimpleNamespace(name="zeta", inputSchema={"type": "object", "properties": {}},
                            description="z"),
            SimpleNamespace(name="alpha", inputSchema={"type": "object", "properties": {}},
                            description="a"),
        ]
        provider = MCPProvider()
        wrappers = provider._build_wrappers("srv", MCPConfig(enabled_tools=["*"]), defs)
        names = [w.name for w in wrappers]
        assert names == sorted(names)


class TestContextGuard:
    """fresh 路径的超长保护。"""

    def test_dangling_tool_use_rejected(self):
        """fresh 路径存在未配对 tool_use（崩溃残留）→ 失败提示，不发送请求。"""
        from core.suggest_service import SuggestError, generate_suggestions

        prepared = [
            Message(role="assistant", content=[
                ToolCallBlock(id="t1", name="bash", arguments="{}"),
            ]),
        ]
        runner = _StubRunner(
            responses=[], session_messages=_USER_MSG, prepared=prepared,
        )
        with pytest.raises(SuggestError):
            generate_suggestions(runner)
        assert runner.client.calls == []

    def test_oversized_context_rejected(self):
        from core.suggest_service import SuggestError, generate_suggestions

        huge = [Message(role="user", content=[TextBlock(text="x" * 5000)])]
        runner = _StubRunner(
            responses=[],
            session_messages=_USER_MSG, prepared=huge, max_ctx=1000,
        )
        with pytest.raises(SuggestError):
            generate_suggestions(runner)
        assert runner.client.calls == []
