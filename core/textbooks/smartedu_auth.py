"""SmartEdu 登录凭据、匿名头与 MAC 签名（目录抓取共用）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import random
import secrets
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from core.config import PROJECT_ROOT
from core.time_utils import now_str

logger = logging.getLogger(__name__)

NONCE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
CDN_PLACEHOLDER = "cs_path:${ref-path}"


_ANON_ID = secrets.token_hex(16)


def anonymous_nd_auth() -> str:
    """匿名的 X-ND-AUTH：随机 id + 当前时间 nonce + 随机 mac（形如真实签名）。"""
    nonce = (f"{int(time.time() * 1000)}:"
             f"{''.join(secrets.choice(NONCE_ALPHABET) for _ in range(8))}")
    mac = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    return f'MAC id="{_ANON_ID}",nonce="{nonce}",mac="{mac}"'


BASE_HEADERS = {
    "Authorization": f"Bearer {_ANON_ID}",
    "Origin": "https://basic.smartedu.cn",
    "Referer": "https://basic.smartedu.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "X-ND-AUTH": anonymous_nd_auth(),
}


# ── 登录凭据与 MAC 签名 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SmartEduCredentials:
    """平台登录凭据；mac_key 为空时只能发占位头（私有资源会 400/401）。"""

    access_token: str = ""
    mac_key: str = ""
    diff: int = 0

    @property
    def complete(self) -> bool:
        return bool(self.access_token and self.mac_key)


AUTH_CACHE_PATH = PROJECT_ROOT / "data" / "cili" / "cache" / "smartedu_auth.json"
_auth_memory: SmartEduCredentials | None = None


def load_credentials() -> SmartEduCredentials:
    """读取浏览器登录后写入缓存目录的平台凭据（长期有效，不进 setting.json）。

    登录一次后凭据写入 ``data/cili/cache/smartedu_auth.json`` 并跨重启复用；
    仅当平台返回认证失效时才清除并重新走浏览器登录。
    """
    global _auth_memory
    if _auth_memory is not None:
        return _auth_memory
    try:
        data = json.loads(AUTH_CACHE_PATH.read_text(encoding="utf-8"))
        creds = SmartEduCredentials(
            access_token=str(data.get("access_token") or ""),
            mac_key=str(data.get("mac_key") or ""),
            diff=int(data.get("diff") or 0),
        )
    except (OSError, ValueError, TypeError):
        creds = SmartEduCredentials()
    _auth_memory = creds
    return creds


def save_credentials(creds: SmartEduCredentials) -> None:
    """保存浏览器登录得到的凭据（本地运行时缓存，权限尽量收紧）。"""
    global _auth_memory
    _auth_memory = creds
    try:
        AUTH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        AUTH_CACHE_PATH.write_text(json.dumps({
            "access_token": creds.access_token,
            "mac_key": creds.mac_key,
            "diff": creds.diff,
            "saved_at": now_str(),
        }, ensure_ascii=False), encoding="utf-8")
        try:
            AUTH_CACHE_PATH.chmod(0o600)
        except OSError:
            pass
    except OSError as e:
        logger.warning(f"[smartedu] 写登录凭据失败: {e}")


def clear_credentials() -> None:
    global _auth_memory
    _auth_memory = None
    try:
        AUTH_CACHE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def credentials() -> SmartEduCredentials:
    return load_credentials()


def is_auth_expired(status_code: int, error_code: str = "") -> bool:
    """判断平台响应是否表示登录凭据失效（401/403 或 400 InvalidArgument）。"""
    return status_code in (401, 403) or (
        status_code == 400 and str(error_code).strip() == "InvalidArgument"
    )


def generate_nonce(diff: int = 0) -> str:
    """按平台前端逻辑生成 nonce：毫秒时间戳(含时差) + 冒号 + 8 位随机字符。"""
    suffix = "".join(
        NONCE_ALPHABET[math.ceil(35 * random.random())] for _ in range(8)
    )
    return f"{int(time.time() * 1000) + int(diff)}:{suffix}"


def signature_string(url: str, method: str, nonce: str) -> str:
    """签名原文：nonce\\nMETHOD\\n解码后的 path(+query)\\nhostname\\n（末尾空行）。"""
    parts = urlsplit(url)
    relative = unquote(parts.path) + (f"?{parts.query}" if parts.query else "")
    return f"{nonce}\n{method.upper()}\n{relative}\n{parts.hostname or ''}\n"


def sign_mac(text: str, mac_key: str) -> str:
    digest = hmac.new(
        mac_key.encode("utf-8"), text.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def build_nd_auth(
    url: str,
    method: str = "GET",
    creds: SmartEduCredentials | None = None,
) -> str:
    creds = creds or credentials()
    if not creds.mac_key:   # 无凭据/凭据不完整 → 匿名占位（见 BASE_HEADERS 注释）
        return anonymous_nd_auth()
    nonce = generate_nonce(creds.diff)
    mac = sign_mac(signature_string(url, method, nonce), creds.mac_key)
    return f'MAC id="{creds.access_token}",nonce="{nonce}",mac="{mac}"'


def cdn_url(storage: str) -> str:
    """把平台存储地址换成 r1 私有 CDN；已是 https 的原样返回。"""
    if not storage:
        return ""
    if storage.startswith(CDN_PLACEHOLDER):
        return storage.replace(CDN_PLACEHOLDER, "https://r1-ndr-private.ykt.cbern.com.cn")
    return storage
