"""Configuration loader - global config with multi-model support"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from dataclasses import dataclass, field
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
    """Configuration for a single LLM model."""
    name: str
    interface_type: str = "anthropic"  # "anthropic" | "openai"
    api_key: str = ""
    base_url: str = "https://api.anthropic.com"
    max_tokens: int = 16384
    max_context_tokens: int = 256000
    multimodal: bool = True  # Whether this model supports image input
    temperature: float = 0.2  # 0.1(古板) ~ 1.0(跳脱)
    reasoning_effort: str = ""  # Reasoning effort for reasoning models: "low" | "medium" | "high" (empty = API default)
    cache_control: str = "auto"  # Prompt cache: "auto" (protocol default) | "true" (force) | "false" (disable)

    @classmethod
    def from_dict(cls, data: dict) -> "ModelConfig":
        """Parse ModelConfig from a dict."""
        return cls(
            name=data.get("name", ""),
            interface_type=data.get("interface_type", "anthropic"),
            api_key=data.get("api_key", ""),
            base_url=data.get("base_url", "https://api.anthropic.com").rstrip("/"),
            max_tokens=int(data.get("max_tokens", 16384)),
            max_context_tokens=int(data.get("max_context_tokens", 256000)),
            multimodal=bool(data.get("multimodal", True)),
            temperature=float(data.get("temperature", 0.2)),
            reasoning_effort=data.get("reasoning_effort", ""),
            cache_control=data.get("cache_control", "auto"),
        )

    def to_dict(self) -> dict:
        """Convert to a serializable dict."""
        return {
            "name": self.name,
            "interface_type": self.interface_type,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "max_tokens": self.max_tokens,
            "max_context_tokens": self.max_context_tokens,
            "multimodal": self.multimodal,
            "temperature": self.temperature,
            "reasoning_effort": self.reasoning_effort,
            "cache_control": self.cache_control,
        }

    def merged_with(self, override: dict) -> "ModelConfig":
        """Return a copy with fields present in *override* applied on top.

        Used for worker/lite model inheritance: an override with only a subset
        of fields (e.g. just ``{"name": "x"}``) keeps all other fields from
        this (master) model.
        """
        data = self.to_dict()
        for key, value in override.items():
            if key in data:
                data[key] = value
        return ModelConfig.from_dict(data)


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
    """Global configuration."""
    model: ModelConfig          # Master (main) model: multi-turn conversation for all agents
    worker_model: ModelConfig | None = None  # Worker model; None = inherit master model
    lite_model: ModelConfig | None = None    # Lite model; None = inherit master model
    system: SystemConfig = field(default_factory=SystemConfig)  # System parameters
    mcp_servers: dict[str, MCPConfig] = field(default_factory=dict)  # MCP 服务器配置

    @property
    def role_models(self) -> dict[str, ModelConfig]:
        """角色名 → 模型配置的显式映射（未单独配置的角色回退 master 模型）。"""
        return {
            "master": self.model,
            "worker": self.worker_model or self.model,
            "lite": self.lite_model or self.model,
        }

    def model_for_role(self, role: str) -> ModelConfig:
        """取角色模型；未知角色回退 master 模型并告警。

        取代原先 `getattr(config, f"{role}_model", None) or config.model` 的静默回退
        （角色名拼错会无提示地使用 master 模型）。
        """
        models = self.role_models
        if role in models:
            return models[role]
        logger.warning(
            f"[Config] 未知角色 {role!r}，回退 master 模型；"
            f"如需独立模型请在 setting.json 中配置 {role}_model"
        )
        return self.model

    @classmethod
    def from_global_config(cls, global_config: dict, model_override: str | None = None) -> "Config":
        """Build Config from raw global config dict with CLI/env overrides."""
        # ── Master model ──
        model_data = global_config.get("model", {})

        api_key = (
            os.environ.get("ANTHROPIC_API_KEY")
            or model_data.get("api_key")
            or ""
        )
        base_url = (
            os.environ.get("ANTHROPIC_BASE_URL")
            or model_data.get("base_url")
            or "https://api.anthropic.com"
        )
        model_name = (
            model_override
            or os.environ.get("ANTHROPIC_MODEL")
            or model_data.get("name")
            or "claude-sonnet-4-6"
        )

        if not api_key:
            raise RuntimeError(
                f"No API key found. Please configure one of:\n"
                f"  1. Environment variable: ANTHROPIC_API_KEY\n"
                f"  2. Global config file: {GLOBAL_CONFIG_PATH}\n"
                f"\nYou can also configure via web UI or edit {GLOBAL_CONFIG_PATH} directly."
            )

        # Use from_dict with env-var overrides applied on top
        model = ModelConfig.from_dict({
            **model_data,
            "name": model_name,
            "api_key": api_key,
            "base_url": base_url,
        })

        # ── Worker/Lite model (optional; inherit master when unset) ──
        worker_model = cls._parse_role_model(global_config, "worker", model)
        lite_model = cls._parse_role_model(global_config, "lite", model)

        # ── System ──
        system = SystemConfig.from_dict(global_config.get("system", {}))

        # ── MCP servers ──
        mcp_servers = {
            name: MCPConfig.from_dict(cfg)
            for name, cfg in (global_config.get("mcp_servers") or {}).items()
            if isinstance(cfg, dict)
        }

        return cls(
            model=model,
            worker_model=worker_model,
            lite_model=lite_model,
            system=system,
            mcp_servers=mcp_servers,
        )

    @staticmethod
    def _parse_role_model(global_config: dict, role: str, base_model: ModelConfig) -> ModelConfig | None:
        """Parse ``{role}_model`` config; returns None when unset or missing name."""
        data = global_config.get(f"{role}_model", {})
        if not data or not data.get("name"):
            return None
        return base_model.merged_with(data)

    def to_dict(self) -> dict:
        """Convert to a serializable dict (for API response)."""
        result = {
            "model": self.model.to_dict(),
            "system": self.system.to_dict(),
        }
        if self.worker_model:
            result["worker_model"] = self.worker_model.to_dict()
        if self.lite_model:
            result["lite_model"] = self.lite_model.to_dict()
        if self.mcp_servers:
            result["mcp_servers"] = {
                name: cfg.to_dict() for name, cfg in self.mcp_servers.items()
            }
        return result


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
    """Load config with priority: CLI override > env vars > global config > defaults."""
    global_config = load_global_config()
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
