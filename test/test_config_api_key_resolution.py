"""测试连接 / 模型列表检测 的 api_key 解析：按 provider 取已保存的 key。

回归背景：这两个端点原先在请求未带 api_key 时一律回退到 **master 角色**的 key。
在非 master 的 provider 卡片上点「测试」或「自动检测模型」时（重开设置后输入框为空，
明文 key 只存在于服务端），会拿别的 provider 的 key 去请求，表现为莫名的 401。
"""

import asyncio
import json

import pytest


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """把全局配置指向临时文件，避免读写真实 data/cili/setting.json。"""
    import core.config as config_mod
    cfg_path = tmp_path / "setting.json"
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", cfg_path)
    return cfg_path


def _write(cfg_path, data) -> None:
    cfg_path.write_text(json.dumps(data), encoding="utf-8")


_CONFIG = {
    "providers": {
        "default": {"api_key": "master-key", "models": {"main": {"name": "m"}}},
        "ms": {"api_key": "ms-key", "base_url": "https://ms.example/v1",
               "models": {"deepseek": {"name": "deepseek-ai/DeepSeek-V4.1-Flash"}}},
    },
    "agents": {"master": {"model": "default/main"}},
}


class TestResolveApiKey:
    def test_named_provider_uses_its_own_saved_key(self, isolated_config):
        """指定 provider → 用它的 key，不得回退到 master 的。"""
        _write(isolated_config, _CONFIG)
        from web.routes_config import TestConfigRequest, _resolve_test_provider

        _, api_key, _, _ = _resolve_test_provider(
            TestConfigRequest(interface_type="openai", provider="ms"))
        assert api_key == "ms-key"

    def test_masked_key_falls_back_to_saved_plaintext(self, isolated_config):
        """前端回传掩码（xxx...yyy）时同样按 provider 取已保存的明文。"""
        _write(isolated_config, _CONFIG)
        from web.routes_config import TestConfigRequest, _resolve_test_provider

        _, api_key, _, _ = _resolve_test_provider(
            TestConfigRequest(api_key="ms-k...-key", provider="ms"))
        assert api_key == "ms-key"

    def test_explicit_key_wins(self, isolated_config):
        _write(isolated_config, _CONFIG)
        from web.routes_config import TestConfigRequest, _resolve_test_provider

        _, api_key, _, _ = _resolve_test_provider(
            TestConfigRequest(api_key="typed-key", provider="ms"))
        assert api_key == "typed-key"

    def test_no_provider_keeps_master_fallback(self, isolated_config):
        """未指定 provider（空请求＝测主模型）保持原行为：回退 master 的 key。"""
        _write(isolated_config, _CONFIG)
        from web.routes_config import TestConfigRequest, _resolve_test_provider

        _, api_key, _, _ = _resolve_test_provider(TestConfigRequest())
        assert api_key == "master-key"

    def test_named_provider_without_saved_key_stays_empty(self, isolated_config):
        """指定了 provider 但它没有已保存的 key → 返回空（由调用方给出明确提示），
        不得静默改用 master 的 key。"""
        _write(isolated_config, _CONFIG)
        from web.routes_config import TestConfigRequest, _resolve_test_provider

        _, api_key, _, _ = _resolve_test_provider(TestConfigRequest(provider="ghost"))
        assert api_key == ""


class TestDetectModelsSharesResolution:
    def test_detect_models_passes_provider_through(self, isolated_config, monkeypatch):
        """detect_models 必须与测试连接共用同一解析（防两处再次分叉）。"""
        _write(isolated_config, _CONFIG)
        import web.routes_config as rc

        seen = {}

        def fake_resolve(request_api_key, provider):
            seen["args"] = (request_api_key, provider)
            return "resolved-key"

        monkeypatch.setattr(rc, "_resolve_api_key", fake_resolve)

        # base_url 指向必然拒绝连接的本地端口：函数会走完解析后失败返回，不触网
        out = asyncio.run(rc.detect_models(rc.DetectModelsRequest(
            interface_type="openai", base_url="http://127.0.0.1:1", provider="ms")))

        assert seen["args"] == (None, "ms")
        assert out["success"] is False
