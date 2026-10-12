"""微信接入端配置：``data/cili/setting.json`` 的 ``"wechat"`` 段。

``setting.json`` 是扁平 dict，直接复用 ``core.config`` 的
``load_global_config()`` / ``save_global_config()``，不需要新的存储层。

**凭证不在这里**：``bot_token`` / ``base_url`` 等来自扫码登录，存在
``data/cili/tools/wechat/account.json``（见 ``interfaces/wechat/account.py``）。
所以本段可以安全地经 Web 配置接口往返，不会泄露任何密钥。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields

from core.config import load_global_config, save_global_config

logger = logging.getLogger(__name__)

CONFIG_SECTION = "wechat"

# 与 Web 的 _MAX_UPLOAD_SIZE 保持一致（用户要求两边统一）
DEFAULT_MAX_INBOUND_BYTES = 100 * 1024 * 1024

# 一个 context_token 的发送预算约 8 条（服务端未公开，社区经验值）。
# 文本分片与出站文件**共享**这份预算，所以出站文件不能给太多。
DEFAULT_CONTEXT_MESSAGE_BUDGET = 8
DEFAULT_MAX_OUTBOUND_FILES = 3

# iLink 协议常量（可覆盖：协议未公开，字段与域名都可能变）
DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_CLIENT_VERSION = "2.4.6"

# 单条文本上限：iLink 的硬限制是 1800 字符
DEFAULT_TEXT_CHUNK_LIMIT = 1800


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
class WechatConfig:
    """微信单聊接入端配置（仅私聊、仅单人使用）。"""

    enabled: bool = False

    # 「正在输入…」是微信侧唯一的进度反馈——iLink 不支持更新已发消息，
    # 所以 QQ 那种流式 transcript 在这里做不出来（见 sink.py）。
    typing_enabled: bool = True
    typing_keepalive_seconds: float = 5.0

    # context_token 的发送预算与刷新阈值
    context_message_budget: int = DEFAULT_CONTEXT_MESSAGE_BUDGET
    context_token_max_age_seconds: float = 60.0
    defer_queue_max: int = 3

    text_chunk_limit: int = DEFAULT_TEXT_CHUNK_LIMIT
    max_inbound_bytes: int = DEFAULT_MAX_INBOUND_BYTES
    max_outbound_files: int = DEFAULT_MAX_OUTBOUND_FILES

    base_url: str = DEFAULT_BASE_URL
    cdn_base: str = DEFAULT_CDN_BASE
    client_version: str = DEFAULT_CLIENT_VERSION

    @classmethod
    def from_dict(cls, data: dict | None) -> "WechatConfig":
        """从 setting.json 的 "wechat" 段构造；未知键忽略、缺失键取默认。

        逐项做类型校验：dataclass 不做强制转换，手工编辑的 JSON 里写错一个
        类型（如 ``"text_chunk_limit": "abc"``）会一路流到切分比较里炸掉。
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
                    f"[wechat] 配置项 {f.name}={data[f.name]!r} 类型不合法，改用默认值 "
                    f"{getattr(defaults, f.name)!r}"
                )
                continue
            kwargs[f.name] = coerced
        return cls(**kwargs)

    def to_dict(self) -> dict:
        return asdict(self)

    def is_ready(self) -> bool:
        """**配置**是否可用（base_url 没配好一切都不通）。

        注意这与 QQ 的 ``is_ready()`` 语义不同：微信的凭证来自扫码、不在配置里，
        所以「能不能启动」还要看账号是否已登录——那由 ``account.has_token()``
        判定，见 ``service.start_wechat_interface()``。
        """
        return bool(self.base_url)


def load_wechat_config() -> WechatConfig:
    """读取全局配置里的 wechat 段（缺失时返回默认配置）。"""
    return WechatConfig.from_dict(load_global_config().get(CONFIG_SECTION))


def save_wechat_config(cfg: WechatConfig) -> bool:
    """把 wechat 段写回全局配置（保留其它段不动）。"""
    global_config = load_global_config()
    global_config[CONFIG_SECTION] = cfg.to_dict()
    return save_global_config(global_config)
