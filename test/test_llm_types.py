"""LLM 类型与响应解析测试。"""



from core.llm import (
    LLMResponse,
    TextBlock,
    ToolCallBlock,
    UsageData,
)


class TestLLMResponse:
    def test_default_fields(self):
        resp = LLMResponse(content=[])
        assert resp.content == []
        assert resp.stop_reason == ""
        assert resp.usage == UsageData()
        assert resp.headers == {}

    def test_with_values(self):
        blocks = [TextBlock(text="hello")]
        usage = UsageData(input_tokens=10, output_tokens=20)
        resp = LLMResponse(
            content=blocks,
            stop_reason="end_turn",
            usage=usage,
            headers={"x-test": "1"},
        )
        assert resp.content == blocks
        assert resp.stop_reason == "end_turn"
        assert resp.usage.input_tokens == 10
        assert resp.headers["x-test"] == "1"

    def test_get_text(self):
        resp = LLMResponse(content=[TextBlock(text="hello"), TextBlock(text=" world")])
        assert resp.get_text() == "hello world"

    def test_get_tool_calls(self):
        tc = ToolCallBlock(id="t1", name="read", arguments='{"path": "test.py"}')
        resp = LLMResponse(content=[TextBlock(text="hi"), tc])
        tool_calls = resp.get_tool_calls()
        assert len(tool_calls) == 1
        assert tool_calls[0].name == "read"


# ========== Retry Logic ==========
