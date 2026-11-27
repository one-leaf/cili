"""组织服务端 HTTP 客户端（按设计文档的 /api/v1 契约）。"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OrgError(Exception):
    """组织相关错误基类。"""


class OrgNetworkError(OrgError):
    pass


class OrgAuthError(OrgError):
    def __init__(self, status: int, code: str = "", message: str = ""):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message or code or f"认证失败（HTTP {status}）")


class OrgRateLimited(OrgError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"操作过于频繁，请 {self.retry_after} 秒后再试")


class OrgProtocolError(OrgError):
    def __init__(self, status: int, code: str = "", message: str = ""):
        self.status = status
        self.code = code
        super().__init__(message or code or f"组织服务端返回 HTTP {status}")


class OrgClient:
    def __init__(
        self,
        server_url: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 15.0,
    ):
        self.server_url = str(server_url or "").rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
            self._owns_client = True
        return self._client

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str = "",
        etag: str = "",
        **kwargs,
    ) -> tuple[int, dict[str, str], Any]:
        url = self.server_url + path
        headers = dict(kwargs.pop("headers", {}) or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if etag:
            headers["If-None-Match"] = etag
        try:
            response = self._http().request(method, url, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise OrgNetworkError(f"无法连接组织服务端: {exc}") from exc

        if response.status_code == 304:
            return 304, dict(response.headers), None
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400:
            code = ""
            message = ""
            if isinstance(data, dict):
                raw_error = data.get("error")
                detail = data.get("detail")
                if isinstance(raw_error, dict):
                    code = str(raw_error.get("error") or raw_error.get("code") or "")
                    message = str(raw_error.get("message") or "")
                elif isinstance(raw_error, str) and raw_error:
                    code = raw_error
                    message = detail if isinstance(detail, str) and detail else raw_error
                elif isinstance(detail, dict):
                    code = str(detail.get("error") or detail.get("code") or "")
                    message = str(detail.get("message") or "")
                elif isinstance(detail, str) and detail:
                    code = detail
                    message = detail
            if response.status_code in (401, 403):
                raise OrgAuthError(response.status_code, code, message)
            if response.status_code == 429:
                retry_after = 60
                if isinstance(data, dict):
                    try:
                        retry_after = int(data.get("retry_after") or 60)
                    except (TypeError, ValueError):
                        retry_after = 60
                raise OrgRateLimited(retry_after)
            raise OrgProtocolError(response.status_code, code, message)
        return response.status_code, dict(response.headers), data

    # ── 端点 ──────────────────────────────────────────────

    def discover(self) -> list[dict]:
        _status, _headers, data = self._request("GET", "/api/v1/public/orgs")
        orgs = data.get("orgs") if isinstance(data, dict) else None
        return orgs if isinstance(orgs, list) else []

    def send_sms(self, org_id: str, phone: str) -> dict:
        _status, _headers, data = self._request(
            "POST",
            "/api/v1/auth/sms/send",
            json={"org_id": org_id, "phone": phone},
        )
        return data if isinstance(data, dict) else {}

    def verify(
        self,
        *,
        org_id: str,
        phone: str,
        code: str,
        device_name: str = "cili",
    ) -> dict:
        _status, _headers, data = self._request(
            "POST",
            "/api/v1/auth/sms/verify",
            json={
                "org_id": org_id,
                "phone": phone,
                "code": code,
                "device_name": device_name,
            },
        )
        if not isinstance(data, dict) or not data.get("token"):
            raise OrgProtocolError(200, "invalid_response", "组织服务端未返回设备令牌")
        return data

    def me(self, token: str) -> dict:
        _status, _headers, data = self._request(
            "GET", "/api/v1/auth/me", token=token
        )
        return data if isinstance(data, dict) else {}

    def fetch_config(self, token: str, etag: str = "") -> tuple[bool, dict]:
        """返回 ``(not_modified, payload)``。"""
        status, _headers, data = self._request(
            "GET", "/api/v1/config", token=token, etag=etag
        )
        if status == 304:
            return True, {}
        return False, data if isinstance(data, dict) else {}

    def logout(self, token: str) -> None:
        try:
            self._request("POST", "/api/v1/auth/logout", token=token)
        except OrgError:
            # 退出是本地行为，服务端吊销失败不阻断清理。
            logger.warning("[org] 服务端吊销设备失败", exc_info=True)
