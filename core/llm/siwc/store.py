"""SIWC 凭证文件：host_id、账号文件、原子写与元数据枚举。

存储模式与 QQ/微信凭证一致：凭证与配置分离、每账号一文件、原子写。
不额外设置权限位（本地单用户场景）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from pathlib import Path

from core.config import DATA_DIR
from core.fs_utils import atomic_write_json, atomic_write_text, load_json_or_backup
from core.llm.siwc import const
from core.llm.siwc.credentials import Credentials

logger = logging.getLogger(__name__)

# 模块级根目录（测试可 monkeypatch 到 tmp）
SIWC_ROOT: Path | None = None

_ACCOUNT_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def root() -> Path:
    return SIWC_ROOT or (DATA_DIR / "tools" / "chatgpt")


def accounts_dir() -> Path:
    return root() / "accounts"


def host_id_path() -> Path:
    return root() / "host_id"


def host_id() -> str:
    """进程级稳定的 agent host id（urn:uuid:…），跨账号共享。"""
    path = host_id_path()
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value.startswith(const.HOST_ID_PREFIX) and len(value) > len(const.HOST_ID_PREFIX):
            return value
    except OSError:
        pass
    value = const.HOST_ID_PREFIX + str(uuid.uuid4())
    try:
        atomic_write_text(path, value)
    except OSError as e:
        logger.warning(f"[SIWC] 写入 host_id 失败: {e}")
    return value


def account_id(client_id: str, sub: str) -> str:
    raw = f"{const.ISSUER}\0{client_id}\0{sub}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _strip(credential: str) -> str:
    value = (credential or "").strip()
    if value.startswith(const.CREDENTIAL_PREFIX):
        value = value[len(const.CREDENTIAL_PREFIX):]
    return value


def account_path(credential: str) -> Path | None:
    aid = _strip(credential)
    if not _ACCOUNT_ID_RE.fullmatch(aid):
        return None
    return accounts_dir() / f"{aid}.json"


def load(credential: str) -> Credentials | None:
    path = account_path(credential)
    if path is None or not path.exists():
        return None
    data = load_json_or_backup(path, {})
    if not isinstance(data, dict) or not data:
        return None
    try:
        creds = Credentials.from_dict(data)
    except Exception as e:
        logger.warning(f"[SIWC] 凭证解析失败: {e}")
        return None
    if not creds.client_id or not creds.sub:
        return None
    return creds


def save(creds: Credentials) -> str:
    """原子写凭证；返回 credential 标识。"""
    aid = account_id(creds.client_id, creds.sub)
    path = accounts_dir() / f"{aid}.json"
    creds.saved_at = creds.saved_at or _now()
    atomic_write_json(path, creds.to_dict())
    return const.CREDENTIAL_PREFIX + aid


def mark_reauth(creds: Credentials) -> str:
    """fatal 刷新失败：清 token、保留身份信息、置 reauth_required。"""
    cleared = Credentials(
        client_id=creds.client_id,
        sub=creds.sub,
        access_token="",
        refresh_token="",
        id_token="",
        email=creds.email,
        plan_type=creds.plan_type,
        expires_at=creds.expires_at,
        earliest_refresh_at=creds.earliest_refresh_at,
        scopes=list(creds.scopes),
        reauth_required=True,
        ext_agent_host_id=creds.ext_agent_host_id,
        issuer=creds.issuer,
    )
    return save(cleared)


def delete(credential: str) -> bool:
    path = account_path(credential)
    if path is None:
        return False
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as e:
        logger.warning(f"[SIWC] 删除凭证失败: {e}")
        return False


def list_accounts() -> list[dict]:
    """只返回元数据，绝不返回 token。"""
    directory = accounts_dir()
    if not directory.exists():
        return []
    out: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        data = load_json_or_backup(path, {})
        if not isinstance(data, dict) or not data:
            continue
        sub = str(data.get("sub") or "")
        out.append({
            "credential": const.CREDENTIAL_PREFIX + path.stem,
            "email": str(data.get("email") or ""),
            "plan_type": str(data.get("plan_type") or ""),
            "sub": sub[:8],
            "expires_at": str(data.get("expires_at") or ""),
            "reauth_required": bool(data.get("reauth_required")),
        })
    return out


def _now():
    from core.llm.siwc.auth import utcnow
    return utcnow()


# ── provider 关联（登录成功后写回 setting.json）──────────────────────

def provider_entry(provider_key: str) -> dict:
    from core.config import load_global_config, migrate_legacy_config

    raw = migrate_legacy_config(load_global_config())
    entry = (raw.get("providers") or {}).get(provider_key)
    return entry if isinstance(entry, dict) else {}


def provider_credential(provider_key: str) -> str:
    return str(provider_entry(provider_key).get("credential") or "")


def upsert_oauth_provider(
    provider_key: str, credential: str, provider_config: dict | None = None
) -> str:
    """登录成功后直接创建/更新 provider（含 credential），返回写回状态。

    前端把当前卡片配置随登录请求带上；provider 尚未保存时也能一次落盘，
    避免“登录成功后卡片消失、还要再点一次保存”。
    """
    from core.config import load_global_config, migrate_legacy_config

    raw = migrate_legacy_config(load_global_config())
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        raw["providers"] = providers

    entry = providers.get(provider_key)
    if not isinstance(entry, dict):
        entry = {}
        providers[provider_key] = entry

    if isinstance(provider_config, dict):
        backend = provider_config.get("backend")
        if isinstance(backend, str) and backend:
            entry["backend"] = backend
        headers = provider_config.get("headers")
        if isinstance(headers, dict):
            entry["headers"] = {str(k): str(v) for k, v in headers.items()}
        models = provider_config.get("models")
        if isinstance(models, dict) and models:
            entry["models"] = models
    if not entry.get("models"):
        entry["models"] = {"default": {}}

    entry["auth_type"] = "oauth"
    entry["interface_type"] = "openai-responses"
    entry["base_url"] = "https://api.openai.com/v1"
    entry["api_path"] = ""
    entry["credential"] = credential
    entry.pop("api_key", None)

    from core.config import save_global_config

    return "written" if save_global_config(raw) else "failed"


def clear_provider_credential(provider_key: str) -> None:
    from core.config import load_global_config, migrate_legacy_config

    raw = migrate_legacy_config(load_global_config())
    entry = (raw.get("providers") or {}).get(provider_key)
    if not isinstance(entry, dict):
        return
    from core.config import save_global_config

    entry["credential"] = ""
    save_global_config(raw)
