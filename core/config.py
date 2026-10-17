"""Configuration loader - global config with multi-model support"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.fs_utils import atomic_write_json

logger = logging.getLogger(__name__)


def _detect_browser_path() -> str:
    """检测系统中可用的浏览器路径（Edge → Chrome 顺序）。

    Returns:
        str: 浏览器可执行文件路径，未找到返回空字符串
    """
    candidates = []

    if sys.platform == "win32":
        candidates = [
            # Edge
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
            # Chrome
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            # Edge
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            os.path.expanduser("~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            # Chrome
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:  # Linux
        candidates = [
            # Edge
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            # Chrome/Chromium
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]

    for path in candidates:
        if os.path.isfile(path):  # 必须是文件，不能是目录
            return path
    return ""


@dataclass
class ModelConfig:
    """运行时模型配置：provider 接入层与 model 条目的合成结果（resolve_model 产出）。

    旧版各角色独立配置的完整对象保留为运行时形态，下游（LLMClient/Adapter/runner）不感知配置格式。
    auth_type/backend/headers 属接入层，随 provider 一起带入（adapter 构造请求头用）。
    """
    name: str
    interface_type: str = "anthropic"  # "anthropic" | "openai"
    api_key: str = ""
    base_url: str = "https://api.anthropic.com"
    auth_type: str = "auto"  # "auto" | "bearer" | "x-api-key" | "none"
    backend: str = "custom"  # "custom" | "codex" | "claude-code" | "opencode" | "openrouter" | "litellm"
    headers: dict[str, str] = field(default_factory=dict)  # 值支持 {session_id} 占位符
    max_output_tokens: int = 8192  # 单次响应最大输出 token
    max_context_tokens: int = 128000  # 上下文窗口
    model_type: str = "reasoning"  # "reasoning" | "chat"：思考选项按此设置
    input: list[str] = field(default_factory=lambda: ["text", "image"])  # 输入模态，必须含 "text"
    reasoning: str = "default"  # 思考档位：default/off/low/medium/high（openai 另支持 none/minimal）
    cache_control: str = "auto"  # Prompt cache: "auto" (protocol default) | "true" (force) | "false" (disable)

    @classmethod
    def from_dict(cls, data: dict) -> "ModelConfig":
        """Parse ModelConfig from a dict."""
        return cls(
            name=data.get("name", ""),
            interface_type=data.get("interface_type", "anthropic"),
            api_key=data.get("api_key", ""),
            base_url=data.get("base_url", "https://api.anthropic.com").rstrip("/"),
            auth_type=data.get("auth_type", "auto"),
            backend=data.get("backend", "custom"),
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            max_output_tokens=int(data.get("max_output_tokens", data.get("max_tokens", 8192))),
            max_context_tokens=int(data.get("max_context_tokens", 128000)),
            model_type=data.get("model_type", "reasoning"),
            input=list(data.get("input") or ["text", "image"]),
            reasoning=data.get("reasoning", "default"),
            cache_control=data.get("cache_control", "auto"),
        )

    def to_dict(self) -> dict:
        """Convert to a serializable dict."""
        return {
            "name": self.name,
            "interface_type": self.interface_type,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "auth_type": self.auth_type,
            "backend": self.backend,
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
    max_output_tokens: int = 8192  # 单次响应最大输出 token
    max_context_tokens: int = 128000  # 上下文窗口
    model_type: str = "reasoning"  # "reasoning" | "chat"
    input: list[str] = field(default_factory=lambda: ["text", "image"])
    reasoning: str = "default"  # 取值域按 provider 的 interface_type 各自定义
    cache_control: str = "auto"

    @classmethod
    def from_dict(cls, data: dict) -> "ModelSpec":
        return cls(
            name=str(data.get("name", "")),
            max_output_tokens=int(data.get("max_output_tokens", data.get("max_tokens", 8192))),
            max_context_tokens=int(data.get("max_context_tokens", 128000)),
            model_type=data.get("model_type", "reasoning"),
            input=list(data.get("input") or ["text", "image"]),
            reasoning=data.get("reasoning", "default"),
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
    interface_type: str = "anthropic"  # "anthropic" | "openai"
    api_key: str = ""  # 支持 ${ENV_VAR} 整值插值
    auth_type: str = "auto"  # "auto" | "bearer" | "x-api-key" | "none"
    backend: str = "custom"  # "custom" | "codex" | "claude-code" | "opencode" | "openrouter" | "litellm"
    base_url: str = "https://api.anthropic.com"
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
            base_url=data.get("base_url", "https://api.anthropic.com").rstrip("/"),
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            models=models,
        )

    def to_dict(self) -> dict:
        return {
            "interface_type": self.interface_type,
            "api_key": self.api_key,
            "auth_type": self.auth_type,
            "backend": self.backend,
            "base_url": self.base_url,
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
class Config:
    """Global configuration.

    模型配置为两级结构：``providers``（凭据/接入层 + models 表）与 ``agents``
    （角色 → 模型引用字符串，如 ``"bg/main"``）。
    """
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    agents: dict[str, AgentConfig] = field(default_factory=dict)  # 键 = 角色名（master/worker/lite）
    system: SystemConfig = field(default_factory=SystemConfig)
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
            auth_type=provider.auth_type,
            backend=provider.backend,
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

    def role_refs(self) -> dict[str, str]:
        """角色名 → 模型引用；worker/lite 未配置时继承 master。"""
        master_ref = self.master_ref()
        return {
            "master": master_ref,
            "worker": (self.agents.get("worker") or AgentConfig()).model or master_ref,
            "lite": (self.agents.get("lite") or AgentConfig()).model or master_ref,
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

    @property
    def model(self) -> ModelConfig:
        """master 模型（运行时形态）。供少量旧调用点过渡使用。"""
        return self.resolve_model(self.master_ref())

    # ── 构建 ──

    @classmethod
    def from_global_config(cls, global_config: dict, model_override: str | None = None) -> "Config":
        """Build Config from raw global config dict with CLI/env overrides.

        旧格式（model/worker_model/lite_model 为对象）由 ``migrate_legacy_config``
        在此之前转换，此处只处理新格式。
        """
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

        config = cls(providers=providers, agents=agents)

        # ── env/CLI 覆盖：只作用于 master 的接入层/模型 ID，不改变模型归属 ──
        master_ref = config.master_ref()
        provider_key, _, model_key = master_ref.partition("/")
        provider = config.providers[provider_key]
        env_api_key = os.environ.get("ANTHROPIC_API_KEY")
        env_base_url = os.environ.get("ANTHROPIC_BASE_URL")
        env_model = model_override or os.environ.get("ANTHROPIC_MODEL")
        if env_api_key:
            provider.api_key = env_api_key
        if env_base_url:
            provider.base_url = env_base_url.rstrip("/")
        if env_model:
            provider.models[model_key].name = env_model

        if not provider.api_key:
            raise RuntimeError(
                f"No API key found. Please configure one of:\n"
                f"  1. Environment variable: ANTHROPIC_API_KEY\n"
                f"  2. Global config file: {GLOBAL_CONFIG_PATH}\n"
                f"\nYou can also configure via web UI or edit {GLOBAL_CONFIG_PATH} directly."
            )

        # ── System ──
        config.system = SystemConfig.from_dict(global_config.get("system", {}))

        # ── MCP servers ──
        config.mcp_servers = {
            name: MCPConfig.from_dict(cfg)
            for name, cfg in (global_config.get("mcp_servers") or {}).items()
            if isinstance(cfg, dict)
        }

        return config

    def to_dict(self) -> dict:
        """Convert to a serializable dict (for API response)。api_key 脱敏在路由层做。"""
        result: dict = {
            "providers": {name: cfg.to_dict() for name, cfg in self.providers.items()},
            "agents": {name: cfg.to_dict() for name, cfg in self.agents.items()},
            "system": self.system.to_dict(),
        }
        if self.mcp_servers:
            result["mcp_servers"] = {
                name: cfg.to_dict() for name, cfg in self.mcp_servers.items()
            }
        return result


# ── 旧格式迁移 ──

_LEGACY_MODEL_SPEC_EXCLUDE = {"name", "interface_type", "api_key", "base_url", "temperature", "multimodal", "reasoning_effort", "max_tokens", "max_context_tokens"}


def _convert_legacy_model_spec(obj: dict) -> dict:
    """旧角色模型对象 → 新 ModelSpec 字段（multimodal/reasoning_effort 转换，temperature 丢弃）。"""
    spec = {k: v for k, v in obj.items() if k not in _LEGACY_MODEL_SPEC_EXCLUDE}
    spec["name"] = obj.get("name", "")
    spec["max_output_tokens"] = int(obj.get("max_tokens", 8192))
    spec["max_context_tokens"] = int(obj.get("max_context_tokens", 128000))
    if "multimodal" in obj:
        spec["input"] = ["text", "image"] if obj.get("multimodal") else ["text"]
    effort = obj.get("reasoning_effort", "")
    spec["reasoning"] = effort if effort else "default"
    return spec


def _backup_global_config() -> Path:
    """备份 setting.json；返回备份路径。已存在 setting.json.bak 时用带时间戳后缀，不覆盖更早的回滚副本。"""
    backup = GLOBAL_CONFIG_PATH.with_suffix(".json.bak")
    if backup.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = GLOBAL_CONFIG_PATH.with_suffix(f".json.bak-{stamp}")
    shutil.copy2(GLOBAL_CONFIG_PATH, backup)
    return backup


def convert_legacy_config(global_config: dict) -> dict | None:
    """旧格式（model/worker_model/lite_model 为对象）→ 新格式 dict；非旧格式返回 None。

    纯函数，不碰文件系统。master 缺失时抛 RuntimeError。
    合成 provider 键为 ``default``（凭据不同时 ``default_2`` 起），backend 默认 litellm。
    """
    legacy_role_models = {
        role: global_config.get(top_key)
        for role, top_key in (("master", "model"), ("worker", "worker_model"), ("lite", "lite_model"))
    }
    if not any(isinstance(obj, dict) for obj in legacy_role_models.values()):
        return None

    master_obj = legacy_role_models.get("master")
    if not isinstance(master_obj, dict) or not master_obj.get("name"):
        raise RuntimeError("旧格式配置缺少 model.name（master 主模型），无法迁移；请补全后重试")

    providers_raw: dict[str, dict] = {}
    agents: dict[str, dict] = {}
    dropped_fields: list[str] = []

    for role, obj in legacy_role_models.items():
        if not isinstance(obj, dict) or not obj.get("name"):
            continue
        cred = (
            obj.get("interface_type", "anthropic"),
            obj.get("api_key", ""),
            (obj.get("base_url") or "https://api.anthropic.com").rstrip("/"),
        )
        provider_key = next(
            (key for key, raw in providers_raw.items() if raw["_cred"] == cred), None
        )
        if provider_key is None:
            provider_key = "default" if not providers_raw else f"default_{len(providers_raw) + 1}"
            providers_raw[provider_key] = {
                "_cred": cred,
                "interface_type": cred[0],
                "api_key": cred[1],
                "base_url": cred[2],
                "backend": "litellm",
                "headers": {},
                "auth_type": "auto",
                "models": {},
            }
        model_key = "main" if role == "master" else role
        while model_key in providers_raw[provider_key]["models"]:
            model_key += "_2"
        if "temperature" in obj:
            dropped_fields.append(f"{role}.temperature")
        providers_raw[provider_key]["models"][model_key] = _convert_legacy_model_spec(obj)
        agents[role] = {"model": f"{provider_key}/{model_key}"}

    providers: dict[str, dict] = {}
    for key, raw in providers_raw.items():
        raw.pop("_cred")
        providers[key] = raw

    migrated = {
        key: value for key, value in global_config.items()
        if key not in ("model", "worker_model", "lite_model")
    }
    migrated["providers"] = providers
    migrated["agents"] = agents
    if dropped_fields:
        migrated["_migrate_dropped"] = dropped_fields  # 供调用方日志，写盘前剥离
    return migrated


def migrate_legacy_config(global_config: dict) -> dict:
    """旧格式 → 新格式（备份 → 转换 → 原子写回）；新格式原样返回。幂等。

    仅在真实配置加载路径（load_config / get_config）调用；纯转换见 convert_legacy_config。
    """
    migrated = convert_legacy_config(global_config)
    if migrated is None:
        return global_config

    backup_path = _backup_global_config()
    dropped_fields = migrated.pop("_migrate_dropped", [])

    try:
        atomic_write_json(GLOBAL_CONFIG_PATH, migrated)
    except OSError as e:
        raise RuntimeError(f"迁移结果写回失败（原文件未动，备份在 {backup_path}）: {e}") from e

    dropped_note = f"；丢弃字段: {', '.join(dropped_fields)}" if dropped_fields else ""
    logger.info(
        f"[Config] 旧格式配置已迁移：providers={list(migrated.get('providers', {}))}，"
        f"agents={migrated.get('agents', {})}；备份: {backup_path}{dropped_note}"
    )
    return migrated


# Base directories (config.py is in core/, project root is one level up)
PROJECT_ROOT = Path(os.path.dirname(os.path.abspath(__file__))).parent
DATA_ROOT = PROJECT_ROOT / "data"
DATA_DIR = PROJECT_ROOT / "data" / "cili"

# Global config path: data/cili/setting.json
GLOBAL_CONFIG_PATH = DATA_DIR / "setting.json"

# Workspace index: data/cili/workspaces.json (all workspace metadata in one file)
WORKSPACES_JSON = DATA_DIR / "workspaces.json"

# System workspace data dir (directory = data/, data dir = data/.cili/)
SYSTEM_DATA_DIR = DATA_ROOT / ".cili"


def validate_workspace_name(name: str) -> str | None:
    """Validate workspace display name. Returns error message if invalid, None if valid.

    Rules:
    - 1-50 characters
    - No path separators or control characters
    """
    if not name:
        return "Workspace name is required"
    if len(name) > 50:
        return "Workspace name must be at most 50 characters"
    if any(c in name for c in ('/', '\\', '\x00', '\n', '\r')):
        return "Workspace name contains invalid characters"
    return None


def load_workspaces_index() -> list[dict]:
    """Load workspace index from data/cili/workspaces.json. Returns [] if missing."""
    if not WORKSPACES_JSON.exists():
        return []
    try:
        with open(WORKSPACES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        logger.warning(f"Invalid workspaces.json format (expected list): {WORKSPACES_JSON}")
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read {WORKSPACES_JSON}: {e}")
        return []


def save_workspaces_index(workspaces: list[dict]) -> bool:
    """Save workspace index to data/cili/workspaces.json (atomic write)."""
    try:
        atomic_write_json(WORKSPACES_JSON, workspaces)
        return True
    except OSError as e:
        logger.warning(f"Failed to save {WORKSPACES_JSON}: {e}")
        return False


def find_workspace_entry(workspace_uuid: str) -> dict | None:
    """Find a workspace entry by uuid in workspaces.json."""
    for entry in load_workspaces_index():
        if entry.get("uuid") == workspace_uuid:
            return entry
    return None


def upsert_workspace_entry(entry: dict) -> bool:
    """Add or update a workspace entry in workspaces.json (keyed by uuid)."""
    workspaces = load_workspaces_index()
    uuid = entry.get("uuid")
    for i, e in enumerate(workspaces):
        if e.get("uuid") == uuid:
            workspaces[i] = entry
            break
    else:
        workspaces.append(entry)
    return save_workspaces_index(workspaces)


def remove_workspace_entry(workspace_uuid: str) -> bool:
    """Remove a workspace entry from workspaces.json. Idempotent."""
    workspaces = load_workspaces_index()
    filtered = [e for e in workspaces if e.get("uuid") != workspace_uuid]
    if len(filtered) == len(workspaces):
        return True  # not found
    return save_workspaces_index(filtered)


def get_workspace_data_dir(workspace_uuid: str) -> Path:
    """Get the .cili data directory for a workspace: {directory}/.cili/.

    - 空 uuid：PROJECT_ROOT/workspace/.cili/
    - system：data/.cili/（directory = data/，找不到条目时兜底）
    - 其他：从 workspaces.json 读 directory，返回 {directory}/.cili/
    """
    if not workspace_uuid:
        return PROJECT_ROOT / "workspace" / ".cili"
    entry = find_workspace_entry(workspace_uuid)
    if entry and entry.get("directory"):
        return Path(entry["directory"]) / ".cili"
    if workspace_uuid == "system":
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
    return PROJECT_ROOT / "workspace" / ".cili"


def get_system_workspace_data_dir() -> Path:
    """Get the system workspace data directory: data/.cili/."""
    return SYSTEM_DATA_DIR


def get_workspace_tools_dir(workspace_uuid: str) -> Path:
    """Get the tools data directory for a workspace: {workspace}/.cili/tools/."""
    return get_workspace_data_dir(workspace_uuid) / "tools"


def get_workspace_cron_dir(workspace_uuid: str) -> Path:
    """Get the cron data directory for a workspace: {workspace}/.cili/cron.d/."""
    return get_workspace_data_dir(workspace_uuid) / "cron.d"


def get_all_workspace_uuids() -> list[str]:
    """Get all workspace UUIDs including 'system'."""
    uuids = ["system"]
    for entry in load_workspaces_index():
        uuid = entry.get("uuid")
        if uuid and uuid != "system":
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


def ensure_default_workspace() -> str | None:
    """确保存在名为 "Default" 的工作区，返回其 uuid（失败返回 None）。

    原先私有在 ``web/deps.py``；QQ 接入端启动时也要确定「当前工作区」的兜底，
    故下沉 core。幂等。
    """
    import secrets

    try:
        # 已存在 Default / legacy default → 直接返回（顺手迁移名字）
        for entry in load_workspaces_index():
            name = entry.get("workspace_name")
            uuid = entry.get("uuid")
            if name in ("Default", "default") and uuid:
                if name == "default":
                    entry["workspace_name"] = "Default"
                    save_workspace_config(uuid, entry)
                return uuid

        workspace_uuid = secrets.token_hex(4)
        ws_data_dir = get_workspace_data_dir(workspace_uuid)
        ws_data_dir.mkdir(parents=True, exist_ok=True)
        (ws_data_dir / "sessions").mkdir(exist_ok=True)

        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_workspace_config(workspace_uuid, {
            "workspace_name": "Default",
            "directory": str(PROJECT_ROOT / "workspace"),
            "created_at": now,
            "updated_at": now,
        })
        logger.info(f"[config] 已创建默认工作区: {workspace_uuid}")
        return workspace_uuid
    except (OSError, PermissionError) as e:
        logger.error(f"[config] 创建默认工作区失败: {e}")
        return None
