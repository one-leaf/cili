"""SIWC 客户端接线测试：转流式、401 刷新、错误分类。"""

import httpx
import pytest

from core.config import ResolvedModel
from core.llm.client import create_llm_client
from core.llm.errors import LLMStreamFailedError, classify_llm_error


def _oauth_config(**overrides) -> ResolvedModel:
    values = dict(
        name="gpt-test",
        protocol="openai-responses",
        auth_type="oauth",
        credential="chatgpt-oauth:0123456789abcdef",
        base_url="https://api.openai.com/v1",
    )
    values.update(overrides)
    return ResolvedModel(**values)


def test_requires_streaming_only_for_oauth():
    client = create_llm_client(_oauth_config())
    assert client.adapter.requires_streaming is True
    client.close()

    plain = create_llm_client(_oauth_config(auth_type="auto", credential=""))
    assert plain.adapter.requires_streaming is False
    plain.close()


def test_create_client_locks_oauth_endpoint():
    with pytest.raises(ValueError):
        create_llm_client(_oauth_config(protocol="anthropic"))
    with pytest.raises(ValueError):
        create_llm_client(_oauth_config(base_url="https://evil.example.com"))


def test_chat_delegates_to_stream_for_oauth(monkeypatch):
    client = create_llm_client(_oauth_config())
    called = {}

    def fake_stream(**kwargs):
        called.update(kwargs)
        return "streamed"

    monkeypatch.setattr(client, "chat_stream", fake_stream)
    assert client.chat(messages=[]) == "streamed"
    assert called["max_retries"] == 2
    client.close()


def test_stream_401_forces_refresh_and_retries(monkeypatch):
    client = create_llm_client(_oauth_config())
    calls = {"n": 0, "force": []}

    def fake_token(credential, force_refresh=False):
        calls["force"].append(force_refresh)
        return "tok-refresh" if force_refresh else "tok-initial"

    monkeypatch.setattr("core.llm.chatgpt_oauth.get_access_token", fake_token)

    def fake_stream(url, headers, body, stop_check=None, timeout=None):
        calls["n"] += 1
        assert headers["Authorization"].startswith("Bearer tok")
        if calls["n"] == 1:
            request = httpx.Request("POST", url)
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("401", request=request, response=response)
        return iter([{
            "type": "response.completed",
            "response": {"status": "completed", "output": []},
        }])

    monkeypatch.setattr(client.transport, "stream", fake_stream)
    client.chat(messages=[])
    assert calls["n"] == 2
    assert calls["force"] == [False, True]
    client.close()


def test_classify_siwc_errors():
    info = classify_llm_error(LLMStreamFailedError(code="subscription_sharing_usage_unavailable"))
    assert info.should_retry is True

    info = classify_llm_error(LLMStreamFailedError(code="subscription_sharing_usage_limit_exceeded"))
    assert info.kind == "quota"
    assert info.should_retry is False

    info = classify_llm_error(LLMStreamFailedError(code="chatpass_v2_probe_failed"))
    assert info.kind == "auth"
    assert info.should_retry is False


def test_classify_siwc_manager_exceptions():
    class FakeAuthRequired(RuntimeError):
        siwc_kind = "auth"

    class FakeTransient(RuntimeError):
        siwc_kind = "transient"

    assert classify_llm_error(FakeAuthRequired("请登录")).kind == "auth"
    assert classify_llm_error(FakeTransient("稍后重试")).should_retry is True
