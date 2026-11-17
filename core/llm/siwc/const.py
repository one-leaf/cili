"""SIWC（Sign in with ChatGPT）常量与开关。

均为 OpenAI 预览期行为：端点/字段策略变化时优先改这里，不改散落逻辑。
"""

from __future__ import annotations

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
REVOKE_URL = f"{ISSUER}/api/accounts/oauth/revoke"

API_BASE = "https://api.openai.com/v1"
RESOURCE = API_BASE
MODELS_URL = f"{API_BASE}/models"

DYNAMIC_CLIENT_ID = "dynamic_agent_client"
CLIENT_ID_RE = r"^oaiapp_[A-Za-z0-9_-]+$"
HOST_ID_PREFIX = "urn:uuid:"
AGENT_NAME_HINT = "Cili Agent"

SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"

TOKEN_EXPIRY_MARGIN_SECONDS = 300
CALLBACK_PATH = "/auth/callback"
LOGIN_TIMEOUT_SECONDS = 600
REFRESH_TIMEOUT_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 30
HTTP_CONNECT_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1024 * 1024

CREDENTIAL_PREFIX = "siwc:"

FATAL_REFRESH_ERRORS = frozenset({
    "invalid_grant", "invalid_client", "invalid_refresh_token",
    "refresh_token_expired", "refresh_token_invalidated",
    "refresh_token_reused", "token_expired",
})

# SIWC 预览期拒绝的请求字段（profile.shape_request 统一裁剪）。
UNSUPPORTED_REQUEST_FIELDS = (
    "background", "conversation", "context_management", "metadata",
    "moderation", "multi_agent", "prompt", "prompt_cache_retention",
    "prompt_cache_options", "safety_identifier", "service_tier", "temperature",
    "top_logprobs", "top_p", "truncation", "user",
)

# 能力开关：预览期字段策略变化时改这里，避免动业务代码。
KEEP_PROMPT_CACHE_KEY = True
SEND_REASONING = True
SEND_INCLUDE_ENCRYPTED = True
SEND_STRICT_FALSE = True
