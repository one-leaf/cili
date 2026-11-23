"""DeepSeek 服务端联网工具（客户端包装）测试。"""

from __future__ import annotations

import types

import pytest

from core.config import ModelConfig
from core.llm import LLMResponse, ServerBlock, TextBlock
from core.llm.anthropic import AnthropicAdapter
from core.llm.deepseek_capabilities import (
    is_deepseek_model,
    is_unsupported,
    mark_unsupported,
    reset_unsupported_cache,
    tools_visible,
)
from core.tools import deepseek_web as dw
from core.tools.deepseek_web import DeepSeekReadUrlTool, DeepSeekWebSearchTool


def _model(
    name: str = "va/deepseek-v4.1-flash",
    interface_type: str = "anthropic",
    backend: str = "litellm",
    base_url: str = "http://localhost:4000",
) -> ModelConfig:
    return ModelConfig(
        name=name,
        interface_type=interface_type,
        api_key="test-key",
        base_url=base_url,
        backend=backend,
    )


class _FakeClient:
    """记录请求参数的假 LLMClient。"""

    def __init__(self, response: LLMResponse | None = None, exc: BaseException | None = None):
        self.response = response
        self.exc = exc
        self.calls: list[dict] = []
        self.model_config: ModelConfig | None = None
        self.closed = False

    def chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        assert self.response is not None
        return self.response

    def close(self) -> None:
        self.closed = True


class _HTTPLikeError(Exception):
    """模拟 httpx.HTTPStatusError：错误文本在 response.text 里。"""

    def __init__(self, text: str):
        super().__init__("HTTP 400")
        self.response = types.SimpleNamespace(text=text)


@pytest.fixture(autouse=True)
def _clean_capability_cache():
    reset_unsupported_cache()
    yield
    reset_unsupported_cache()


@pytest.fixture
def install_client(monkeypatch):
    """安装假 client 工厂，返回 (client, factory)。"""

    def _install(response=None, exc=None):
        client = _FakeClient(response, exc)

        def factory(config):
            client.model_config = config
            return client

        monkeypatch.setattr(dw, "create_llm_client", factory)
        return client

    return _install


def _search_response(*items: dict, text: str = "summary") -> LLMResponse:
    return LLMResponse(
        content=[
            ServerBlock(raw={
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": list(items),
            }),
            TextBlock(text=text),
        ]
    )


# ========== 能力判定 ==========

class TestCapabilities:
    def test_is_deepseek_model_by_name(self):
        assert is_deepseek_model(_model(name="va/deepseek-v4.1-flash"))

    def test_is_deepseek_model_by_backend(self):
        assert is_deepseek_model(_model(name="custom-alias", backend="deepseek"))

    def test_is_deepseek_model_by_host(self):
        assert is_deepseek_model(
            _model(name="whatever", backend="custom", base_url="https://api.deepseek.com/anthropic")
        )

    def test_not_deepseek(self):
        assert not is_deepseek_model(_model(name="va/glm-5.3-flash"))

    def test_tools_visible_requires_anthropic(self):
        assert not tools_visible(_model(interface_type="openai"))
        assert tools_visible(_model())

    def test_mark_unsupported_disables_model(self):
        model = _model()
        assert tools_visible(model)
        mark_unsupported(model.name)
        assert is_unsupported(model.name)
        assert not tools_visible(model)
        # 其他模型不受影响
        assert tools_visible(_model(name="va/deepseek-v4.1-flash-2"))


# ========== 搜索工具 ==========

class TestWebSearchTool:
    def test_formats_results_and_untrusted_wrapper(self, install_client):
        client = install_client(_search_response(
            {"type": "web_search_result", "title": "A", "url": "https://a.example", "snippet": "sa"},
            {"type": "web_search_result", "title": "B", "url": "https://b.example", "snippet": "sb"},
        ))
        tool = DeepSeekWebSearchTool(model_provider=lambda: _model())

        result = tool.execute(query="hello")

        assert not result.error
        assert "Search results for: hello" in result.output
        assert "https://a.example" in result.output
        assert "sb" in result.output
        assert "Summary:\nsummary" in result.output
        assert dw.UNTRUSTED_DATA_BEGIN in result.output and dw.UNTRUSTED_DATA_END in result.output

        call = client.calls[0]
        assert call["tools"] == [{"type": "deepseek_web_search", "name": "deepseek_web_search"}]
        assert call["max_tokens"] == tool.max_output_tokens
        # 辅助调用固定关闭思考
        assert client.model_config is not None and client.model_config.reasoning == "off"
        assert client.closed

    def test_dedupes_urls_and_caps_snippet(self, install_client):
        install_client(_search_response(
            {"type": "web_search_result", "title": "A", "url": "https://a.example", "snippet": "x" * 600},
            {"type": "web_search_result", "title": "A2", "url": "https://a.example", "snippet": "dup"},
        ))
        tool = DeepSeekWebSearchTool(model_provider=lambda: _model())

        result = tool.execute(query="q")

        assert result.output.count("https://a.example") == 1
        assert "x" * 600 not in result.output
        assert "..." in result.output

    def test_text_only_response_is_returned(self, install_client):
        # 模型判断无需搜索：没有结果块，但仍有文本
        install_client(LLMResponse(content=[TextBlock(text="no search needed")]))
        tool = DeepSeekWebSearchTool(model_provider=lambda: _model())

        result = tool.execute(query="q")

        assert not result.error
        assert "No search result links were returned" in result.output
        assert "no search needed" in result.output

    def test_empty_response_is_error(self, install_client):
        install_client(LLMResponse(content=[]))
        tool = DeepSeekWebSearchTool(model_provider=lambda: _model())

        result = tool.execute(query="q")

        assert result.error

    def test_error_object_in_content(self, install_client):
        install_client(LLMResponse(content=[
            ServerBlock(raw={
                "type": "web_search_tool_result",
                "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"},
            }),
        ]))
        tool = DeepSeekWebSearchTool(model_provider=lambda: _model())

        result = tool.execute(query="q")

        assert result.error
        assert "unavailable" in result.output

    def test_requires_query(self):
        tool = DeepSeekWebSearchTool(model_provider=lambda: _model())
        assert tool.execute(query="  ").error

    def test_non_deepseek_model_hidden(self):
        tool = DeepSeekWebSearchTool(model_provider=lambda: _model(name="va/glm-5.3-flash"))
        result = tool.execute(query="q")
        assert result.error
        assert "not available" in result.output

    def test_capability_missing_marks_model_and_calls_handler(self, install_client):
        handler_calls = []
        install_client(exc=_HTTPLikeError(
            '{"error":{"message":"The parameter tools[i].type ... unsupported server tool deepseek_web_search"}}'
        ))
        model = _model()
        tool = DeepSeekWebSearchTool(
            model_provider=lambda: model,
            capability_missing_handler=lambda: handler_calls.append(1),
        )

        result = tool.execute(query="q")

        assert result.error
        assert model.name in result.output
        assert is_unsupported(model.name)
        assert handler_calls == [1]

    def test_access_denied_also_marks_capability_missing(self, install_client):
        install_client(exc=_HTTPLikeError("Access denied for web search. Please go to ..."))
        model = _model(name="va/deepseek-v4-flash")
        tool = DeepSeekWebSearchTool(model_provider=lambda: model)

        result = tool.execute(query="q")

        assert result.error
        assert is_unsupported(model.name)

    def test_generic_error_does_not_mark_capability(self, install_client):
        install_client(exc=_HTTPLikeError("network unreachable"))
        model = _model()
        tool = DeepSeekWebSearchTool(model_provider=lambda: model)

        result = tool.execute(query="q")

        assert result.error
        assert not is_unsupported(model.name)


# ========== 读链接工具 ==========

class TestReadUrlTool:
    def test_formats_fetch_result(self, install_client):
        client = install_client(LLMResponse(content=[
            ServerBlock(raw={
                "type": "web_fetch_tool_result",
                "tool_use_id": "srvtoolu_rl",
                "kind": "read_link",
                "signature": "sig",
                "content": {
                    "type": "web_fetch_result",
                    "url": "https://example.com",
                    "content": {"type": "document", "title": "Example Domain", "source": {"data": ""}},
                    "published_at": None,
                },
            }),
            TextBlock(text="This is an example page."),
        ]))
        tool = DeepSeekReadUrlTool(model_provider=lambda: _model())

        result = tool.execute(url="https://example.com")

        assert not result.error
        assert "URL: https://example.com" in result.output
        assert "Title: Example Domain" in result.output
        assert "This is an example page." in result.output
        call = client.calls[0]
        assert call["tools"] == [{"type": "deepseek_read_url", "name": "deepseek_read_url"}]
        # 用户消息显式点名工具，避免模型按既有知识直接作答
        assert "deepseek_read_url" in call["messages"][0].content

    def test_invalid_url_rejected(self):
        tool = DeepSeekReadUrlTool(model_provider=lambda: _model())
        assert tool.execute(url="not-a-url").error
        assert tool.execute(url="ftp://example.com/x").error

    def test_no_content_is_error(self, install_client):
        install_client(LLMResponse(content=[]))
        tool = DeepSeekReadUrlTool(model_provider=lambda: _model())

        result = tool.execute(url="https://example.com")

        assert result.error


# ========== Adapter 解析 ==========

class TestServerBlockParsing:
    @pytest.fixture
    def adapter(self):
        return AnthropicAdapter(_model())

    def test_parse_response_server_blocks(self, adapter):
        blocks, stop, _ = adapter.parse_response({
            "content": [
                {"type": "thinking", "thinking": "t", "signature": "s"},
                {"type": "server_tool_use", "id": "s1", "name": "deepseek_web_search"},
                {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
                {"type": "web_fetch_tool_result", "tool_use_id": "s2", "content": {}, "signature": "sig"},
                {"type": "text", "text": "done"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        })
        assert [type(b).__name__ for b in blocks] == [
            "ReasoningBlock", "ServerBlock", "ServerBlock", "ServerBlock", "TextBlock",
        ]
        assert blocks[1].raw["name"] == "deepseek_web_search"
        assert blocks[3].raw["signature"] == "sig"
        assert stop == "end_turn"

    def test_server_block_roundtrip(self):
        from core.llm.types import block_from_dict, block_to_dict

        raw = {"type": "web_fetch_tool_result", "tool_use_id": "x", "signature": "sig",
               "content": {"type": "web_fetch_result", "url": "https://e"}}
        assert block_to_dict(block_from_dict(raw)) == raw

    def test_server_tool_schema_not_cache_controlled(self, adapter):
        """服务端工具声明不贴 cache_control（否则端点可能静默忽略该工具）。"""
        from core.llm import Message

        tools = [{"type": "deepseek_web_search", "name": "deepseek_web_search"}]
        body = adapter.serialize(
            messages=[Message(role="user", content="q")],
            system="",
            tools=tools,
            model="va/deepseek-v4.1-flash",
            max_tokens=100,
        )
        assert "cache_control" not in body["tools"][0]

    def test_normal_tool_still_cache_controlled_when_mixed(self, adapter):
        from core.llm import Message

        tools = [
            {"type": "deepseek_web_search", "name": "deepseek_web_search"},
            {"name": "read", "description": "Read", "input_schema": {"type": "object", "properties": {}}},
        ]
        body = adapter.serialize(
            messages=[Message(role="user", content="q")],
            system="",
            tools=tools,
            model="va/deepseek-v4.1-flash",
            max_tokens=100,
        )
        assert "cache_control" not in body["tools"][0]
        assert body["tools"][1]["cache_control"] == {"type": "ephemeral"}


# ========== Runner 可见性 ==========

class TestRunnerVisibility:
    def _runner(self, model):
        from core.session_runner import SessionRunner

        runner = object.__new__(SessionRunner)
        runner.tools = [
            DeepSeekWebSearchTool(),
            DeepSeekReadUrlTool(),
            types.SimpleNamespace(name="read"),
        ]
        runner.model = model
        runner._turn_interface = "web"
        return runner

    def test_deepseek_model_exposes_tools(self):
        runner = self._runner(_model())
        names = [t.name for t in runner._eligible_tools()]
        assert "deepseek_web_search" in names
        assert "deepseek_read_url" in names

    def test_non_deepseek_model_hides_tools(self):
        runner = self._runner(_model(name="va/glm-5.3-flash"))
        names = [t.name for t in runner._eligible_tools()]
        assert "deepseek_web_search" not in names
        assert "deepseek_read_url" not in names

    def test_unsupported_model_hides_tools(self):
        model = _model(name="va/deepseek-v4-flash")
        mark_unsupported(model.name)
        runner = self._runner(model)
        names = [t.name for t in runner._eligible_tools()]
        assert "deepseek_web_search" not in names
