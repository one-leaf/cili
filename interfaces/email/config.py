"""邮件接入端配置：``data/cili/setting.json`` 的 ``"email"`` 段。

**多账号模型**：``"email"`` 段是 ``{"accounts": [...]}``，每个元素一个账号。
账号 id = 邮箱地址（稳定、天然唯一、用户可读）。

**凭证存放**：``password``（授权码/应用专用密码）与 QQ 的 ``secret`` 同一模式——
存在 ``setting.json`` 里，``GET /api/config`` 返回掩码后的 ``password_masked``，
前端留空回传即保留原值。状态与游标**不在配置里**（见 ``state.py``），配置接口的
读-改-写不会碰轮询游标。

**固定默认值**：轮询间隔、体积上限、额度等调参字段保留在类里当默认值供调用点读，
``from_dict`` **不读**它们（与 QQ/微信的配置面收敛一致）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields

from core.channels import normalize_session_mode
from core.config import load_global_config, save_global_config
from interfaces.common.coerce import INVALID as _INVALID
from interfaces.common.coerce import coerce_value as _coerce

logger = logging.getLogger(__name__)

CONFIG_SECTION = "email"

# —— 以下均为固定默认值的运行参数（不从 setting.json 读取） ——

# 异步通道，1 分钟延迟无感；登录频率对各家邮箱都安全
DEFAULT_POLL_INTERVAL_SECONDS = 60.0
# 与 Web 拖放上传的上限一致
DEFAULT_MAX_INBOUND_BYTES = 100 * 1024 * 1024
# 主流邮箱服务商单封上限（约 25MB）留余量
DEFAULT_MAX_OUTBOUND_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_OUTBOUND_FILES = 5
# 超长正文（newsletter 类排版垃圾）截断
DEFAULT_MAX_BODY_CHARS = 50_000
# 积压时不一次性跑一堆回合
DEFAULT_MAX_MESSAGES_PER_POLL = 50
# 成本闸门（安全阀，不是计费系统，进程重启清零）
DEFAULT_TURNS_PER_SENDER_PER_HOUR = 20
DEFAULT_TURNS_PER_ACCOUNT_PER_DAY = 200

DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 465
DEFAULT_SMTP_SECURITY = "ssl"

# from_dict / to_dict 的字段白名单：只有这些进配置文件
_PERSIST_FIELDS = (
    "id",
    "label",
    "enabled",
    "email_address",
    "imap_host",
    "imap_port",
    "imap_ssl",
    "smtp_host",
    "smtp_port",
    "smtp_security",
    "username",
    "password",
    "allowed_senders",
    "default_workspace_uuid",
    "workspace_locked",
    "session_mode",
    "session_locked",
)


@dataclass
class EmailConfig:
    """单个邮箱账号的配置 + 运行参数。"""

    # —— 账号身份与开关（进配置文件） ——
    id: str = ""
    label: str = ""
    enabled: bool = False
    email_address: str = ""
    imap_host: str = ""
    imap_port: int = DEFAULT_IMAP_PORT
    imap_ssl: bool = True
    smtp_host: str = ""
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_security: str = DEFAULT_SMTP_SECURITY     # ssl | starttls
    username: str = ""
    password: str = ""
    # 默认拒绝：只有这些发件地址能触发回合（§三 3.3）
    allowed_senders: list[str] = field(default_factory=list)
    default_workspace_uuid: str = ""
    workspace_locked: bool = False
    session_mode: str = "dedicated"
    session_locked: bool = False

    # —— 运行参数（固定默认值，不进配置文件） ——
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    max_inbound_bytes: int = DEFAULT_MAX_INBOUND_BYTES
    max_outbound_bytes: int = DEFAULT_MAX_OUTBOUND_BYTES
    max_outbound_files: int = DEFAULT_MAX_OUTBOUND_FILES
    max_body_chars: int = DEFAULT_MAX_BODY_CHARS
    max_messages_per_poll: int = DEFAULT_MAX_MESSAGES_PER_POLL
    turns_per_sender_per_hour: int = DEFAULT_TURNS_PER_SENDER_PER_HOUR
    turns_per_account_per_day: int = DEFAULT_TURNS_PER_ACCOUNT_PER_DAY

    @classmethod
    def from_dict(cls, data: dict | None) -> "EmailConfig":
        """从账号字典构造；只读白名单字段，其余（含旧调参键）一律忽略。"""
        if not isinstance(data, dict):
            return cls()
        types = {f.name: f.type for f in fields(cls)}
        kwargs: dict = {}
        for name in _PERSIST_FIELDS:
            if name == "allowed_senders":
                continue
            if name not in data:
                continue
            coerced = _coerce(types.get(name, "str"), data[name])
            if coerced is _INVALID:
                logger.warning(
                    f"[email] 配置项 {name}={data[name]!r} 类型不合法，改用默认值"
                )
                continue
            kwargs[name] = coerced
        kwargs["allowed_senders"] = _coerce_senders(data.get("allowed_senders"))
        if "session_mode" in kwargs:
            kwargs["session_mode"] = normalize_session_mode(kwargs["session_mode"])
        if kwargs.get("smtp_security") not in ("ssl", "starttls"):
            kwargs["smtp_security"] = DEFAULT_SMTP_SECURITY
        return cls(**kwargs)

    def to_dict(self) -> dict:
        """只序列化白名单字段（运行参数不落盘）。"""
        return {name: getattr(self, name) for name in _PERSIST_FIELDS}

    def is_ready(self) -> bool:
        """配置是否足以启动轮询（白名单为空也能启动——拦截统计可见）。"""
        return bool(
            self.email_address and self.imap_host and self.smtp_host
            and self.username and self.password
        )


def _coerce_senders(value) -> list[str]:
    """白名单宽松解析：接受列表或逗号/换行分隔的字符串，去空去重（保持顺序）。"""
    if isinstance(value, str):
        raw = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw = [str(v) for v in value]
    else:
        raw = []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        addr = str(item or "").strip().lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        out.append(addr)
    return out


def load_email_accounts() -> list[EmailConfig]:
    """读取账号列表（空段 / 非法段返回空列表）。"""
    section = load_global_config().get(CONFIG_SECTION)
    if not isinstance(section, dict):
        return []
    accounts_raw = section.get("accounts")
    if not isinstance(accounts_raw, list):
        return []
    out: list[EmailConfig] = []
    for item in accounts_raw:
        if not isinstance(item, dict):
            continue
        cfg = EmailConfig.from_dict(item)
        if not cfg.id:
            cfg.id = cfg.email_address
        out.append(cfg)
    return out


def save_email_accounts(accounts: list[EmailConfig]) -> bool:
    """把账号列表写回全局配置（保留其它段不动；只写白名单字段）。"""
    global_config = load_global_config()
    global_config[CONFIG_SECTION] = {"accounts": [a.to_dict() for a in accounts]}
    return save_global_config(global_config)


def email_account_by_id(account_id: str) -> EmailConfig | None:
    """按 id 取账号；找不到返回 None。"""
    for cfg in load_email_accounts():
        if cfg.id == account_id:
            return cfg
    return None
