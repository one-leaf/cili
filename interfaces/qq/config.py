"""QQ 接入端配置：``data/cili/setting.json`` 的 ``"qq"`` 段。

``setting.json`` 是扁平 dict，直接复用 ``core.config`` 的
``load_global_config()`` / ``save_global_config()``，不需要新的存储层。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields

from core.config import load_global_config, save_global_config

logger = logging.getLogger(__name__)

CONFIG_SECTION = "qq"

# 与 Web 的 _MAX_UPLOAD_SIZE 保持一致（用户要求两边统一）
DEFAULT_MAX_INBOUND_BYTES = 100 * 1024 * 1024

# 单条用户消息的被动回复配额是 4；留 1 条给流式消息本身 → 出站文件最多 3 个。
# 实际上限还会被 ReplyLimiter 的剩余配额进一步压低。
DEFAULT_MAX_OUTBOUND_FILES = 3


_INVALID = object()   # 类型不合法的哨兵


def _coerce(type_name, value):
    """按字段声明的类型做宽松转换；无法转换返回 ``_INVALID``。

    ``from __future__ import annotations`` 生效时 ``field.type`` 是字符串，
    所以两种形态都要处理。
    """
    name = type_name if isinstance(type_name, str) else getattr(type_name, "__name__", "")
    if isinstance(value, bool) and name in ("int", "float"):
        return _INVALID            # True/False 不是合法的数字配置
    try:
        if name == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            if isinstance(value, (int, float)):
                return bool(value)
            return _INVALID
        if name == "int":
            return int(value)
        if name == "float":
            return float(value)
        if name == "str":
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return _INVALID
    return value


@dataclass
class QQConfig:
    """QQ 单聊接入端配置（仅私聊、仅单人使用）。"""

    enabled: bool = False
    app_id: str = ""
    secret: str = ""

    # 未过审机器人只能在沙箱收发，默认走沙箱更安全。
    # ⚠️ SDK 没有沙箱开关——见 interfaces/qq/service.py 里 import 前设 QQ_API_BASE。
    use_sandbox: bool = True

    # 单聊 markdown 已对所有机器人开放（无需申请模板），默认用它
    msg_format: str = "markdown"

    # 流式推送节流（对齐官方插件的 DeliverDebouncer：windowMs=1500 / maxWaitMs=8000）
    flush_interval_seconds: float = 1.5
    flush_max_wait_seconds: float = 8.0

    max_inbound_bytes: int = DEFAULT_MAX_INBOUND_BYTES
    max_outbound_files: int = DEFAULT_MAX_OUTBOUND_FILES

    @classmethod
    def from_dict(cls, data: dict | None) -> "QQConfig":
        """从 setting.json 的 "qq" 段构造；未知键忽略、缺失键取默认。

        逐项做类型校验：dataclass 不做强制转换，手工编辑的 JSON 里写错一个
        类型（如 ``"flush_interval_seconds": "abc"``）会一路流到节流比较里炸掉。
        非法项退回默认值并告警，不影响启动。
        """
        if not isinstance(data, dict):
            return cls()
        defaults = cls()
        kwargs: dict = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            coerced = _coerce(f.type, data[f.name])
            if coerced is _INVALID:
                logger.warning(
                    f"[qq] 配置项 {f.name}={data[f.name]!r} 类型不合法，改用默认值 "
                    f"{getattr(defaults, f.name)!r}"
                )
                continue
            kwargs[f.name] = coerced
        return cls(**kwargs)

    def to_dict(self) -> dict:
        return asdict(self)

    def is_ready(self) -> bool:
        """凭据是否齐全（不齐全时启动只记日志、不报错）。"""
        return bool(self.app_id and self.secret)


def load_qq_config() -> QQConfig:
    """读取全局配置里的 qq 段（缺失时返回默认配置）。"""
    return QQConfig.from_dict(load_global_config().get(CONFIG_SECTION))


def save_qq_config(cfg: QQConfig) -> bool:
    """把 qq 段写回全局配置（保留其它段不动）。"""
    global_config = load_global_config()
    global_config[CONFIG_SECTION] = cfg.to_dict()
    return save_global_config(global_config)
