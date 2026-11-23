"""SIWC 授权、回调解析、换码与刷新。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx

from core.llm.siwc import const
from core.llm.siwc.credentials import Credentials, utcnow  # noqa: F401 —— 兼容旧导入

logger = logging.getLogger(__name__)


class RefreshFatal(Exception):
    """refresh 被永久拒绝，需要重新登录。"""


class RefreshTransient(Exception):
    """refresh 暂时失败（网络/5xx/429），凭证仍然有效。"""


class TokenEndpointError(Exception):
    """token 端点返回错误响应（只保留状态码与错误码，不含响应体）。"""

    def __init__(self, status: int, code: str = "", description: str = ""):
        super().__init__(f"HTTP {status} {code}".strip())
        self.status = status
        self.code = code
        self.description = description


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def decode_jwt_claims(token: str) -> dict:
    """读取 JWT payload（不验签；ID token 来自 TLS 的 token 端点，仅用于展示/绑定）。"""
    try:
        _, payload, _ = token.split(".")
        data = json.loads(_b64url_decode(payload))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _http_client(timeout: float = const.HTTP_TIMEOUT_SECONDS) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(
            connect=const.HTTP_CONNECT_TIMEOUT_SECONDS,
            read=timeout,
            write=timeout,
            pool=timeout,
        ),
        follow_redirects=False,
        headers={"User-Agent": "cili-agent"},
    )


def _read_json(resp: httpx.Response) -> dict:
    if len(resp.content) > const.MAX_RESPONSE_BYTES:
        raise RefreshTransient("认证端点响应过大")
    try:
        data = resp.json()
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def _error_code(data: dict) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        code = err.get("code") or err.get("type") or ""
    else:
        code = err or data.get("error_code") or ""
    return str(code or "")


def _error_description(data: dict) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("description") or "")[:200]
    value = data.get("error_description")
    return value[:200] if isinstance(value, str) else ""


def _post_token(body: dict[str, str], *, retry_connect: bool = False) -> dict:
    """POST token 端点；错误只保留状态码与错误码，不拼响应体。"""
    attempts = 2 if retry_connect else 1
    for attempt in range(attempts):
        try:
            with _http_client() as client:
                resp = client.post(const.TOKEN_URL, data=body)
        except httpx.ConnectError as e:
            if attempt + 1 < attempts:
                continue
            raise RefreshTransient("认证端点连接失败") from e
        except httpx.TimeoutException as e:
            # 一次性 code 可能已被消费：换码路径不允许在超时后重放。
            raise RefreshTransient("认证端点响应超时") from e
        except httpx.HTTPError as e:
            raise RefreshTransient(f"认证端点请求失败: {type(e).__name__}") from e

        data = _read_json(resp)
        if resp.status_code >= 400 or data.get("error"):
            raise TokenEndpointError(resp.status_code, _error_code(data), _error_description(data))
        return data
    raise RefreshTransient("认证端点连接失败")


@dataclass
class AuthorizationRequest:
    url: str
    state: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    client_id: str
    registration: bool
    ext_agent_host_id: str


def create_authorization_request(
    *,
    redirect_uri: str,
    ext_agent_host_id: str,
    client_id: str | None = None,
    id_token_hint: str | None = None,
    agent_name_hint: str | None = const.AGENT_NAME_HINT,
) -> AuthorizationRequest:
    registration = not client_id
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(32))
    nonce = _b64url(secrets.token_bytes(32))

    params = {
        "client_id": client_id or const.DYNAMIC_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "resource": const.RESOURCE,
        "scope": const.SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "ext_agent_host_id": ext_agent_host_id,
    }
    if registration and agent_name_hint:
        params["agent_name_hint"] = agent_name_hint
    if id_token_hint:
        params["id_token_hint"] = id_token_hint

    return AuthorizationRequest(
        url=f"{const.AUTHORIZE_URL}?{urlencode(params)}",
        state=state,
        nonce=nonce,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        client_id=client_id or const.DYNAMIC_CLIENT_ID,
        registration=registration,
        ext_agent_host_id=ext_agent_host_id,
    )


def parse_callback_url(
    url: str,
    *,
    expected_state: str,
    expected_redirect_uri: str | None = None,
    require_client_id: bool = False,
    expected_client_id: str = "",
) -> dict[str, str]:
    """解析回调 URL：校验 origin/path、state、重复参数与 client_id。"""
    parsed = urlparse(url)
    if expected_redirect_uri:
        expected = urlparse(expected_redirect_uri)
        if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path):
            raise ValueError("回调地址与本次登录不匹配")

    seen: dict[str, str] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in ("code", "state", "client_id"):
            if key in seen:
                raise ValueError("回调参数重复")
            seen[key] = value

    state = seen.get("state", "")
    if not expected_state or not hmac.compare_digest(state, expected_state):
        raise ValueError("state 校验失败")

    if seen.get("error"):
        detail = seen.get("error_description") or seen["error"]
        raise ValueError(f"授权被拒绝: {detail[:200]}")

    code = seen.get("code", "")
    if not code:
        raise ValueError("回调缺少 code")

    client_id = seen.get("client_id", "")
    if require_client_id:
        if not client_id or not re.fullmatch(const.CLIENT_ID_RE, client_id):
            raise ValueError("回调缺少有效的 client_id")
    elif client_id and expected_client_id and client_id != expected_client_id:
        raise ValueError("回调 client_id 与已保存的不一致")

    return {"code": code, "state": state, "client_id": client_id}


def validate_id_token_claims(id_token: str, *, client_id: str, nonce: str) -> dict:
    claims = decode_jwt_claims(id_token)
    if not claims:
        raise ValueError("ID token 无法解析")
    if claims.get("iss") != const.ISSUER:
        raise ValueError("ID token issuer 不匹配")
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud] if aud else []
    if client_id not in audiences:
        raise ValueError("ID token audience 不匹配")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or float(exp) <= time.time():
        raise ValueError("ID token 已过期")
    if nonce and claims.get("nonce") != nonce:
        raise ValueError("ID token nonce 不匹配")
    if not claims.get("sub"):
        raise ValueError("ID token 缺少 sub")
    return claims


def _claim_email(claims: dict) -> str:
    profile = claims.get("https://api.openai.com/profile")
    if isinstance(profile, dict) and profile.get("email"):
        return str(profile["email"])
    return str(claims.get("email") or "")


def _claim_plan(claims: dict) -> str:
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        plan = auth.get("chatgpt_plan_type") or auth.get("plan_type")
        return str(plan or "")
    return ""


def _parse_scope(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return value.split()
    return []


def exchange_code(
    *,
    code: str,
    request: AuthorizationRequest,
    client_id: str,
    ext_agent_host_id: str = "",
) -> Credentials:
    """用授权码换取凭证；任何失败都抛 ValueError/RefreshTransient。"""
    body = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": request.code_verifier,
        "redirect_uri": request.redirect_uri,
        "resource": const.RESOURCE,
    }
    try:
        data = _post_token(body, retry_connect=True)
    except TokenEndpointError as e:
        raise ValueError(f"换码失败: {e.code or f'HTTP {e.status}'}") from e

    access = str(data.get("access_token") or "")
    refresh_token = str(data.get("refresh_token") or "")
    id_token = str(data.get("id_token") or "")
    if not access or not refresh_token or not id_token:
        raise ValueError("授权响应缺少必要 token")

    scopes = _parse_scope(data.get("scope")) or const.SCOPE.split()
    if const.PLAN_SCOPE not in scopes:
        raise ValueError("该授权未包含套餐额度（chatgpt.tokens.use.direct），请重新登录并允许")

    claims = validate_id_token_claims(id_token, client_id=client_id, nonce=request.nonce)
    expires_in = int(data.get("expires_in") or 3600)
    return Credentials(
        client_id=client_id,
        sub=str(claims.get("sub") or ""),
        access_token=access,
        refresh_token=refresh_token,
        id_token=id_token,
        email=_claim_email(claims),
        plan_type=_claim_plan(claims),
        expires_at=utcnow() + timedelta(seconds=expires_in),
        scopes=scopes,
        ext_agent_host_id=ext_agent_host_id or request.ext_agent_host_id,
    )


def refresh(creds: Credentials) -> Credentials:
    """刷新 access token；失败分 fatal / transient。

    响应缺 refresh_token 或 id_token 时保留旧值；sub 前后不一致按 fatal 处理。
    """
    if not creds.client_id or not creds.refresh_token:
        raise RefreshFatal("invalid_refresh_token")

    body = {
        "grant_type": "refresh_token",
        "client_id": creds.client_id,
        "refresh_token": creds.refresh_token,
        "resource": const.RESOURCE,
    }
    try:
        data = _post_token(body, retry_connect=False)
    except TokenEndpointError as e:
        if e.code in const.FATAL_REFRESH_ERRORS or e.status in (400, 401, 403):
            raise RefreshFatal(e.code or f"HTTP {e.status}") from e
        raise RefreshTransient(e.code or f"HTTP {e.status}") from e

    access = str(data.get("access_token") or "")
    if not access:
        raise RefreshTransient("刷新响应缺少 access_token")

    new_refresh = str(data.get("refresh_token") or creds.refresh_token)
    new_id = str(data.get("id_token") or creds.id_token)
    new_sub = creds.sub
    if new_id and new_id != creds.id_token:
        claims = decode_jwt_claims(new_id)
        claimed_sub = str(claims.get("sub") or "")
        if claimed_sub and creds.sub and claimed_sub != creds.sub:
            raise RefreshFatal("账号在刷新期间发生变化")
        new_sub = claimed_sub or creds.sub

    expires_in = int(data.get("expires_in") or 3600)
    scopes = _parse_scope(data.get("scope")) or creds.scopes
    return Credentials(
        client_id=creds.client_id,
        sub=new_sub,
        access_token=access,
        refresh_token=new_refresh,
        id_token=new_id,
        email=creds.email,
        plan_type=creds.plan_type,
        expires_at=utcnow() + timedelta(seconds=expires_in),
        earliest_refresh_at=creds.earliest_refresh_at,
        scopes=scopes,
        ext_agent_host_id=creds.ext_agent_host_id,
        issuer=creds.issuer,
    )


def revoke(creds: Credentials) -> bool:
    """尽力撤销；失败返回 False（调用方提示远程撤销未确认）。"""
    token = creds.refresh_token or creds.access_token
    if not token:
        return False
    body = {
        "token": token,
        "token_type_hint": "refresh_token" if creds.refresh_token else "access_token",
    }
    if creds.refresh_token:
        body["client_id"] = creds.client_id
    try:
        with _http_client(timeout=10) as client:
            resp = client.post(const.REVOKE_URL, data=body)
        return resp.status_code < 400
    except Exception:
        return False
