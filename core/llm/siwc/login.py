"""SIWC 登录尝试：回环回调监听与手动粘贴。"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from core.llm.siwc import auth, const, manager, store
from core.llm.siwc.auth import AuthorizationRequest

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ATTEMPTS: dict[str, "LoginAttempt"] = {}
# 登录成功后的回调（provider_key, credential）：供接入端立即写回配置，
# 不依赖前端轮询（用户授权后直接关页面时仍能落盘）。
_ON_AUTHORIZED: Callable[[str, str], None] | None = None


def set_authorized_hook(callback: Callable[[str, str], None] | None) -> None:
    global _ON_AUTHORIZED
    _ON_AUTHORIZED = callback


@dataclass
class LoginAttempt:
    attempt_id: str
    provider_key: str
    credential: str
    auth_url: str
    redirect_uri: str
    request: AuthorizationRequest
    status: str = "pending"  # pending / processing / authorized / failed / expired / cancelled
    error: str = ""
    credential_result: str = ""
    email: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    server: Any = None


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "cili-siwc"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 —— 静默，避免回调 URL 进日志
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != const.CALLBACK_PATH:
            self._respond(404, "未找到")
            return
        host = self.headers.get("Host", "")
        if host != getattr(self.server, "expected_host", ""):
            self._respond(400, "Host 校验失败")
            return
        attempt_id = getattr(self.server, "attempt_id", "")
        result = _complete(attempt_id, f"http://{host}{self.path}", from_callback=True)
        if result.get("status") == "authorized":
            self._respond(200, "登录完成，请回到 cili。")
        elif result.get("status") == "already_done":
            self._respond(409, "该登录已完成")
        else:
            self._respond(400, "登录失败，请回到 cili 查看原因。")

    def _respond(self, code: int, text: str) -> None:
        body = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>cili</title></head><body style='font-family:sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            f"<p>{text}</p></body></html>"
        ).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


def start_login(provider_key: str, credential: str = "") -> LoginAttempt:
    with _LOCK:
        _cleanup_locked()
        for attempt in _ATTEMPTS.values():
            if attempt.provider_key == provider_key and attempt.status in ("pending", "processing"):
                return attempt

    creds = store.load(credential) if credential else None
    client_id = creds.client_id if creds else None
    id_token_hint = creds.id_token if creds and creds.id_token else None

    server = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CallbackHandler)
        server.daemon_threads = True
        port = int(server.server_address[1])
        redirect_uri = f"http://127.0.0.1:{port}{const.CALLBACK_PATH}"
    except OSError as e:
        logger.warning(f"[SIWC] 回环监听启动失败，改用固定回调地址: {e}")
        redirect_uri = f"http://127.0.0.1:1455{const.CALLBACK_PATH}"

    request = auth.create_authorization_request(
        redirect_uri=redirect_uri,
        ext_agent_host_id=store.host_id(),
        client_id=client_id,
        id_token_hint=id_token_hint,
    )
    attempt = LoginAttempt(
        attempt_id=secrets.token_hex(8),
        provider_key=provider_key,
        credential=credential or "",
        auth_url=request.url,
        redirect_uri=redirect_uri,
        request=request,
        expires_at=time.time() + const.LOGIN_TIMEOUT_SECONDS,
        server=server,
    )

    if server is not None:
        server.attempt_id = attempt.attempt_id
        server.expected_host = urlparse(redirect_uri).netloc
        threading.Thread(target=server.serve_forever, daemon=True, name="cili-siwc-callback").start()

    with _LOCK:
        _ATTEMPTS[attempt.attempt_id] = attempt
    return attempt


def poll(attempt_id: str) -> dict:
    with _LOCK:
        _cleanup_locked()
        attempt = _ATTEMPTS.get(attempt_id)
        if attempt is None:
            return {"status": "expired", "error": "登录尝试已过期", "credential": "", "email": ""}
        return {
            "status": attempt.status,
            "error": attempt.error,
            "credential": attempt.credential_result,
            "email": attempt.email,
            "provider": attempt.provider_key,
        }


def complete_manual(attempt_id: str, callback_url: str) -> dict:
    return _complete(attempt_id, callback_url.strip(), from_callback=False)


def cancel(attempt_id: str) -> None:
    with _LOCK:
        attempt = _ATTEMPTS.get(attempt_id)
        if attempt is None:
            return
        attempt.status = "cancelled"
    _close_server(attempt)


def shutdown() -> None:
    with _LOCK:
        attempts = list(_ATTEMPTS.values())
        _ATTEMPTS.clear()
    for attempt in attempts:
        _close_server(attempt)


def _complete(attempt_id: str, callback_url: str, *, from_callback: bool) -> dict:
    with _LOCK:
        attempt = _ATTEMPTS.get(attempt_id)
        if attempt is None:
            return {"status": "expired", "error": "登录尝试已过期"}
        if attempt.status in ("authorized", "cancelled", "expired"):
            return {"status": "already_done", "credential": attempt.credential_result}
        if attempt.status == "processing":
            return {"status": "processing"}
        if time.time() > attempt.expires_at:
            attempt.status = "expired"
            attempt.error = "登录超时"
            _close_server(attempt)
            return {"status": "expired", "error": attempt.error}
        attempt.status = "processing"

    try:
        parsed = auth.parse_callback_url(
            callback_url,
            expected_state=attempt.request.state,
            expected_redirect_uri=attempt.redirect_uri if not from_callback else None,
            require_client_id=attempt.request.registration,
            expected_client_id="" if attempt.request.registration else attempt.request.client_id,
        )
        client_id = parsed["client_id"] if attempt.request.registration else attempt.request.client_id
        creds = auth.exchange_code(
            code=parsed["code"],
            request=attempt.request,
            client_id=client_id,
            ext_agent_host_id=attempt.request.ext_agent_host_id,
        )
        credential = manager.save_login_result(creds)
    except Exception as e:  # noqa: BLE001 —— 登录失败统一记账，不抛出
        logger.warning(f"[SIWC] 登录换码失败: {type(e).__name__}: {e}")
        with _LOCK:
            attempt.status = "failed"
            attempt.error = str(e)[:200]
        _close_server(attempt)
        return {"status": "failed", "error": attempt.error}

    with _LOCK:
        attempt.status = "authorized"
        attempt.credential_result = credential
        attempt.email = creds.email
    _close_server(attempt)

    hook = _ON_AUTHORIZED
    if hook is not None:
        try:
            hook(attempt.provider_key, credential)
        except Exception as e:  # noqa: BLE001 —— 写回失败不应影响登录结果
            logger.warning(f"[SIWC] authorized 回调失败: {e}")

    return {"status": "authorized", "credential": credential, "email": creds.email}


def _close_server(attempt: LoginAttempt) -> None:
    server = attempt.server
    attempt.server = None
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass


def _cleanup_locked() -> None:
    """过期 attempt 标记并关闭监听；已结束的保留 5 分钟供前端轮询。"""
    now = time.time()
    stale: list[str] = []
    for aid, attempt in _ATTEMPTS.items():
        if attempt.status in ("pending", "processing") and now > attempt.expires_at:
            attempt.status = "expired"
            attempt.error = "登录超时"
            _close_server(attempt)
        if attempt.status not in ("pending", "processing") and now > attempt.expires_at + 300:
            stale.append(aid)
    for aid in stale:
        _ATTEMPTS.pop(aid, None)
