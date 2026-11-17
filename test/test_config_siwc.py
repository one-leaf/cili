"""OAuth provider 的配置模型、启动豁免与端点锁定测试。"""

from core.config import Config, ModelConfig, ProviderConfig


def test_credential_roundtrip():
    provider = ProviderConfig.from_dict({"auth_type": "oauth", "credential": "siwc:abc"})
    assert provider.credential == "siwc:abc"
    assert ProviderConfig.from_dict(provider.to_dict()).credential == "siwc:abc"

    model = ModelConfig(name="m", credential="siwc:def")
    assert ModelConfig.from_dict(model.to_dict()).credential == "siwc:def"


def _oauth_raw(**provider_overrides) -> dict:
    provider = {
        "interface_type": "anthropic",
        "auth_type": "oauth",
        "credential": "siwc:abc",
        "models": {"m": {}},
    }
    provider.update(provider_overrides)
    return {
        "providers": {"gpt": provider},
        "agents": {"master": {"model": "gpt/m"}},
    }


def test_oauth_provider_skips_api_key_check():
    config = Config.from_global_config(_oauth_raw())
    model = config.resolve_model("gpt/m")
    assert model.auth_type == "oauth"
    assert model.api_key == ""
    assert model.credential == "siwc:abc"


def test_oauth_provider_endpoint_normalized():
    config = Config.from_global_config(
        _oauth_raw(interface_type="anthropic", base_url="http://evil.example.com", api_path="/v1/x")
    )
    model = config.resolve_model("gpt/m")
    assert model.interface_type == "openai-responses"
    assert model.base_url == "https://api.openai.com/v1"
    assert model.api_path == ""


def test_env_does_not_override_oauth(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://env.example.com")
    monkeypatch.setenv("ANTHROPIC_MODEL", "env-model")
    config = Config.from_global_config(_oauth_raw())
    model = config.resolve_model("gpt/m")
    assert model.api_key == ""
    assert model.base_url == "https://api.openai.com/v1"
    assert model.name == "m"


def test_non_oauth_provider_still_requires_key():
    raw = {
        "providers": {"p": {"interface_type": "openai", "models": {"m": {}}}},
        "agents": {"master": {"model": "p/m"}},
    }
    try:
        Config.from_global_config(raw)
    except RuntimeError as e:
        assert "No API key" in str(e)
    else:
        raise AssertionError("非 OAuth provider 缺少 api_key 时应启动失败")
