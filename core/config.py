"""Configuration loader - global config with multi-model support"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.config_migrations import convert_legacy_config, migrate_legacy_config  # noqa: F401 —— 对外重导出
from core.fs_utils import atomic_write_json
from core.permission_settings import PermissionConfig
from core.time_utils import now_str

logger = logging.getLogger(__name__)


# 协议缺省地址（与 core/llm/server_presets.py 的预设表保持同一口径）
_PROTOCOL_DEFAULT_BASE_URLS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "openai-compatible": "https://api.openai.com/v1",
    "openai-responses": "https://api.openai.com/v1",
}


def default_base_url(interface_type: str) -> str:
    """按协议返回缺省 Base URL（openai 系统一 /v1）。"""
    return _PROTOCOL_DEFAULT_BASE_URLS.get((interface_type or "").lower(), "https://api.anthropic.com")


def _normalize_api_path(value) -> str:
    """路径覆盖：空 = 协议默认；非空必须以 / 开头（fail-fast）。"""
    path = str(value or "").strip()
    if path and not path.startswith("/"):
        raise ValueError(f"api_path 必须以 / 开头: {path!r}")
    return path


def _detect_browser_path() -> str:
    """系统浏览器探测（实现见 core.browser_paths；保留薄封装便于测试替换）。"""
    from core.browser_paths import detect_browser_path

    return detect_browser_path()


@dataclass
class ModelConfig:
    """运行时模型配置：provider 接入层与 model 条目的合成结果（resolve_model 产出）。

    旧版各角色独立配置的完整对象保留为运行时形态，下游（LLMClient/Adapter/runner）不感知配置格式。
    auth_type/backend/headers 属接入层，随 provider 一起带入（adapter 构造请求头用）。
    """
    name: str
    interface_type: str = "anthropic"  # "anthropic" | "openai" | "openai-responses"
    api_key: str = ""
    base_url: str = "https://api.anthropic.com"
    api_path: str = ""  # 协议路径覆盖；空 = 用协议默认（/v1/messages 等）
    auth_type: str = "auto"  # "auto" | "bearer" | "x-api-key" | "none" | "oauth"
    backend: str = "custom"  # 接入服务商预设键（见 core/llm/server_presets.py）
    credential: str = ""  # OAuth 凭证标识（"siwc:<account_id>"）；非秘密
    headers: dict[str, str] = field(default_factory=dict)  # 值支持 {session_id} 占位符
    max_output_tokens: int = 32768  # 单次响应最大输出 token
    max_context_tokens: int = 200000  # 上下文工作预算（压缩阈值基数）
    model_type: str = "reasoning"  # "reasoning" | "chat"：思考选项按此设置
    input: list[str] = field(default_factory=lambda: ["text", "image"])  # 输入模态，必须含 "text"
    reasoning: str = "medium"  # 思考档位：default/off/low/medium/high（default=不发参数，交给服务端）
    cache_control: str = "auto"  # Prompt cache: "auto" (protocol default) | "true" (force) | "false" (disable)

    @classmethod
    def from_dict(cls, data: dict) -> "ModelConfig":
        """Parse ModelConfig from a dict."""
        return cls(
            name=data.get("name", ""),
            interface_type=data.get("interface_type", "anthropic"),
            api_key=data.get("api_key", ""),
            base_url=(data.get("base_url") or default_base_url(data.get("interface_type", "anthropic"))).rstrip("/"),
            api_path=_normalize_api_path(data.get("api_path")),
            auth_type=data.get("auth_type", "auto"),
            backend=data.get("backend", "custom"),
            credential=str(data.get("credential", "") or ""),
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            max_output_tokens=int(data.get("max_output_tokens", data.get("max_tokens", 32768))),
            max_context_tokens=int(data.get("max_context_tokens", 200000)),
            model_type=data.get("model_type", "reasoning"),
            input=list(data.get("input") or ["text", "image"]),
            reasoning=data.get("reasoning", "medium"),
            cache_control=data.get("cache_control", "auto"),
        )

    def to_dict(self) -> dict:
        """Convert to a serializable dict."""
        return {
            "name": self.name,
            "interface_type": self.interface_type,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "api_path": self.api_path,
            "auth_type": self.auth_type,
            "backend": self.backend,
            "credential": self.credential,
            "headers": dict(self.headers),
            "max_output_tokens": self.max_output_tokens,
            "max_context_tokens": self.max_context_tokens,
            "model_type": self.model_type,
            "input": list(self.input),
            "reasoning": self.reasoning,
            "cache_control": self.cache_control,
        }


@dataclass
class ModelSpec:
    """模型条目（provider 下 models 表的值；键为自由别名，``name`` 省略时用键名作 API 模型 ID）。"""
    name: str = ""  # 发给 API 的真实模型 ID；空 = 用键名
    max_output_tokens: int = 32768  # 单次响应最大输出 token
    max_context_tokens: int = 200000  # 上下文工作预算（压缩阈值基数）
    model_type: str = "reasoning"  # "reasoning" | "chat"
    input: list[str] = field(default_factory=lambda: ["text", "image"])
    reasoning: str = "medium"  # 取值域按 provider 的 interface_type 各自定义
    cache_control: str = "auto"

    @classmethod
    def from_dict(cls, data: dict) -> "ModelSpec":
        return cls(
            name=str(data.get("name", "")),
            max_output_tokens=int(data.get("max_output_tokens", data.get("max_tokens", 32768))),
            max_context_tokens=int(data.get("max_context_tokens", 200000)),
            model_type=data.get("model_type", "reasoning"),
            input=list(data.get("input") or ["text", "image"]),
            reasoning=data.get("reasoning", "medium"),
            cache_control=data.get("cache_control", "auto"),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "max_output_tokens": self.max_output_tokens,
            "max_context_tokens": self.max_context_tokens,
            "model_type": self.model_type,
            "input": list(self.input),
            "reasoning": self.reasoning,
            "cache_control": self.cache_control,
        }


@dataclass
class ProviderConfig:
    """接入层配置：管 api_key / base_url / 协议与多个模型条目。"""
    interface_type: str = "anthropic"  # "anthropic" | "openai" | "openai-responses"
    api_key: str = ""  # 支持 ${ENV_VAR} 整值插值
    auth_type: str = "auto"  # "auto" | "bearer" | "x-api-key" | "none" | "oauth"
    backend: str = "custom"  # 接入服务商预设键（见 core/llm/server_presets.py）
    credential: str = ""  # OAuth 凭证标识（"siwc:<account_id>"）；非秘密
    base_url: str = "https://api.anthropic.com"
    api_path: str = ""  # 协议路径覆盖；空 = 用协议默认（/v1/messages 等）
    headers: dict[str, str] = field(default_factory=dict)  # 值支持 {session_id} 占位符
    models: dict[str, ModelSpec] = field(default_factory=dict)  # 键 = 自由别名；省略时视为 {"default": {}}

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderConfig":
        raw_models = data.get("models")
        if isinstance(raw_models, dict) and raw_models:
            models = {key: ModelSpec.from_dict(v if isinstance(v, dict) else {})
                      for key, v in raw_models.items()}
        else:
            models = {"default": ModelSpec.from_dict({})}
        return cls(
            interface_type=data.get("interface_type", "anthropic"),
            api_key=_expand_env_value(str(data.get("api_key", ""))),
            auth_type=data.get("auth_type", "auto"),
            backend=data.get("backend", "custom"),
            credential=str(data.get("credential", "") or ""),
            base_url=(data.get("base_url") or default_base_url(data.get("interface_type", "anthropic"))).rstrip("/"),
            api_path=_normalize_api_path(data.get("api_path")),
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            models=models,
        )

    def to_dict(self) -> dict:
        return {
            "interface_type": self.interface_type,
            "api_key": self.api_key,
            "auth_type": self.auth_type,
            "backend": self.backend,
            "credential": self.credential,
            "base_url": self.base_url,
            "api_path": self.api_path,
            "headers": dict(self.headers),
            "models": {key: spec.to_dict() for key, spec in self.models.items()},
        }


@dataclass
class AgentConfig:
    """角色条目：模型引用（"provider键/模型键"）。"""
    model: str = ""  # 空 = 继承 master

    @classmethod
    def from_dict(cls, data: dict) -> "AgentConfig":
        return cls(model=str(data.get("model", "")) if isinstance(data, dict) else "")

    def to_dict(self) -> dict:
        return {"model": self.model}


_ENV_INTERP_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _expand_env_value(value: str) -> str:
    """``${ENV_VAR}`` 整值插值；变量未设置时启动报错（fail-fast，不静默空 key）。"""
    matched = _ENV_INTERP_RE.fullmatch(value.strip())
    if not matched:
        return value
    var = matched.group(1)
    env_value = os.environ.get(var)
    if env_value is None:
        raise RuntimeError(
            f"环境变量 {var} 未设置（api_key 引用了 ${{{var}}}）；"
            f"请设置该变量或将 api_key 改为字面值"
        )
    return env_value


@dataclass
class SystemConfig:
    """System-level configuration (pip mirror, env paths, etc.)."""
    pip_mirror: str = "https://repo.huaweicloud.com/repository/pypi/simple/"
    allowed_ips: list[str] = field(default_factory=list)  # IP whitelist; empty = localhost only
    browser_path: str = ""  # Browser executable path (auto-detected if empty or invalid)
    search_engine: str = "bing"  # Web search engine: "bing" or "google"
    mineru_api_key: str = ""  # MinerU API key for PDF to Markdown (Precision Parse API)
    max_iterations: int = 200  # Agent maximum tool call iterations per session
    max_concurrent_agents: int = 2  # 同时运行的后台子代理上限（范围 1-10）
    access_token: str = ""  # 访问令牌：非 localhost 绑定且未配置时拒绝启动；配置后 Web 请求需携带
    parallel_tools: bool = True  # 工具批内并行：无依赖的纯读工具（concurrency_safe）同一回合并行执行

    @classmethod
    def from_dict(cls, data: dict) -> "SystemConfig":
        """Parse SystemConfig from a dict."""
        max_concurrent_agents = int(data.get("max_concurrent_agents", 2))
        max_concurrent_agents = max(1, min(10, max_concurrent_agents))
        return cls(
            pip_mirror=data.get("pip_mirror", "https://repo.huaweicloud.com/repository/pypi/simple/"),
            allowed_ips=data.get("allowed_ips") or [],
            browser_path=data.get("browser_path", ""),
            search_engine=data.get("search_engine", "bing"),
            mineru_api_key=data.get("mineru_api_key", ""),
            max_iterations=int(data.get("max_iterations", 200)),
            max_concurrent_agents=max_concurrent_agents,
            access_token=data.get("access_token", ""),
            parallel_tools=bool(data.get("parallel_tools", True)),
        )

    def to_dict(self) -> dict:
        """Convert to a serializable dict."""
        return {
            "pip_mirror": self.pip_mirror,
            "allowed_ips": self.allowed_ips,
            "browser_path": self.browser_path,
            "search_engine": self.search_engine,
            "mineru_api_key": self.mineru_api_key,
            "max_iterations": self.max_iterations,
            "max_concurrent_agents": self.max_concurrent_agents,
            "access_token": self.access_token,
            "parallel_tools": self.parallel_tools,
        }


@dataclass
class MCPConfig:
    """MCP 服务器连接配置（streamableHttp，唯一支持的传输）。

    headers 支持 Bearer/API Key 认证，如 {"Authorization": "Bearer xxx"}。
    """
    url: str = ""  # streamableHttp 端点 URL
    headers: dict[str, str] = field(default_factory=dict)  # HTTP 请求头（如 Authorization / X-API-Key）
    tool_timeout: int = 30  # 工具调用超时（秒）
    enabled_tools: list[str] = field(default_factory=lambda: ["*"])  # 工具白名单；["*"] = 全部

    @classmethod
    def from_dict(cls, data: dict) -> "MCPConfig":
        """Parse MCPConfig from a dict."""
        return cls(
            url=data.get("url", ""),
            headers=dict(data.get("headers", {})),
            tool_timeout=int(data.get("tool_timeout", 30)),
            enabled_tools=list(data.get("enabled_tools", ["*"])),
        )

    def to_dict(self) -> dict:
        """Convert to a serializable dict."""
        return {
            "url": self.url,
            "headers": self.headers,
            "tool_timeout": self.tool_timeout,
            "enabled_tools": self.enabled_tools,
        }


@dataclass
class ImageConfig:
    """图片处理配置：``setting.json -> images``。

    ``history_mode`` 默认 ``current_turn``；``full`` 仅作为显式回退。

    预算口径：
    - ``max_images_per_request``：请求内图片块数量上限（默认 100）。
    - ``outbound_image_hard_limit``：所有图片都 ≤ ``max_dimension`` 时的放宽上限。
    - ``max_request_image_bytes``：请求内图片 base64 总字节预算。
    - ``max_images_per_tool_result``：单个工具结果最多内联几张，超出转引用。
    - ``eviction_batch`` / ``eviction_floor``：请求级退役的步进量子与最新载体保底数。
    """

    history_mode: str = "current_turn"  # current_turn（默认） | full（回退）
    # DeepSeek：单请求 ≥15 张图时 provider 单边上限 4096px；统一按 4096 归一化
    max_dimension: int = 4096
    # 每张请求图的编码目标（DeepSeek DEFAULT_REQUEST_IMAGE_MAX_BYTES = 2 MiB）
    request_image_max_bytes: int = 2 * 1024 * 1024
    # 请求内图片 base64 总字节预算：20 MiB，对齐 deepseek-harness 的 inline 默认值
    max_request_image_bytes: int = 20 * 1024 * 1024
    # 请求内图片块数量上限：deepseek 单消息准入 20 / 请求上限 600；
    # 取 100（hermes hard limit）作为桌面端默认
    max_images_per_request: int = 100
    # 所有图片都 ≤ max_dimension 时的放宽上限（hermes: 100）
    outbound_image_hard_limit: int = 100
    # 单个工具结果内联图片上限：超出部分转路径引用
    max_images_per_tool_result: int = 8
    # 请求级退役：每次退役的载体数 / 最新载体保底数
    eviction_batch: int = 4
    eviction_floor: int = 3
    # 重读去重：同一内容已内联且未退役时不再新增图片块
    dedupe_inline_images: bool = True
    jpeg_quality: int = 85

    @classmethod
    def from_dict(cls, data: dict) -> "ImageConfig":
        data = data if isinstance(data, dict) else {}
        mode = str(data.get("history_mode", "current_turn"))
        if mode not in ("full", "current_turn"):
            mode = "current_turn"
        return cls(
            history_mode=mode,
            max_dimension=max(1, int(data.get("max_dimension", 4096))),
            request_image_max_bytes=max(
                1, int(data.get("request_image_max_bytes", 2 * 1024 * 1024))
            ),
            max_request_image_bytes=max(
                1, int(data.get("max_request_image_bytes", 20 * 1024 * 1024))
            ),
            max_images_per_request=max(1, int(data.get("max_images_per_request", 100))),
            outbound_image_hard_limit=max(
                1, int(data.get("outbound_image_hard_limit", 100))
            ),
            max_images_per_tool_result=max(
                1, int(data.get("max_images_per_tool_result", 8))
            ),
            eviction_batch=max(1, int(data.get("eviction_batch", 4))),
            eviction_floor=max(0, int(data.get("eviction_floor", 3))),
            dedupe_inline_images=bool(data.get("dedupe_inline_images", True)),
            jpeg_quality=max(1, min(100, int(data.get("jpeg_quality", 85)))),
        )

    def to_dict(self) -> dict:
        return {
            "history_mode": self.history_mode,
            "max_dimension": self.max_dimension,
            "request_image_max_bytes": self.request_image_max_bytes,
            "max_request_image_bytes": self.max_request_image_bytes,
            "max_images_per_request": self.max_images_per_request,
            "outbound_image_hard_limit": self.outbound_image_hard_limit,
            "max_images_per_tool_result": self.max_images_per_tool_result,
            "eviction_batch": self.eviction_batch,
            "eviction_floor": self.eviction_floor,
            "dedupe_inline_images": self.dedupe_inline_images,
            "jpeg_quality": self.jpeg_quality,
        }


@dataclass
class Config:
    """Global configuration.

    模型配置为两级结构：``providers``（凭据/接入层 + models 表）与 ``agents``
    （角色 → 模型引用字符串，如 ``"bg/main"``）。
    """
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    agents: dict[str, AgentConfig] = field(default_factory=dict)  # 键 = 角色名（master/worker/lite）
    system: SystemConfig = field(default_factory=SystemConfig)
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    images: ImageConfig = field(default_factory=ImageConfig)
    mcp_servers: dict[str, MCPConfig] = field(default_factory=dict)

    # ── 引用解析 ──

    def resolve_model(self, ref: str) -> ModelConfig:
        """解析 ``"provider键/模型键"`` 为运行时 ModelConfig。

        按第一个 ``/`` 拆分（模型键内可再含 ``/``）；引用无效时 RuntimeError 并列出可用引用。
        """
        provider_key, _, model_key = ref.partition("/")
        if not provider_key or not model_key:
            raise RuntimeError(
                f"模型引用 {ref!r} 无效，应为 \"provider键/模型键\"；可用: {self.available_refs() or '（无）'}"
            )
        provider = self.providers.get(provider_key)
        if provider is None:
            raise RuntimeError(f"模型引用 {ref!r} 的 provider {provider_key!r} 不存在；可用: {self.available_refs()}")
        spec = provider.models.get(model_key)
        if spec is None:
            raise RuntimeError(
                f"模型引用 {ref!r} 的模型键 {model_key!r} 不存在；可用: {self.available_refs()}"
            )
        return ModelConfig(
            name=spec.name or model_key,
            interface_type=provider.interface_type,
            api_key=provider.api_key,
            base_url=provider.base_url,
            api_path=provider.api_path,
            auth_type=provider.auth_type,
            backend=provider.backend,
            credential=provider.credential,
            headers=dict(provider.headers),
            max_output_tokens=spec.max_output_tokens,
            max_context_tokens=spec.max_context_tokens,
            model_type=spec.model_type,
            input=list(spec.input),
            reasoning=spec.reasoning,
            cache_control=spec.cache_control,
        )

    def available_refs(self) -> list[str]:
        """全部可用模型引用（"provider键/模型键"），按 provider 分组排序。"""
        return [f"{pkey}/{mkey}" for pkey in sorted(self.providers) for mkey in sorted(self.providers[pkey].models)]

    def master_ref(self) -> str:
        """master 角色的模型引用；agents 未配置时按"全局恰好只有一个可用模型"兜底。"""
        ref = (self.agents.get("master") or AgentConfig()).model
        if ref:
            return ref
        available = self.available_refs()
        if len(available) == 1:
            return available[0]
        raise RuntimeError(
            f"未配置 agents.master.model 且无法从唯一可用模型兜底（可用: {available or '（无）'}）；"
            f"请在 setting.json 的 agents.master.model 配置模型引用"
        )

    def explicit_role_model(self, role: str) -> str | None:
        """角色在 setting.json 中显式配置的模型引用；未配置返回 None。

        与 ``role_refs`` 的「worker/lite 未配置时继承 master」判定同源——
        需要区分「角色有独立模型」与「跟随父级」的调用方（如 agent 工具的
        会话级模型继承）都用这里，避免两处各自判定漂移。
        """
        return (self.agents.get(role) or AgentConfig()).model or None

    def role_refs(self) -> dict[str, str]:
        """角色名 → 模型引用；worker/lite/workflow 未配置时继承 master。"""
        master_ref = self.master_ref()
        worker_ref = self.explicit_role_model("worker") or master_ref
        return {
            "master": master_ref,
            "worker": worker_ref,
            "lite": self.explicit_role_model("lite") or master_ref,
            "workflow": self.explicit_role_model("workflow") or master_ref,
        }

    @property
    def role_models(self) -> dict[str, ModelConfig]:
        """角色名 → 模型配置的显式映射（未单独配置的角色回退 master 模型）。"""
        return {role: self.resolve_model(ref) for role, ref in self.role_refs().items()}

    def model_for_role(self, role: str) -> ModelConfig:
        """取角色模型；未知角色回退 master 模型并告警。"""
        models = self.role_models
        if role in models:
            return models[role]
        logger.warning(
            f"[Config] 未知角色 {role!r}，回退 master 模型；"
            f"如需独立模型请在 setting.json 的 agents.{role}.model 配置模型引用"
        )
        return models["master"]

    # ── 构建 ──

    @classmethod
    def from_dict(cls, global_config: dict) -> "Config":
        """解析新格式配置 dict（结构/凭据/OAuth 端点锁定），不做环境变量覆盖。"""
        providers = {
            name: ProviderConfig.from_dict(cfg)
            for name, cfg in (global_config.get("providers") or {}).items()
            if isinstance(cfg, dict)
        }
        agents = {
            name: AgentConfig.from_dict(cfg)
            for name, cfg in (global_config.get("agents") or {}).items()
            if isinstance(cfg, dict)
        }
        if "master" not in agents:
            agents["master"] = AgentConfig.from_dict({})

        # OAuth provider：端点/协议由代码锁定，避免 token 被发往用户改写的主机。
        for p in providers.values():
            if p.auth_type == "oauth":
                p.interface_type = "openai-responses"
                p.base_url = "https://api.openai.com/v1"
                p.api_path = ""

        config = cls(providers=providers, agents=agents)

        # ── System ──
        config.system = SystemConfig.from_dict(global_config.get("system", {}))

        # ── Permissions（顶层 permissions 字段，不放进 system） ──
        config.permissions = PermissionConfig.from_dict(global_config.get("permissions") or {})

        # ── Images（顶层 images 字段） ──
        config.images = ImageConfig.from_dict(global_config.get("images") or {})

        # ── MCP servers ──
        config.mcp_servers = {
            name: MCPConfig.from_dict(cfg)
            for name, cfg in (global_config.get("mcp_servers") or {}).items()
            if isinstance(cfg, dict)
        }

        return config

    def apply_env_overrides(self, model_override: str | None = None) -> "Config":
        """env/CLI 覆盖：只作用于 master 的接入层/模型 ID，不改变模型归属。"""
        # ── env/CLI 覆盖：只作用于 master 的接入层/模型 ID，不改变模型归属 ──
        master_ref = self.master_ref()
        provider_key, _, model_key = master_ref.partition("/")
        provider = self.providers[provider_key]
        is_oauth = provider.auth_type == "oauth"
        env_api_key = os.environ.get("ANTHROPIC_API_KEY")
        env_base_url = os.environ.get("ANTHROPIC_BASE_URL")
        env_model = model_override or os.environ.get("ANTHROPIC_MODEL")
        if not is_oauth:
            if env_api_key:
                provider.api_key = env_api_key
            if env_base_url:
                provider.base_url = env_base_url.rstrip("/")
            if env_model:
                provider.models[model_key].name = env_model

        if not provider.api_key and not is_oauth:
            raise RuntimeError(
                f"No API key found. Please configure one of:\n"
                f"  1. Environment variable: ANTHROPIC_API_KEY\n"
                f"  2. Global config file: {GLOBAL_CONFIG_PATH}\n"
                f"\nYou can also configure via web UI or edit {GLOBAL_CONFIG_PATH} directly."
            )
        return self

    @classmethod
    def from_global_config(cls, global_config: dict, model_override: str | None = None) -> "Config":
        """新格式 dict → Config，并应用 env/CLI 覆盖。

        旧格式（model/worker_model/lite_model 为对象）由 ``migrate_legacy_config``
        在此之前转换，此处只处理新格式。
        """
        return cls.from_dict(global_config).apply_env_overrides(model_override)

    def to_dict(self) -> dict:
        """Convert to a serializable dict (for API response)。api_key 脱敏在路由层做。"""
        result: dict = {
            "providers": {name: cfg.to_dict() for name, cfg in self.providers.items()},
            "agents": {name: cfg.to_dict() for name, cfg in self.agents.items()},
            "system": self.system.to_dict(),
            "permissions": self.permissions.to_dict(),
            "images": self.images.to_dict(),
        }
        if self.mcp_servers:
            result["mcp_servers"] = {
                name: cfg.to_dict() for name, cfg in self.mcp_servers.items()
            }
        return result


# Base directories (config.py is in core/, project root is one level up)
PROJECT_ROOT = Path(os.path.dirname(os.path.abspath(__file__))).parent

# System 工作区（全局 cron 等系统数据）的保留 uuid
SYSTEM_WORKSPACE_UUID = "system"
DATA_ROOT = PROJECT_ROOT / "data"
DATA_DIR = PROJECT_ROOT / "data" / "cili"

# Global config path: data/cili/setting.json
GLOBAL_CONFIG_PATH = DATA_DIR / "setting.json"

# Workspace index: data/cili/workspaces.json (all workspace metadata in one file)
WORKSPACES_JSON = DATA_DIR / "workspaces.json"

# System workspace data dir (directory = data/, data dir = data/.cili/)
SYSTEM_DATA_DIR = DATA_ROOT / ".cili"


def validate_workspace_name(name: str) -> str | None:
    """校验工作区显示名（实现见 core.workspaces）。"""
    from core.workspaces import validate_workspace_name as _validate

    return _validate(name)


def load_workspaces_index() -> list[dict]:
    """读取 data/cili/workspaces.json（实现与缓存见 core.workspaces）。"""
    from core.workspaces import load_index

    return load_index(WORKSPACES_JSON)


def save_workspaces_index(workspaces: list[dict]) -> bool:
    """写入索引（实现在 core.workspaces）。"""
    from core.workspaces import save_index

    return save_index(WORKSPACES_JSON, workspaces)


def find_workspace_entry(workspace_uuid: str) -> dict | None:
    """按 uuid 查找工作区条目。"""
    from core.workspaces import find_entry

    return find_entry(WORKSPACES_JSON, workspace_uuid)


def upsert_workspace_entry(entry: dict) -> bool:
    """新增或更新工作区条目（按 uuid）。"""
    from core.workspaces import upsert_entry

    return upsert_entry(WORKSPACES_JSON, entry)


def remove_workspace_entry(workspace_uuid: str) -> bool:
    """删除工作区条目（幂等）。"""
    from core.workspaces import remove_entry

    return remove_entry(WORKSPACES_JSON, workspace_uuid)


def get_default_workspace_uuid() -> str:
    """默认工作区的 uuid（仅查询，不存在时返回空串；不触发创建）。"""
    for entry in load_workspaces_index():
        if entry.get("workspace_name") in ("Default", "default") and entry.get("uuid"):
            return str(entry["uuid"])
    return ""


def get_workspace_data_dir(workspace_uuid: str) -> Path:
    """Get the .cili data directory for a workspace: {directory}/.cili/.

    - 空 uuid：PROJECT_ROOT/workspace/default/.cili/
    - system：data/.cili/（directory = data/，找不到条目时兜底）
    - 其他：从 workspaces.json 读 directory，返回 {directory}/.cili/
    """
    if not workspace_uuid:
        return PROJECT_ROOT / "workspace" / "default" / ".cili"
    entry = find_workspace_entry(workspace_uuid)
    if entry and entry.get("directory"):
        return Path(entry["directory"]) / ".cili"
    if workspace_uuid == SYSTEM_WORKSPACE_UUID:
        return SYSTEM_DATA_DIR
    # 未知 uuid 兜底到「Default 工作区」：优先取索引里 Default 的目录，没有才退回
    # 硬编码路径。必须查索引而不是直接用 PROJECT_ROOT/workspace——session_search、
    # memory 等工具是 from core.config import 本函数的直接导入方，测试环境靠重定向
    # 后的索引把它们兜到 .test，硬编码会让隔离失效（测试数据写进真实工作区）。
    for entry in load_workspaces_index():
        if entry.get("workspace_name") in ("Default", "default") and entry.get("directory"):
            return Path(entry["directory"]) / ".cili"
    logger.warning(
        f"[config] 未知 workspace_uuid={workspace_uuid!r}，回退到默认工作区数据目录"
    )
    return PROJECT_ROOT / "workspace" / "default" / ".cili"


def get_system_workspace_data_dir() -> Path:
    """Get the system workspace data directory: data/.cili/."""
    return SYSTEM_DATA_DIR


def get_workspace_tools_dir(workspace_uuid: str) -> Path:
    """Get the tools data directory for a workspace: {workspace}/.cili/tools/."""
    return get_workspace_data_dir(workspace_uuid) / "tools"


def sessions_dir_for_data_dir(data_dir: Path | str) -> Path:
    """返回已解析的工作区数据目录下的 sessions 目录。"""
    return Path(data_dir) / "sessions"


def get_workspace_sessions_dir(workspace_uuid: str = "") -> Path:
    """工作区的 sessions 目录：{workspace}/.cili/sessions/。"""
    return sessions_dir_for_data_dir(get_workspace_data_dir(workspace_uuid))


def workspace_uuid_for_sessions_dir(sessions_dir: Path | str) -> str | None:
    """反查一个 sessions 目录所属的工作区 uuid；找不到返回 None。"""
    try:
        target = Path(sessions_dir).resolve()
    except OSError:
        return None
    for uuid in get_all_workspace_uuids():
        try:
            if get_workspace_sessions_dir(uuid).resolve() == target:
                return uuid
        except OSError:
            continue
    return None


def get_workspace_cwd(workspace_uuid: str = "") -> str:
    """工作区的用户可见目录（workspaces.json 条目；缺失时回退到数据目录父级）。"""
    ws_config = load_workspace_config(workspace_uuid)
    if ws_config and ws_config.get("directory"):
        return str(ws_config["directory"])
    return str(get_workspace_data_dir(workspace_uuid).parent)


def get_all_workspace_uuids() -> list[str]:
    """Get all workspace UUIDs including 'system'."""
    uuids = [SYSTEM_WORKSPACE_UUID]
    for entry in load_workspaces_index():
        uuid = entry.get("uuid")
        if uuid and uuid != SYSTEM_WORKSPACE_UUID:
            uuids.append(uuid)
    return uuids


# 全局配置 mtime 缓存（审查 P1-33）：check_access_control 中间件每请求调用
# load_config()，原实现每次 open+json.load 读盘。以 (路径, mtime_ns, size) 为
# 指纹，变更才重读，保住「改配置即生效」语义；返回深拷贝，调用方（如
# update_config）的就地改写不会污染缓存。
_GLOBAL_CONFIG_CACHE: dict = {}
_GLOBAL_CONFIG_SIG: tuple | None = None


def load_global_config() -> dict:
    """Load global model configuration from data/cili/setting.json.

    Returns empty dict if file doesn't exist or can't be read.
    Result is cached by (path, mtime_ns, size); a deep copy is returned so
    callers may mutate the result freely without poisoning the cache.
    """
    global _GLOBAL_CONFIG_CACHE, _GLOBAL_CONFIG_SIG

    path_key = str(GLOBAL_CONFIG_PATH)
    try:
        st = GLOBAL_CONFIG_PATH.stat()
        sig = (path_key, st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None

    if sig is not None and sig == _GLOBAL_CONFIG_SIG:
        return copy.deepcopy(_GLOBAL_CONFIG_CACHE)

    data: dict = {}
    if sig is not None:
        try:
            with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read {GLOBAL_CONFIG_PATH}: {e}")
            data = {}
    _GLOBAL_CONFIG_CACHE = data
    _GLOBAL_CONFIG_SIG = sig
    return copy.deepcopy(data)


def save_global_config(config: dict) -> bool:
    """Save global model configuration to data/cili/setting.json."""
    try:
        atomic_write_json(GLOBAL_CONFIG_PATH, config)
        return True
    except OSError as e:
        logger.warning(f"Failed to save {GLOBAL_CONFIG_PATH}: {e}")
        return False


def load_config(model_override: str | None = None) -> Config:
    """Load config with priority: CLI override > env vars > global config > defaults.

    旧格式（model/worker_model/lite_model 为对象）在此处自动迁移：
    备份原文件 → 转换 → 原子写回 → 按新格式解析（详见 migrate_legacy_config）。
    """
    global_config = migrate_legacy_config(load_global_config())
    return Config.from_global_config(global_config, model_override)


def get_workspace_config(workspace_uuid: str) -> dict:
    """Load workspace metadata from workspaces.json index.

    Returns dict with workspace_name, directory, created_at, updated_at, or {} if not found.
    """
    entry = find_workspace_entry(workspace_uuid)
    return entry or {}


def save_workspace_config(workspace_uuid: str, config: dict) -> bool:
    """Save workspace metadata into workspaces.json (keyed by uuid)."""
    config["uuid"] = workspace_uuid
    return upsert_workspace_entry(config)


def load_workspace_config(workspace_uuid: str) -> dict:
    """Load workspace metadata from workspaces.json index.

    Backward-compatible alias for get_workspace_config(): returns the
    workspaces.json entry dict, or {} if not found.
    """
    return get_workspace_config(workspace_uuid)


def _init_workspace_git_quietly(workspace_uuid: str) -> None:
    """尽力初始化工作区 Git 仓库（失败仅记日志，不阻断启动/调用方流程）。"""
    try:
        from core.workspace_git import init_workspace_git, is_git_initialized
        if not is_git_initialized(workspace_uuid):
            ok, msg = init_workspace_git(workspace_uuid)
            if not ok:
                logger.info(f"[config] 工作区 Git 初始化未执行: {msg}")
    except Exception as e:  # noqa: BLE001 —— git 缺失/子进程异常都不该影响启动
        logger.warning(f"[config] 工作区 Git 初始化失败: {e}")


def ensure_default_workspace() -> str | None:
    """确保存在名为 "Default" 的工作区，返回其 uuid（失败返回 None）。

    原先私有在 ``web/deps.py``；QQ 接入端启动时也要确定「当前工作区」的兜底，
    故下沉 core。幂等。

    Default 默认开启版本管理与记忆：新建时直接写入；旧条目缺省字段一次性
    补齐（显式关闭过的不动），补齐时顺带初始化 Git 仓库。
    """
    import secrets

    try:
        # 旧布局迁移（workspace/ 根 → workspace/default/）；已迁移或全新安装时为 no-op
        try:
            from core.workspace_migration import migrate_default_workspace
            migrate_default_workspace()
        except Exception as e:  # noqa: BLE001 —— 迁移失败不阻断启动，旧路径仍可用
            logger.error(f"[config] 默认工作区迁移失败: {e}")

        # 已存在 Default / legacy default → 直接返回（顺手迁移名字 + 补默认字段）
        for entry in load_workspaces_index():
            name = entry.get("workspace_name")
            uuid = entry.get("uuid")
            if name in ("Default", "default") and uuid:
                changed = False
                if name == "default":
                    entry["workspace_name"] = "Default"
                    changed = True
                if "git_enabled" not in entry or "memory_enabled" not in entry:
                    entry.setdefault("git_enabled", True)
                    entry.setdefault("memory_enabled", True)
                    changed = True
                if changed:
                    save_workspace_config(uuid, entry)
                    if entry.get("git_enabled"):
                        _init_workspace_git_quietly(uuid)
                # 数据目录被手动删除时按需重建（原 main._ensure_default_workspace 行为）
                get_workspace_data_dir(uuid).mkdir(parents=True, exist_ok=True)
                (get_workspace_data_dir(uuid) / "sessions").mkdir(exist_ok=True)
                return uuid

        workspace_uuid = secrets.token_hex(4)
        ws_data_dir = get_workspace_data_dir(workspace_uuid)
        ws_data_dir.mkdir(parents=True, exist_ok=True)
        (ws_data_dir / "sessions").mkdir(exist_ok=True)

        now = now_str()
        save_workspace_config(workspace_uuid, {
            "workspace_name": "Default",
            "directory": str(PROJECT_ROOT / "workspace" / "default"),
            "created_at": now,
            "updated_at": now,
            # 新建默认开启版本管理与记忆
            "git_enabled": True,
            "memory_enabled": True,
        })
        _init_workspace_git_quietly(workspace_uuid)
        logger.info(f"[config] 已创建默认工作区: {workspace_uuid}")
        return workspace_uuid
    except (OSError, PermissionError) as e:
        logger.error(f"[config] 创建默认工作区失败: {e}")
        return None
