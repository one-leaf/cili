"""QQ 接入端配置：``data/cili/setting.json`` 的 ``"qq"`` 段。

**多账号模型**（设计 §3.2）：``"qq"`` 段是 ``{"accounts": [...]}``，每个元素
一个账号。``QQConfig`` 既是账号配置也是**运行参数对象**——节流、上限这类
调参字段保留在类里当默认值，但 ``from_dict`` **不再从 setting.json 读取**
（§3.2.1），sink / inbound / media 里 ``cfg.xxx`` 的读法因此一处不改，而
「值只有一个来源」由「不读配置」保证。

**兼容读**：旧扁平段（字段直接铺在段顶层）按「整段就是一个账号」解析，
id 取 ``app_id``（§3.1）。写函数只写新形态。

API 域名不在这里：它是进程级资源（SDK 在 import 时求值），默认沙箱，用
环境变量 ``QQ_API_BASE`` 覆盖（见 ``gateway.py``）。
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, fields

from core.config import load_global_config, save_global_config

logger = logging.getLogger(__name__)

CONFIG_SECTION = "qq"

# 与 Web 的 _MAX_UPLOAD_SIZE 保持一致（用户要求两边统一）
DEFAULT_MAX_INBOUND_BYTES = 100 * 1024 * 1024

# 单条用户消息的被动回复配额是 4；留 1 条给流式消息本身 → 出站文件最多 3 个。
# 实际上限还会被 ReplyLimiter 的剩余配额进一步压低。
DEFAULT_MAX_OUTBOUND_FILES = 3

# —— 以下为固定默认值的运行参数（§3.2.1），不从 setting.json 读取 ——
DEFAULT_FLUSH_INTERVAL_SECONDS = 1.5
DEFAULT_FLUSH_MAX_WAIT_SECONDS = 8.0

# from_dict / to_dict 的字段白名单：只有这些进配置文件。
# ⚠️ 新增可配字段时同步改这里；调参字段永远不进这个元组。
_PERSIST_FIELDS = (
    "id",
    "label",
    "enabled",
    "default_workspace_uuid",
    "workspace_locked",
    "app_id",
    "secret",
    "msg_format",
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
class QQConfig:
    """单个 QQ 账号的配置 + 运行参数（§3.2 / §3.2.1）。

    上半部分是账号身份与开关（进配置文件）；下半部分是固定默认值的运行
    参数（不进配置文件，``from_dict`` 不读它们）。
    """

    # —— 账号身份与开关（进配置文件） ——
    id: str = ""
    label: str = ""
    enabled: bool = False
    default_workspace_uuid: str = ""
    workspace_locked: bool = False
    app_id: str = ""
    secret: str = ""
    msg_format: str = "markdown"

    # —— 运行参数（固定默认值，不进配置文件） ——
    flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS
    flush_max_wait_seconds: float = DEFAULT_FLUSH_MAX_WAIT_SECONDS
    max_inbound_bytes: int = DEFAULT_MAX_INBOUND_BYTES
    max_outbound_files: int = DEFAULT_MAX_OUTBOUND_FILES

    @classmethod
    def from_dict(cls, data: dict | None) -> "QQConfig":
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
                    f"[qq] 配置项 {name}={data[name]!r} 类型不合法，改用默认值"
                )
                continue
            kwargs[name] = coerced
        return cls(**kwargs)

    def to_dict(self) -> dict:
        """只序列化白名单字段（运行参数不落盘）。"""
        return {name: getattr(self, name) for name in _PERSIST_FIELDS}

    def is_ready(self) -> bool:
        """凭据是否齐全（不齐全时启动只记日志、不报错）。"""
        return bool(self.app_id and self.secret)


def _account_from_raw(item: dict) -> QQConfig:
    """原始账号字典 → QQConfig，并补齐缺失的 id。

    id 缺失（旧数据 / 手工编辑）时 QQ 用 ``app_id`` 兜底——它是机器人的
    自然标识；再不行才生成随机 id。
    """
    cfg = QQConfig.from_dict(item)
    if not cfg.id:
        cfg.id = cfg.app_id or secrets.token_hex(4)
    return cfg


def load_qq_accounts() -> list[QQConfig]:
    """读取账号列表（兼容旧扁平段：整段视为一个账号，id 取 app_id）。"""
    section = load_global_config().get(CONFIG_SECTION)
    if not isinstance(section, dict):
        return []
    accounts_raw = section.get("accounts")
    if isinstance(accounts_raw, list):
        return [_account_from_raw(a) for a in accounts_raw if isinstance(a, dict)]
    if section:
        # 旧扁平段：整段就是一个账号
        return [_account_from_raw(section)]
    return []


def save_qq_accounts(accounts: list[QQConfig]) -> bool:
    """把账号列表写回全局配置（保留其它段不动；只写白名单字段）。"""
    global_config = load_global_config()
    global_config[CONFIG_SECTION] = {"accounts": [a.to_dict() for a in accounts]}
    return save_global_config(global_config)


def load_qq_config() -> QQConfig:
    """兼容入口：优先取第一个 enabled 且凭据齐全的账号，其次第一个 enabled。

    多宿主落地后 ``start_qq_interface`` 遍历 :func:`load_qq_accounts`，
    此函数只剩兼容调用方与测试在用。
    """
    accounts = load_qq_accounts()
    for cfg in accounts:
        if cfg.enabled and cfg.is_ready():
            return cfg
    for cfg in accounts:
        if cfg.enabled:
            return cfg
    return QQConfig()


def save_qq_config(cfg: QQConfig) -> bool:
    """兼容入口：按 id 替换（无此 id 则追加）。"""
    accounts = load_qq_accounts()
    for i, a in enumerate(accounts):
        if a.id == cfg.id:
            accounts[i] = cfg
            break
    else:
        accounts.append(cfg)
    return save_qq_accounts(accounts)
