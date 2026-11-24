"""web 访问控制测试：localhost 判定与令牌 cookie 鉴权。"""

import asyncio
from types import SimpleNamespace


class TestLocalhostIPs:
    """Access control localhost IP set."""

    def test_contains_standard_ips(self):
        from web.access_control import LOCALHOST_IPS as _LOCALHOST_IPS
        assert "127.0.0.1" in _LOCALHOST_IPS
        assert "::1" in _LOCALHOST_IPS


class TestTokenCookieAuth:
    """P1-32：?token= 首次鉴权成功后下发 HttpOnly cookie；cookie 可单独通过鉴权。"""

    @staticmethod
    def _request(headers=None, query=""):
        from starlette.requests import Request as StarletteRequest
        raw = []
        for k, v in (headers or {}).items():
            raw.append((k.lower().encode("latin-1"), v.encode("latin-1")))
        scope = {
            "type": "http", "method": "GET", "path": "/",
            "headers": raw, "query_string": query.encode(),
            "client": ("127.0.0.1", 1), "scheme": "http",
            "server": ("127.0.0.1", 80),
        }
        return StarletteRequest(scope)

    def _run_middleware(self, monkeypatch, request):

        import web.web_api as wa

        cfg = SimpleNamespace(system=SimpleNamespace(access_token="secret", allowed_ips=[]))
        monkeypatch.setattr(wa, "load_config", lambda: cfg)

        async def call_next(req):
            from fastapi.responses import JSONResponse
            return JSONResponse({"ok": True})

        return asyncio.run(wa.check_access_control(request, call_next))

    def test_query_token_sets_httponly_cookie(self, monkeypatch):
        resp = self._run_middleware(monkeypatch, self._request(query="token=secret"))
        assert resp.status_code == 200
        set_cookie = resp.headers.get("set-cookie", "")
        assert "cili_access_token=secret" in set_cookie
        assert "httponly" in set_cookie.lower()

    def test_cookie_alone_authenticates(self, monkeypatch):
        resp = self._run_middleware(
            monkeypatch, self._request(headers={"Cookie": "cili_access_token=secret"}))
        assert resp.status_code == 200

    def test_invalid_token_rejected(self, monkeypatch):
        resp = self._run_middleware(monkeypatch, self._request(query="token=wrong"))
        assert resp.status_code == 403

    def test_header_token_does_not_set_cookie(self, monkeypatch):
        resp = self._run_middleware(
            monkeypatch, self._request(headers={"X-Access-Token": "secret"}))
        assert resp.status_code == 200
        assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}
