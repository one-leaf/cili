"""SIWC 请求整形测试。"""

from core.llm.siwc import const
from core.llm.siwc.catalog import parse_models_payload
from core.llm.siwc.profile import shape_request


def test_shape_request_removes_unsupported_fields():
    body = {
        "model": "m",
        "input": [{"role": "system", "content": []}, {"role": "user", "content": []}],
        "max_output_tokens": 10,
        "temperature": 0.5,
        "prompt_cache_retention": "x",
        "prompt_cache_options": {},
        "store": True,
        "tool_choice": {"type": "function"},
        "tools": [
            {"type": "function", "name": "f", "strict": False},
            {"type": "web_search"},
        ],
    }
    out = shape_request(body)
    assert "max_output_tokens" not in out
    assert "temperature" not in out
    assert "prompt_cache_retention" not in out
    assert "prompt_cache_options" not in out
    assert out["store"] is False
    assert out["stream"] is True
    assert out["input"][0]["role"] == "developer"
    assert out["tools"] == [{"type": "function", "name": "f", "strict": False}]
    assert out["tool_choice"] == "auto"


def test_shape_request_removes_tools_when_none_left():
    body = {"model": "m", "tools": [{"type": "web_search"}], "tool_choice": "auto"}
    out = shape_request(body)
    assert "tools" not in out
    assert "tool_choice" not in out


def test_shape_request_switches(monkeypatch):
    monkeypatch.setattr(const, "KEEP_PROMPT_CACHE_KEY", False)
    monkeypatch.setattr(const, "SEND_REASONING", False)
    monkeypatch.setattr(const, "SEND_INCLUDE_ENCRYPTED", False)
    body = {
        "model": "m",
        "prompt_cache_key": "k",
        "reasoning": {"effort": "high"},
        "include": ["reasoning.encrypted_content"],
    }
    out = shape_request(body)
    assert "prompt_cache_key" not in out
    assert "reasoning" not in out
    assert "include" not in out


def test_parse_models_standard_shape():
    data = {"object": "list", "data": [{"id": "gpt-a"}, {"id": "gpt-b"}, {"id": "gpt-a"}]}
    models = parse_models_payload(data)
    assert [m["id"] for m in models] == ["gpt-a", "gpt-b"]


def test_parse_models_chatgpt_catalog_shape():
    data = {
        "models": [
            {"slug": "gpt-a", "visibility": "list", "display_name": "A"},
            {"slug": "hidden", "visibility": "hide"},
            {"slug": ""},
        ]
    }
    models = parse_models_payload(data)
    assert [m["id"] for m in models] == ["gpt-a"]
    assert models[0]["display_name"] == "A"


def test_parse_models_invalid_returns_empty():
    assert parse_models_payload({}) == []
    assert parse_models_payload({"data": "nope"}) == []
    assert parse_models_payload(None) == []


def test_oauth_serialize_keeps_images():
    """SIWC 整形不得误删图片输入（真机验证前的本地保证）。"""
    from core.config import ModelConfig
    from core.llm.openai_responses import OpenAIResponsesAdapter
    from core.llm.types import ImageBlock, Message, TextBlock

    cfg = ModelConfig(
        name="m",
        interface_type="openai-responses",
        auth_type="oauth",
        credential="siwc:0123456789abcdef",
        base_url="https://api.openai.com/v1",
    )
    adapter = OpenAIResponsesAdapter(cfg)
    message = Message(
        role="user",
        content=[TextBlock(text="看图"), ImageBlock(data="AAA", mime_type="image/png")],
    )
    body = adapter.serialize([message], system="", tools=None, model="m", max_tokens=100)
    parts = [part for item in body["input"] for part in (item.get("content") or [])]
    assert any(part.get("type") == "input_image" for part in parts)
    assert "max_output_tokens" not in body
    assert body["store"] is False
    assert body["stream"] is True
