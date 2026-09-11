"""Agent role configuration - load core/agents/{role}.json into AgentRoleConfig."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from core.config import Config, PROJECT_ROOT

logger = logging.getLogger(__name__)

# Role definitions live in core/agents/{role}.json (system-level, checked in)
ROLE_DIR = PROJECT_ROOT / "core" / "agents"

VALID_MODES = ("interactive", "autonomous")

# Defaults applied to any role field not present in its JSON
_DEFAULTS = {
    "label": "",
    "mode": "autonomous",
    "tools": [],
    "deferred_tools": [],
    "skills": [],
    "streaming": True,
    "ask_user": False,
    "approval": False,
    "session_persistence": False,
    "check_phase": False,
    "budget_notice": False,
    "progress_persistence": False,
    "max_iterations": None,  # None -> config.system.max_iterations
    "max_consecutive_failures": 5,
    "max_tokens": None,  # None -> 继承角色模型的 max_tokens
    "system_prompt": {},
    "user_layers": [],
    "mcp": False,  # 是否注入已配置的 MCP 服务器工具
}


@dataclass
class AgentRoleConfig:
    """A single agent role loaded from core/agents/{role}.json."""

    name: str
    label: str = ""
    mode: str = "autonomous"  # "interactive" | "autonomous"
    tools: list[str] = field(default_factory=list)  # tool name whitelist
    deferred_tools: list[str] = field(default_factory=list)  # tools whose schemas are hidden until activated
    skills: list[str] = field(default_factory=list)  # [] or ["*"] = all visible
    streaming: bool = True
    ask_user: bool = False
    approval: bool = False
    session_persistence: bool = False
    check_phase: bool = False
    budget_notice: bool = False
    progress_persistence: bool = False
    max_iterations: int | None = None  # None -> config.system.max_iterations
    max_consecutive_failures: int = 5
    max_tokens: int | None = None  # None -> 继承角色模型的 max_tokens；否则覆盖
    system_prompt: dict = field(default_factory=dict)  # {"blocks": [...]}
    user_layers: list[dict] = field(default_factory=list)
    mcp: bool = False  # 是否注入已配置的 MCP 服务器工具


def _role_file(role: str) -> Path:
    return ROLE_DIR / f"{role}.json"


def list_roles() -> list[str]:
    """Return available role names from core/agents/*.json."""
    if not ROLE_DIR.is_dir():
        return []
    return sorted(p.stem for p in ROLE_DIR.glob("*.json"))


def load_agent_role(role: str, config: Config | None = None) -> AgentRoleConfig:
    """Load role definition from core/agents/{role}.json with defaults.

    ``max_iterations`` falls back to ``config.system.max_iterations`` when
    unset (or when no Config is provided).
    """
    path = _role_file(role)
    if not path.is_file():
        raise ValueError(f"Unknown agent role: {role!r} (no {path})")

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"Failed to load agent role {role!r}: {e}") from e

    merged = {**_DEFAULTS, **data}
    if merged["name"] != role:
        logger.warning(f"[agent_config] role file {path} name={merged['name']!r} != requested {role!r}")
    if merged["mode"] not in VALID_MODES:
        raise ValueError(f"Role {role!r} has invalid mode {merged['mode']!r} (valid: {VALID_MODES})")

    role_cfg = AgentRoleConfig(
        name=role,
        label=merged["label"],
        mode=merged["mode"],
        tools=list(merged["tools"]),
        deferred_tools=list(merged["deferred_tools"]),
        skills=list(merged["skills"]),
        streaming=bool(merged["streaming"]),
        ask_user=bool(merged["ask_user"]),
        approval=bool(merged["approval"]),
        session_persistence=bool(merged["session_persistence"]),
        check_phase=bool(merged["check_phase"]),
        budget_notice=bool(merged["budget_notice"]),
        progress_persistence=bool(merged["progress_persistence"]),
        max_iterations=merged["max_iterations"],
        max_consecutive_failures=int(merged["max_consecutive_failures"]),
        max_tokens=int(merged["max_tokens"]) if merged["max_tokens"] is not None else None,
        system_prompt=merged["system_prompt"] or {},
        user_layers=list(merged["user_layers"]),
        mcp=bool(merged["mcp"]),
    )

    if role_cfg.max_iterations is None:
        role_cfg.max_iterations = config.system.max_iterations if config else 200

    return role_cfg
