"""SIWC token 管理器：读取、刷新、状态、登出。"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from core.llm.siwc import auth, const, store
from core.llm.siwc.credentials import Credentials

logger = logging.getLogger(__name__)


class SiwcAuthRequired(RuntimeError):
    """需要用户重新登录（未登录 / 凭证失效）。"""

    siwc_kind = "auth"


class SiwcTransientError(RuntimeError):
    """瞬时失败（网络/5xx），凭证仍然有效。"""

    siwc_kind = "transient"


_LOCK = threading.Lock()
_CACHE: dict[str, Credentials] = {}


def _cache_key(credential: str) -> str:
    return credential.strip()


def _remember(credential: str, creds: Credentials) -> None:
    _CACHE[_cache_key(credential)] = creds


def _lookup(credential: str) -> Credentials | None:
    key = _cache_key(credential)
    creds = _CACHE.get(key)
    if creds is None:
        creds = store.load(credential)
        if creds is not None:
            _CACHE[key] = creds
    return creds


def _forget(credential: str) -> None:
    _CACHE.pop(_cache_key(credential), None)


def get_access_token(credential: str, *, force_refresh: bool = False) -> str:
    """取可用 access token；必要时刷新。

    刷新在进程内锁内串行化。锁等待超时抛瞬态错误，不重放已消费的 refresh token。
    """
    if not credential:
        raise SiwcAuthRequired("请先在设置页登录 ChatGPT")

    if not _LOCK.acquire(timeout=const.REFRESH_TIMEOUT_SECONDS + 5):
        raise SiwcTransientError("ChatGPT 凭证刷新等待超时，请稍后重试")

    try:
        creds = _lookup(credential)
        if creds is None:
            raise SiwcAuthRequired("请先在设置页登录 ChatGPT")
        if creds.reauth_required:
            raise SiwcAuthRequired("ChatGPT 登录已失效，请在设置页重新登录")

        now = auth.utcnow()
        margin = timedelta(seconds=const.TOKEN_EXPIRY_MARGIN_SECONDS)
        if not force_refresh and creds.expires_at - margin > now:
            return creds.access_token

        if (
            not force_refresh
            and creds.earliest_refresh_at is not None
            and now < creds.earliest_refresh_at
        ):
            if creds.expires_at > now:
                return creds.access_token
            raise SiwcTransientError("ChatGPT 凭证刷新受限，请稍后重试")

        try:
            new_creds = auth.refresh(creds)
        except auth.RefreshFatal as e:
            store.mark_reauth(creds)
            _forget(credential)
            logger.warning(f"[SIWC] refresh 被永久拒绝 ({e})，需要重新登录")
            raise SiwcAuthRequired("ChatGPT 登录已失效，请在设置页重新登录") from e
        except auth.RefreshTransient as e:
            raise SiwcTransientError(f"ChatGPT 凭证刷新失败：{e}") from e
        except Exception as e:  # noqa: BLE001 —— 未预期异常按瞬态处理，保留凭证
            logger.warning(f"[SIWC] refresh 异常: {type(e).__name__}")
            raise SiwcTransientError("ChatGPT 凭证刷新失败，请稍后重试") from e

        credential_id = store.save(new_creds)
        _remember(credential_id, new_creds)
        _remember(credential, new_creds)
        return new_creds.access_token
    finally:
        _LOCK.release()


def save_login_result(creds: Credentials) -> str:
    """登录换码成功后落盘（先写文件再返回 credential）。"""
    credential = store.save(creds)
    with _LOCK:
        _remember(credential, creds)
    return credential


def status(credential: str) -> dict:
    """只读状态，不触发刷新。"""
    creds = _lookup(credential) if credential else None
    if creds is None:
        return {"connected": False, "email": "", "expires_at": "", "scopes": [], "reauth_required": False}
    return {
        "connected": bool(creds.access_token) and not creds.reauth_required,
        "email": creds.email,
        "plan_type": creds.plan_type,
        "expires_at": auth._iso(creds.expires_at),
        "scopes": list(creds.scopes),
        "reauth_required": creds.reauth_required,
    }


def logout(credential: str, *, revoke: bool = True) -> dict:
    """撤销（尽力）并删除本地凭证。"""
    creds = _lookup(credential) if credential else None
    revoked = False
    if creds is not None and revoke:
        revoked = auth.revoke(creds)
    deleted = store.delete(credential) if credential else False
    _forget(credential)
    return {"deleted": deleted, "revoked": revoked}


def shutdown() -> None:
    with _LOCK:
        _CACHE.clear()
