"""接入服务商预设表。

一个预设把「服务商 / 订阅套餐」的连接信息收敛成结构化数据：
协议默认值、各协议的 Base URL 与路径、认证方式、静态请求头、会话标识头、模型清单端点。

- UI（web/static/settings.js）按 plan / direct 分组渲染下拉，并在选择服务商 / 切换协议时
  自动填充 Base URL、api_path、auth_type。
- 运行时 Adapter 用 ``get_preset(config.backend)`` 取会话头与静态 headers。
- 未知 / 旧 ``backend`` 值一律回退 ``custom``（不发送任何预设会话头）。

URL 口径（与 core/llm/adapter.join_api_url 配合）：
- anthropic 默认路径 ``/v1/messages``、openai 默认 ``/v1/chat/completions``、
  openai-responses 默认 ``/v1/responses``；base 自带 ``/v1`` 时自动去重。
- 厂商路径不含 ``/v1`` 的，通过 ``api_paths`` 覆盖（百度/火山/GLM/腾讯/讯飞的 Chat，
  DeepSeek 的 Responses）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

SUPPORTED_PROTOCOLS = ("anthropic", "openai", "openai-responses")

# 协议默认路径（空 api_path 时使用）
PROTOCOL_DEFAULT_API_PATHS: dict[str, str] = {
    "anthropic": "/v1/messages",
    "openai": "/v1/chat/completions",
    "openai-responses": "/v1/responses",
}

# 兼容手写旧别名
_PROTOCOL_ALIASES = {"openai-compatible": "openai"}

_DEFAULT_USER_AGENT = "claude-code/0.1.0"


def normalize_protocol(protocol: str | None) -> str:
    """把 interface_type 归一为 SUPPORTED_PROTOCOLS 之一（未知值按 anthropic）。"""
    value = (protocol or "").lower()
    value = _PROTOCOL_ALIASES.get(value, value)
    return value if value in SUPPORTED_PROTOCOLS else "anthropic"


@dataclass(frozen=True)
class ServerPreset:
    """一个接入服务商预设。

    字段语义见模块 docstring；``base_urls`` / ``api_paths`` / ``headers`` 的键：
    - base_urls / api_paths：``anthropic`` | ``openai`` | ``openai-responses``
    - headers / session_headers：HTTP 头名；值支持 ``{session_id}`` 占位符（显式 headers 同名覆盖）
    """

    key: str
    label: str
    group: str  # "plan"（套餐）| "direct"（直连/网关）
    default_protocol: str | None = None
    base_urls: dict[str, str] = field(default_factory=dict)
    api_paths: dict[str, str] = field(default_factory=dict)
    auth_type: str = "auto"
    headers: dict[str, str] = field(default_factory=dict)
    session_headers: dict[str, str] = field(default_factory=dict)
    hint: str = ""
    models_url: str = ""  # 「检测模型」用的独立清单端点；空 = 按 base 推导


# 顺序即下拉顺序：plan 分组在前，direct 在后。
PRESETS: dict[str, ServerPreset] = {
    "custom": ServerPreset(
        key="custom",
        label="自定义",
        group="direct",
    ),
    # ── 订阅套餐 ──────────────────────────────────────────────────
    "deepseek": ServerPreset(
        key="deepseek",
        label="DeepSeek 深度求索",
        group="direct",
        default_protocol="anthropic",
        base_urls={
            "anthropic": "https://api.deepseek.com/anthropic",
            "openai": "https://api.deepseek.com",
            "openai-responses": "https://api.deepseek.com",
        },
        api_paths={"openai-responses": "/responses"},
        hint="官方无套餐，按量计费；Responses 端点 /responses。",
    ),
    "chatgpt-siwc": ServerPreset(
        key="chatgpt-siwc",
        label="ChatGPT 套餐额度",
        group="plan",
        default_protocol="openai-responses",
        base_urls={"openai-responses": "https://api.openai.com/v1"},
        auth_type="oauth",
        session_headers={"session-id": "{session_id}"},
        models_url="https://api.openai.com/v1/models",
        hint="使用 ChatGPT Plus/Pro 套餐额度；需在设置页登录，仅限本地/自托管用法。",
    ),
    "opencode-go": ServerPreset(
        key="opencode-go",
        label="OpenCode Go",
        group="plan",
        default_protocol="anthropic",
        base_urls={
            "anthropic": "https://opencode.ai/inference/go/anthropic",
            "openai": "https://opencode.ai/inference/go/openai/v1",
            "openai-responses": "https://opencode.ai/inference/go/openai/v1",
        },
        session_headers={"x-opencode-session": "{session_id}"},
        models_url="https://opencode.ai/inference/go/v1/models",
        hint="Go 订阅；模型清单在独立端点。",
    ),
    "qwen-token-plan": ServerPreset(
        key="qwen-token-plan",
        label="阿里云 Token Plan",
        group="plan",
        default_protocol="anthropic",
        base_urls={
            "anthropic": "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic",
            "openai": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "openai-responses": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        },
        auth_type="bearer",
        hint="Token Plan 专属 Key；国内站端点。",
    ),
    "qianfan-coding": ServerPreset(
        key="qianfan-coding",
        label="百度千帆 Coding Plan",
        group="plan",
        default_protocol="anthropic",
        base_urls={
            "anthropic": "https://qianfan.baidubce.com/anthropic/coding",
            "openai": "https://qianfan.baidubce.com/v2/coding",
            "openai-responses": "https://qianfan.baidubce.com/v2/coding",
        },
        api_paths={
            "openai": "/chat/completions",
            "openai-responses": "/responses",
        },
        auth_type="bearer",
        hint="Coding Plan 专属 Key；OpenAI 路径不带 /v1。",
    ),
    "volcengine-coding": ServerPreset(
        key="volcengine-coding",
        label="火山方舟 Coding Plan",
        group="plan",
        default_protocol="anthropic",
        base_urls={
            "anthropic": "https://ark.cn-beijing.volces.com/api/coding",
            "openai": "https://ark.cn-beijing.volces.com/api/coding/v3",
            "openai-responses": "https://ark.cn-beijing.volces.com/api/coding/v3",
        },
        api_paths={
            "openai": "/chat/completions",
            "openai-responses": "/responses",
        },
        auth_type="bearer",
        hint="Coding Plan 专属 Key；OpenAI 路径不带 /v1。",
    ),
    "glm-coding": ServerPreset(
        key="glm-coding",
        label="智谱 GLM Coding Plan",
        group="plan",
        default_protocol="anthropic",
        base_urls={
            "anthropic": "https://open.bigmodel.cn/api/anthropic",
            "openai": "https://open.bigmodel.cn/api/coding/paas/v4",
            "openai-responses": "https://open.bigmodel.cn/api/v1",
        },
        api_paths={"openai": "/chat/completions"},
        auth_type="bearer",
        hint="Coding Plan 专属 Key；OpenAI 路径不带 /v1。",
    ),
    "kimi-coding": ServerPreset(
        key="kimi-coding",
        label="Kimi Coding Plan",
        group="plan",
        default_protocol="anthropic",
        base_urls={
            "anthropic": "https://api.kimi.com/coding",
            "openai": "https://api.kimi.com/coding/v1",
            "openai-responses": "https://api.kimi.com/coding/v1",
        },
        auth_type="bearer",
        headers={"User-Agent": _DEFAULT_USER_AGENT},
        hint="sk-kimi-* Key；需 Coding-Agent UA（已自动携带）。",
    ),
    "minimax-plan": ServerPreset(
        key="minimax-plan",
        label="MiniMax M Plan",
        group="plan",
        default_protocol="anthropic",
        base_urls={
            "anthropic": "https://api.minimax.cn/anthropic",
            "openai": "https://api.minimax.cn/v1",
            "openai-responses": "https://api.minimax.cn/v1",
        },
        auth_type="bearer",
        hint="M Plan 订阅 Key（sk-cp-*）；国内站端点。",
    ),
    "tencent-token-plan": ServerPreset(
        key="tencent-token-plan",
        label="腾讯 Token Plan",
        group="plan",
        default_protocol="anthropic",
        base_urls={
            "anthropic": "https://tokenhub.tencentmaas.com/plan/anthropic",
            "openai": "https://tokenhub.tencentmaas.com/plan/v3",
            "openai-responses": "https://tokenhub.tencentmaas.com/plan/v3",
        },
        api_paths={
            "openai": "/chat/completions",
            "openai-responses": "/responses",
        },
        auth_type="bearer",
        hint="Token Plan 订阅 Key（sk-tp-*）；OpenAI 路径不带 /v1。",
    ),
    "xfyun-coding": ServerPreset(
        key="xfyun-coding",
        label="讯飞星辰 Coding Plan",
        group="plan",
        default_protocol="anthropic",
        base_urls={
            "anthropic": "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic",
            "openai": "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2",
            "openai-responses": "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2",
        },
        api_paths={
            "openai": "/chat/completions",
            "openai-responses": "/responses",
        },
        auth_type="bearer",
        hint="套餐订阅 Key；模型固定 astron-code-latest。",
    ),
    "stepfun-step-plan": ServerPreset(
        key="stepfun-step-plan",
        label="阶跃 Step Plan",
        group="plan",
        default_protocol="anthropic",
        base_urls={
            "anthropic": "https://api.stepfun.com/step_plan",
            "openai": "https://api.stepfun.com/step_plan/v1",
            "openai-responses": "https://api.stepfun.com/step_plan/v1",
        },
        auth_type="bearer",
        headers={"User-Agent": _DEFAULT_USER_AGENT},
        hint="Step Plan 订阅 Key；需 Claude 兼容 UA（已自动携带）。",
    ),
    "xiaomi-token-plan": ServerPreset(
        key="xiaomi-token-plan",
        label="小米 MiMo Token Plan",
        group="plan",
        default_protocol="anthropic",
        base_urls={
            "anthropic": "https://token-plan-cn.xiaomimimo.com/anthropic",
            "openai": "https://token-plan-cn.xiaomimimo.com/v1",
            "openai-responses": "https://token-plan-cn.xiaomimimo.com/v1",
        },
        auth_type="bearer",
        hint="Token Plan 订阅 Key（tp-* / ttp-*）；国内站端点。",
    ),
    # ── 直连 / 网关 ──────────────────────────────────────────────
    "anthropic": ServerPreset(
        key="anthropic",
        label="Anthropic 官方",
        group="direct",
        default_protocol="anthropic",
        base_urls={"anthropic": "https://api.anthropic.com"},
    ),
    "openai": ServerPreset(
        key="openai",
        label="OpenAI 官方",
        group="direct",
        default_protocol="openai",
        base_urls={
            "openai": "https://api.openai.com",
            "openai-responses": "https://api.openai.com/v1",
        },
    ),
    "openrouter": ServerPreset(
        key="openrouter",
        label="OpenRouter",
        group="direct",
        default_protocol="openai",
        base_urls={
            "anthropic": "https://openrouter.ai/api",
            "openai": "https://openrouter.ai/api/v1",
            "openai-responses": "https://openrouter.ai/api/v1",
        },
        session_headers={"x-session-id": "{session_id}"},
    ),
    "litellm": ServerPreset(
        key="litellm",
        label="LiteLLM",
        group="direct",
        default_protocol="openai",
        base_urls={
            "anthropic": "http://localhost:4000",
            "openai": "http://localhost:4000",
            "openai-responses": "http://localhost:4000/v1",
        },
        auth_type="bearer",
        session_headers={"x-litellm-session-id": "{session_id}"},
        hint="自建代理默认 http://localhost:4000。",
    ),
}


def get_preset(key: str | None) -> ServerPreset:
    """按 ``backend`` 取预设；未知 / 空值回退 ``custom``。"""
    return PRESETS.get((key or "").strip()) or PRESETS["custom"]


def protocol_default_api_path(protocol: str) -> str:
    return PROTOCOL_DEFAULT_API_PATHS[normalize_protocol(protocol)]


def preset_headers(key: str | None) -> dict[str, str]:
    return dict(get_preset(key).headers)


def preset_payloads() -> list[dict]:
    """前端下拉所需的数据（不含会话头细节）。"""
    return [
        {
            "key": preset.key,
            "label": preset.label,
            "group": preset.group,
            "default_protocol": preset.default_protocol,
            "base_urls": dict(preset.base_urls),
            "api_paths": dict(preset.api_paths),
            "auth_type": preset.auth_type,
            "headers": dict(preset.headers),
            "hint": preset.hint,
            "models_url": preset.models_url,
        }
        for preset in PRESETS.values()
    ]


# 会话标识头：由预设表派生，避免两处维护。
# 注意：codex / claude-code / opencode 已彻底移除，不再出现在此表。
BACKEND_SESSION_HEADERS: dict[str, dict[str, str]] = {
    preset.key: dict(preset.session_headers)
    for preset in PRESETS.values()
    if preset.session_headers
}
