"""微信接入端配置：``data/cili/setting.json`` 的 ``"wechat"`` 段。

**多账号模型**（设计 §3.2）：``"wechat"`` 段是 ``{"accounts": [...]}``，
每个元素一个账号。``WechatConfig`` 既是账号配置也是**运行参数对象**——
预算、切分上限、协议常量这些调参字段保留在类里当默认值，但 ``from_dict``
**不再从 setting.json 读取**（§3.2.1），outbound / sink / typing / client
里 ``cfg.xxx`` 的读法因此一处不改。

**凭证不在这里**：``bot_token`` 等来自扫码登录，存
``data/cili/tools/wechat/accounts/{account_id}.json``（见 account.py）。
所以本段可以安全地经 Web 配置接口往返，不会泄露任何密钥。

**兼容读**：旧扁平段（字段直接铺在段顶层）按「整段就是一个账号」解析，
id 取旧单文件凭证里的 ``ilink_bot_id``（见 :func:`legacy_compat_account_id`）。
写函数只写新形态。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields

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

# —— 以下均为固定默认值的运行参数（§3.2.1），不从 setting.json 读取 ——

# 「正在输入…」+ 工具进度节拍是微信侧的进度反馈——iLink 不支持更新已发消息，
# 所以 QQ 那种流式 transcript 在这里做不出来（见 sink.py）。
DEFAULT_TYPING_ENABLED = True
DEFAULT_TYPING_KEEPALIVE_SECONDS = 5.0

# 工具进度节拍：累计 1/2/4/8/16/32/64 次工具调用时各发一条进度消息，
# 正文最后一次性发出。进度与正文共享 context_token 预算（见 sink.py）。
DEFAULT_TOOL_PROGRESS = True

# context_token 的发送预算与刷新阈值
DEFAULT_CONTEXT_TOKEN_MAX_AGE_SECONDS = 60.0
DEFAULT_DEFER_QUEUE_MAX = 3

# from_dict / to_dict 的字段白名单：只有这些进配置文件
_PERSIST_FIELDS = (
    "id",
    "label",
    "enabled",
    "default_workspace_uuid",
    "workspace_locked",
)

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
    """单个微信账号的配置 + 运行参数（§3.2 / §3.2.1）。

    ``enabled`` 等白名单字段进配置文件；其余全部是固定默认值的运行参数
    （iLink 协议常量、预算、切分上限），不进配置文件。
    """

    # —— 账号身份与开关（进配置文件） ——
    id: str = ""
    label: str = ""
    enabled: bool = False
    default_workspace_uuid: str = ""
    workspace_locked: bool = False

    # —— 运行参数（固定默认值，不进配置文件） ——
    typing_enabled: bool = DEFAULT_TYPING_ENABLED
    typing_keepalive_seconds: float = DEFAULT_TYPING_KEEPALIVE_SECONDS
    tool_progress: bool = DEFAULT_TOOL_PROGRESS
    context_message_budget: int = DEFAULT_CONTEXT_MESSAGE_BUDGET
    context_token_max_age_seconds: float = DEFAULT_CONTEXT_TOKEN_MAX_AGE_SECONDS
    defer_queue_max: int = DEFAULT_DEFER_QUEUE_MAX
    text_chunk_limit: int = DEFAULT_TEXT_CHUNK_LIMIT
    max_inbound_bytes: int = DEFAULT_MAX_INBOUND_BYTES
    max_outbound_files: int = DEFAULT_MAX_OUTBOUND_FILES
    base_url: str = DEFAULT_BASE_URL
    cdn_base: str = DEFAULT_CDN_BASE
    client_version: str = DEFAULT_CLIENT_VERSION

    @classmethod
    def from_dict(cls, data: dict | None) -> "WechatConfig":
        """从账号字典构造；只读白名单字段，其余（含旧调参键）一律忽略。"""
        if not isinstance(data, dict):
            return cls()
        types = {f.name: f.type for f in fields(cls)}
        kwargs: dict = {}
        for name in _PERSIST_FIELDS:
            if name not in data:
                continue
            coerced = _coerce(types.get(name, "str"), data[name])
            if coerced is _INVALID:
                logger.warning(
                    f"[wechat] 配置项 {name}={data[name]!r} 类型不合法，改用默认值"
                )
                continue
            kwargs[name] = coerced
        return cls(**kwargs)

    def to_dict(self) -> dict:
        """只序列化白名单字段（运行参数不落盘）。"""
        return {name: getattr(self, name) for name in _PERSIST_FIELDS}


def legacy_compat_account_id() -> str:
    """旧扁平段时代的账号标识（兼容读用）。

    取旧单文件凭证里的 ``ilink_bot_id``（回退 ``ilink_user_id``、再回退
    ``"default"``）——与 core.migration 的迁移口径一致，保证「兼容读期间
    写下的状态」和「迁移后的账号键」是同一个。迁移通常在启动期已完成，
    这里只是兜底。
    """
    from interfaces.wechat.account import legacy_account_id

    return legacy_account_id()


def load_wechat_accounts() -> list[WechatConfig]:
    """读取账号列表（兼容旧扁平段：整段视为一个账号）。"""
    section = load_global_config().get(CONFIG_SECTION)
    if not isinstance(section, dict):
        return []
    accounts_raw = section.get("accounts")
    if isinstance(accounts_raw, list):
        out: list[WechatConfig] = []
        for item in accounts_raw:
            if not isinstance(item, dict):
                continue
            cfg = WechatConfig.from_dict(item)
            if not cfg.id:
                cfg.id = legacy_compat_account_id()
            out.append(cfg)
        return out
    if section:
        # 旧扁平段：整段就是一个账号（调参键被忽略，退回默认值）
        cfg = WechatConfig.from_dict(section)
        if not cfg.id:
            cfg.id = legacy_compat_account_id()
        return [cfg]
    return []


def save_wechat_accounts(accounts: list[WechatConfig]) -> bool:
    """把账号列表写回全局配置（保留其它段不动；只写白名单字段）。"""
    global_config = load_global_config()
    global_config[CONFIG_SECTION] = {"accounts": [a.to_dict() for a in accounts]}
    return save_global_config(global_config)


def load_wechat_config() -> WechatConfig:
    """兼容入口：第一个 enabled 账号；无则返回默认（未启用）。"""
    for cfg in load_wechat_accounts():
        if cfg.enabled:
            return cfg
    return WechatConfig()


def save_wechat_config(cfg: WechatConfig) -> bool:
    """兼容入口：按 id 替换（无此 id 则追加）。"""
    accounts = load_wechat_accounts()
    for i, a in enumerate(accounts):
        if a.id == cfg.id:
            accounts[i] = cfg
            break
    else:
        accounts.append(cfg)
    return save_wechat_accounts(accounts)
