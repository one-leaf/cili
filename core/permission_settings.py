"""权限配置 schema：setting.json → permissions（不放进 system 块）。"""

from __future__ import annotations

from dataclasses import dataclass, field


def _as_bool(value, default: bool) -> bool:
    """宽松布尔解析：字符串 "false"/"0"/"no"/"off" 视为 False。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("false", "0", "no", "off", ""):
            return False
        if text in ("true", "1", "yes", "on"):
            return True
    return bool(value)


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_permission_config(config) -> "PermissionConfig":
    """从运行时 Config 取顶层权限配置；缺省/测试替身场景回退默认值。"""
    permissions = getattr(config, "permissions", None)
    if isinstance(permissions, PermissionConfig):
        return permissions
    return PermissionConfig()


@dataclass
class OutsideConfig:
    """工作区外访问策略（权限配置的 outside 子块）。"""

    read: str = "allow"      # allow | ai | ask | deny
    write: str = "ask"       # allow | ai | ask | deny
    delete: str = "ask"      # allow | ai | ask | deny
    navigate: str = "ai"     # allow | ai | ask | deny

    _CHOICES = ("allow", "ai", "ask", "deny")

    @classmethod
    def _choice(cls, data: dict, key: str, default: str) -> str:
        value = str(data.get(key, default))
        return value if value in cls._CHOICES else default

    @classmethod
    def from_dict(cls, data: dict) -> "OutsideConfig":
        data = data if isinstance(data, dict) else {}
        return cls(
            read=cls._choice(data, "read", "allow"),
            write=cls._choice(data, "write", "ask"),
            delete=cls._choice(data, "delete", "ask"),
            navigate=cls._choice(data, "navigate", "ai"),
        )

    def to_dict(self) -> dict:
        return {
            "read": self.read,
            "write": self.write,
            "delete": self.delete,
            "navigate": self.navigate,
        }


@dataclass
class JudgeConfig:
    """AI 判定器参数（权限配置的 judge 子块）。"""

    enabled: bool = True
    timeout_seconds: int = 60
    on_error: str = "ask"  # ask | deny | allow
    persist_allow: bool = True
    cache: str = "session"  # session | none
    max_consecutive_failures: int = 3

    @classmethod
    def from_dict(cls, data: dict) -> "JudgeConfig":
        data = data if isinstance(data, dict) else {}
        on_error = str(data.get("on_error", "ask"))
        if on_error not in ("ask", "deny", "allow"):
            on_error = "ask"
        cache = str(data.get("cache", "session"))
        if cache not in ("session", "none"):
            cache = "session"
        return cls(
            enabled=_as_bool(data.get("enabled"), True),
            timeout_seconds=max(5, _as_int(data.get("timeout_seconds"), 60)),
            on_error=on_error,
            persist_allow=_as_bool(data.get("persist_allow"), True),
            cache=cache,
            max_consecutive_failures=max(1, _as_int(data.get("max_consecutive_failures"), 3)),
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "timeout_seconds": self.timeout_seconds,
            "on_error": self.on_error,
            "persist_allow": self.persist_allow,
            "cache": self.cache,
            "max_consecutive_failures": self.max_consecutive_failures,
        }


@dataclass
class ApprovalConfig:
    """审批卡生命周期参数（权限配置的 approval 子块）。

    - ``timeout_seconds``：卡片存活时长；0 = 不自动过期（兼容/调试）。
    - ``auto_resume``：超时后是否自动恢复循环。``web`` 只对 Web 恢复；
      ``all`` 对有服务端推送通道的接口（Web/TUI）恢复；``none`` 一律只结算。
      IM 无入站消息时送不出去，因此不参与自动恢复。
    """

    timeout_seconds: int = 300
    auto_resume: str = "web"  # web | all | none

    _AUTO_RESUME = ("web", "all", "none")
    _MIN_TIMEOUT = 30
    _MAX_TIMEOUT = 86400

    @classmethod
    def from_dict(cls, data: dict) -> "ApprovalConfig":
        data = data if isinstance(data, dict) else {}
        timeout = _as_int(data.get("timeout_seconds"), 300)
        if timeout < 0:
            timeout = 300
        if timeout > 0:
            timeout = max(cls._MIN_TIMEOUT, min(cls._MAX_TIMEOUT, timeout))
        auto_resume = str(data.get("auto_resume", "web"))
        if auto_resume not in cls._AUTO_RESUME:
            auto_resume = "web"
        return cls(timeout_seconds=timeout, auto_resume=auto_resume)

    def to_dict(self) -> dict:
        return {
            "timeout_seconds": self.timeout_seconds,
            "auto_resume": self.auto_resume,
        }


@dataclass
class PermissionConfig:
    """顶层权限配置：``setting.json → permissions``（不放进 system 块）。

    默认值按日常可用性设计：外部读取允许、外部写/删询问、默认策略默认允许、
    只读白名单开启。完整设计见 workspace/default/permission-settings-plan-2026-10-26.md。
    """

    enabled: bool = True
    default_mode: str = "allow"  # allow | ai | ask
    readonly_bypass: bool = True
    outside: OutsideConfig = field(default_factory=OutsideConfig)
    rules_hot_reload: bool = True
    global_rules_path: str = ""  # 空 = data/cili/approval_rules.json
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "PermissionConfig":
        data = data if isinstance(data, dict) else {}
        mode = str(data.get("default_mode", "allow"))
        if mode not in ("allow", "ai", "ask"):
            mode = "allow"
        rules_path = data.get("global_rules_path")
        return cls(
            enabled=_as_bool(data.get("enabled"), True),
            default_mode=mode,
            readonly_bypass=_as_bool(data.get("readonly_bypass"), True),
            outside=OutsideConfig.from_dict(data.get("outside") or {}),
            rules_hot_reload=_as_bool(data.get("rules_hot_reload"), True),
            global_rules_path="" if rules_path is None else str(rules_path),
            judge=JudgeConfig.from_dict(data.get("judge") or {}),
            approval=ApprovalConfig.from_dict(data.get("approval") or {}),
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "default_mode": self.default_mode,
            "readonly_bypass": self.readonly_bypass,
            "outside": self.outside.to_dict(),
            "rules_hot_reload": self.rules_hot_reload,
            "global_rules_path": self.global_rules_path,
            "judge": self.judge.to_dict(),
            "approval": self.approval.to_dict(),
        }
