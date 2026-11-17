"""接入服务商预设表测试（server_presets）。"""

from __future__ import annotations

import pytest

from core.llm.adapter import join_api_url
from core.llm.server_presets import (
    BACKEND_SESSION_HEADERS,
    PRESETS,
    PROTOCOL_DEFAULT_API_PATHS,
    SUPPORTED_PROTOCOLS,
    get_preset,
    preset_headers,
    preset_payloads,
    protocol_default_api_path,
)


def actual_endpoint(key: str, protocol: str) -> str:
    """按 Adapter 的拼接规则得到预设在该协议下的实际端点。"""
    preset = get_preset(key)
    base = preset.base_urls.get(protocol)
    if not base:
        return ""
    path = preset.api_paths.get(protocol) or protocol_default_api_path(protocol)
    return join_api_url(base, path)


# ---------- 表结构 ----------

def test_preset_keys_unique_and_labeled():
    assert len(PRESETS) == len(set(PRESETS))
    for key, preset in PRESETS.items():
        assert preset.key == key
        assert preset.label
        assert preset.group in ("plan", "direct")


def test_base_urls_and_api_paths_use_supported_protocols():
    for preset in PRESETS.values():
        for protocol in preset.base_urls:
            assert protocol in SUPPORTED_PROTOCOLS, (preset.key, protocol)
        for protocol in preset.api_paths:
            assert protocol in SUPPORTED_PROTOCOLS, (preset.key, protocol)
        for url in preset.base_urls.values():
            assert url.startswith(("http://", "https://"))
            assert not url.endswith("/"), (preset.key, url)
        for path in preset.api_paths.values():
            assert path.startswith("/"), (preset.key, path)


def test_default_protocol_has_base_url():
    for preset in PRESETS.values():
        if preset.default_protocol:
            assert preset.default_protocol in SUPPORTED_PROTOCOLS
            assert preset.base_urls.get(preset.default_protocol), preset.key


def test_unknown_backend_falls_back_to_custom():
    assert get_preset("codex").key == "custom"
    assert get_preset("claude-code").key == "custom"
    assert get_preset("opencode").key == "custom"
    assert get_preset("").key == "custom"
    assert get_preset(None).key == "custom"


# ---------- 会话头 / 静态头 ----------

def test_removed_session_headers_are_gone():
    """codex / claude-code / opencode 彻底移除。"""
    for key in ("codex", "claude-code", "opencode"):
        assert key not in BACKEND_SESSION_HEADERS
        assert key not in PRESETS


def test_session_headers_expected():
    assert BACKEND_SESSION_HEADERS == {
        "chatgpt-siwc": {"session-id": "{session_id}"},
        "opencode-go": {"x-opencode-session": "{session_id}"},
        "openrouter": {"x-session-id": "{session_id}"},
        "litellm": {"x-litellm-session-id": "{session_id}"},
    }


def test_deepseek_has_no_session_headers():
    assert get_preset("deepseek").session_headers == {}


def test_coding_agent_ua_headers():
    assert preset_headers("kimi-coding")["User-Agent"] == "claude-code/0.1.0"
    assert preset_headers("stepfun-step-plan")["User-Agent"] == "claude-code/0.1.0"
    assert preset_headers("deepseek") == {}


def test_payload_excludes_session_headers():
    payloads = {p["key"]: p for p in preset_payloads()}
    for payload in payloads.values():
        assert "session_headers" not in payload
        assert "base_urls" in payload and "api_paths" in payload
    # 已移除的预设不出现在下发数据里
    assert "codex" not in payloads
    assert "claude-code" not in payloads
    assert "opencode" not in payloads
    # opencode-go 的独立模型清单端点
    assert payloads["opencode-go"]["models_url"] == "https://opencode.ai/inference/go/v1/models"


# ---------- 实际端点 ----------

@pytest.mark.parametrize(("key", "protocol", "expected"), [
    # DeepSeek：Responses 走 /responses
    ("deepseek", "anthropic", "https://api.deepseek.com/anthropic/v1/messages"),
    ("deepseek", "openai", "https://api.deepseek.com/v1/chat/completions"),
    ("deepseek", "openai-responses", "https://api.deepseek.com/responses"),
    # OpenCode Go
    ("opencode-go", "anthropic", "https://opencode.ai/inference/go/anthropic/v1/messages"),
    ("opencode-go", "openai", "https://opencode.ai/inference/go/openai/v1/chat/completions"),
    ("opencode-go", "openai-responses", "https://opencode.ai/inference/go/openai/v1/responses"),
    # 阿里云 Token Plan
    ("qwen-token-plan", "anthropic",
     "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic/v1/messages"),
    ("qwen-token-plan", "openai",
     "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"),
    # 百度千帆：Chat/Responses 均无 /v1
    ("qianfan-coding", "anthropic", "https://qianfan.baidubce.com/anthropic/coding/v1/messages"),
    ("qianfan-coding", "openai", "https://qianfan.baidubce.com/v2/coding/chat/completions"),
    ("qianfan-coding", "openai-responses", "https://qianfan.baidubce.com/v2/coding/responses"),
    # 火山方舟
    ("volcengine-coding", "anthropic", "https://ark.cn-beijing.volces.com/api/coding/v1/messages"),
    ("volcengine-coding", "openai", "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions"),
    ("volcengine-coding", "openai-responses", "https://ark.cn-beijing.volces.com/api/coding/v3/responses"),
    # 智谱 GLM
    ("glm-coding", "anthropic", "https://open.bigmodel.cn/api/anthropic/v1/messages"),
    ("glm-coding", "openai", "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"),
    ("glm-coding", "openai-responses", "https://open.bigmodel.cn/api/v1/responses"),
    # Kimi
    ("kimi-coding", "anthropic", "https://api.kimi.com/coding/v1/messages"),
    ("kimi-coding", "openai", "https://api.kimi.com/coding/v1/chat/completions"),
    ("kimi-coding", "openai-responses", "https://api.kimi.com/coding/v1/responses"),
    # MiniMax
    ("minimax-plan", "anthropic", "https://api.minimax.cn/anthropic/v1/messages"),
    ("minimax-plan", "openai", "https://api.minimax.cn/v1/chat/completions"),
    # 腾讯 Token Plan
    ("tencent-token-plan", "anthropic", "https://tokenhub.tencentmaas.com/plan/anthropic/v1/messages"),
    ("tencent-token-plan", "openai", "https://tokenhub.tencentmaas.com/plan/v3/chat/completions"),
    ("tencent-token-plan", "openai-responses", "https://tokenhub.tencentmaas.com/plan/v3/responses"),
    # 讯飞星辰
    ("xfyun-coding", "anthropic",
     "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic/v1/messages"),
    ("xfyun-coding", "openai", "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2/chat/completions"),
    ("xfyun-coding", "openai-responses", "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2/responses"),
    # 阶跃 Step Plan
    ("stepfun-step-plan", "anthropic", "https://api.stepfun.com/step_plan/v1/messages"),
    ("stepfun-step-plan", "openai", "https://api.stepfun.com/step_plan/v1/chat/completions"),
    # 小米 MiMo Token Plan
    ("xiaomi-token-plan", "anthropic", "https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages"),
    ("xiaomi-token-plan", "openai", "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"),
    # 直连 / 网关
    ("anthropic", "anthropic", "https://api.anthropic.com/v1/messages"),
    ("openai", "openai", "https://api.openai.com/v1/chat/completions"),
    ("openai", "openai-responses", "https://api.openai.com/v1/responses"),
    ("openrouter", "anthropic", "https://openrouter.ai/api/v1/messages"),
    ("openrouter", "openai", "https://openrouter.ai/api/v1/chat/completions"),
    ("openrouter", "openai-responses", "https://openrouter.ai/api/v1/responses"),
    ("litellm", "anthropic", "http://localhost:4000/v1/messages"),
    ("litellm", "openai", "http://localhost:4000/v1/chat/completions"),
    ("litellm", "openai-responses", "http://localhost:4000/v1/responses"),
])
def test_actual_endpoint(key: str, protocol: str, expected: str):
    assert actual_endpoint(key, protocol) == expected


def test_every_preset_has_responses_path():
    """所有预设都必须有 Responses 端点（已确认补全）。"""
    for preset in PRESETS.values():
        if preset.key in ("custom", "anthropic"):
            continue
        assert preset.base_urls.get("openai-responses"), preset.key


def test_default_api_paths():
    assert PROTOCOL_DEFAULT_API_PATHS == {
        "anthropic": "/v1/messages",
        "openai": "/v1/chat/completions",
        "openai-responses": "/v1/responses",
    }
