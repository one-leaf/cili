"""Configuration loader - global config with multi-model support"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

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
        }


@dataclass
class SystemConfig:
    """System-level configuration (pip mirror, env paths, etc.)."""
    pip_mirror: str = "https://mirrors.aliyun.com/pypi/simple/"
    allowed_ips: list[str] = field(default_factory=list)  # IP whitelist; empty = localhost only
    browser_path: str = ""  # Browser executable path (auto-detected if empty or invalid)
    search_engine: str = "bing"  # Web search engine: "bing" or "google"

    @classmethod
    def from_dict(cls, data: dict) -> "SystemConfig":
        """Parse SystemConfig from a dict."""
        return cls(
            pip_mirror=data.get("pip_mirror", "https://mirrors.aliyun.com/pypi/simple/"),
            allowed_ips=data.get("allowed_ips", []),
            browser_path=data.get("browser_path", ""),
            search_engine=data.get("search_engine", "bing"),
        )

    def to_dict(self) -> dict:
        """Convert to a serializable dict."""
        return {
            "pip_mirror": self.pip_mirror,
            "allowed_ips": self.allowed_ips,
            "browser_path": self.browser_path,
            "search_engine": self.search_engine,
        }


@dataclass
class Config:
    """Global configuration."""
    model: ModelConfig          # RootAgent model: multi-turn conversation for root and sub-agent
    llm_model: ModelConfig | None = None  # LLM model: single-turn for llm_tool (optional)
    system: SystemConfig = field(default_factory=SystemConfig)  # System parameters

    @classmethod
    def from_global_config(cls, global_config: dict, model_override: str | None = None) -> "Config":
        """Build Config from raw global config dict with CLI/env overrides."""
        # ── RootAgent model ──
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

        # ── LLM model (optional) ──
        llm_model = None
        llm_model_data = global_config.get("llm_model", {})
        if llm_model_data and llm_model_data.get("name"):
            llm_api_key = llm_model_data.get("api_key") or api_key
            if llm_api_key:
                llm_model = ModelConfig.from_dict({
                    **llm_model_data,
                    "api_key": llm_api_key,  # Inject fallback key
                })
                # bg114 endpoint forces model name to "dgx"
                if "bg114" in llm_model.base_url:
                    llm_model.name = "dgx"

        # ── System ──
        system = SystemConfig.from_dict(global_config.get("system", {}))

        return cls(model=model, llm_model=llm_model, system=system)

    def to_dict(self) -> dict:
        """Convert to a serializable dict (for API response)."""
        result = {
            "model": self.model.to_dict(),
            "system": self.system.to_dict(),
        }
        if self.llm_model:
            result["llm_model"] = self.llm_model.to_dict()
        return result


# Base directories (config.py is in core/, project root is one level up)
PROJECT_ROOT = Path(os.path.dirname(os.path.abspath(__file__))).parent
DATA_DIR = PROJECT_ROOT / "data" / "cili"
AGENTS_DIR = PROJECT_ROOT / "data" / "agents"

# Global config path: data/cili/setting.json
GLOBAL_CONFIG_PATH = DATA_DIR / "setting.json"


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


def get_workspace_data_dir(workspace_uuid: str) -> Path:
    """Get the data directory for a workspace: data/agents/{uuid}/ or workspace/ if empty."""
    if not workspace_uuid:
        return PROJECT_ROOT / "workspace"
    return AGENTS_DIR / workspace_uuid


def get_workspace_config_path(workspace_uuid: str) -> Path:
    """Get the workspace config path: data/agents/{uuid}/setting.json or workspace/setting.json if empty."""
    if not workspace_uuid:
        return PROJECT_ROOT / "workspace" / "setting.json"
    return AGENTS_DIR / workspace_uuid / "setting.json"


def get_user_profile_path(workspace_uuid: str) -> Path:
    """Get the user profile path: data/agents/{uuid}/user-profile.json or workspace/user-profile.json if empty."""
    if not workspace_uuid:
        return PROJECT_ROOT / "workspace" / "user-profile.json"
    return AGENTS_DIR / workspace_uuid / "user-profile.json"


def load_global_config() -> dict:
    """Load global model configuration from data/cili/setting.json.

    Returns empty dict if file doesn't exist or can't be read.
    """
    if not GLOBAL_CONFIG_PATH.exists():
        return {}

    try:
        with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read {GLOBAL_CONFIG_PATH}: {e}")
        return {}


def save_global_config(config: dict) -> bool:
    """Save global model configuration to data/cili/setting.json."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(GLOBAL_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except OSError as e:
        logger.warning(f"Failed to save {GLOBAL_CONFIG_PATH}: {e}")
        return False


def load_config(model_override: str | None = None) -> Config:
    """Load config with priority: CLI override > env vars > global config > defaults."""
    global_config = load_global_config()
    return Config.from_global_config(global_config, model_override)


def load_workspace_config(workspace_uuid: str) -> dict:
    """Load workspace metadata from data/agents/{uuid}/setting.json.

    Returns dict with workspace_name, directory, created_at, updated_at.
    """
    config_path = get_workspace_config_path(workspace_uuid)
    if not config_path.exists():
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read {config_path}: {e}")
        return {}


def save_workspace_config(workspace_uuid: str, config: dict) -> bool:
    """Save workspace metadata to data/agents/{uuid}/setting.json."""
    config_path = get_workspace_config_path(workspace_uuid)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except OSError as e:
        logger.warning(f"Failed to save {config_path}: {e}")
        return False
