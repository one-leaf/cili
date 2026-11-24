"""SIWC 授权、回调解析、换码与刷新测试（不联网）。"""

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from core.llm.chatgpt_oauth import auth, const


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _id_token(*, client_id: str, nonce: str, sub: str = "sub-1", exp_offset: int = 3600) -> str:
    payload = {
        "iss": const.ISSUER,
        "aud": client_id,
        "sub": sub,
        "nonce": nonce,
        "exp": int(time.time()) + exp_offset,
    }
    return f"{_b64({'alg': 'RS256'})}.{_b64(payload)}.sig"


def _request(**kwargs):
    return auth.create_authorization_request(
        redirect_uri="http://127.0.0.1:1234/auth/callback",
        ext_agent_host_id="urn:uuid:host",
        **kwargs,
    )


def test_authorization_request_params():
    req = _request()
    query = parse_qs(urlparse(req.url).query)
    assert query["client_id"] == [const.DYNAMIC_CLIENT_ID]
    assert query["agent_name_hint"] == ["Cili Agent"]
    assert query["ext_agent_host_id"] == ["urn:uuid:host"]
    assert query["resource"] == [const.RESOURCE]
    assert query["scope"] == [const.SCOPE]
    assert query["code_challenge_method"] == ["S256"]
    assert req.state and req.nonce and req.code_verifier
    assert req.registration is True


def test_reauthorization_omits_agent_name_hint():
    req = _request(client_id="oaiapp_abc", id_token_hint="idt")
    query = parse_qs(urlparse(req.url).query)
    assert "agent_name_hint" not in query
    assert query["client_id"] == ["oaiapp_abc"]
    assert query["id_token_hint"] == ["idt"]
    assert req.registration is False


def test_parse_callback_url():
    req = _request()
    good = f"http://127.0.0.1:1234/auth/callback?code=c&state={req.state}&client_id=oaiapp_abc"
    parsed = auth.parse_callback_url(
        good,
        expected_state=req.state,
        expected_redirect_uri=req.redirect_uri,
        require_client_id=True,
    )
    assert parsed["code"] == "c"
    assert parsed["client_id"] == "oaiapp_abc"

    with pytest.raises(ValueError):
        auth.parse_callback_url(good.replace(req.state, "bad"), expected_state=req.state)
    with pytest.raises(ValueError):
        auth.parse_callback_url(good + "&code=x", expected_state=req.state)
    with pytest.raises(ValueError):
        auth.parse_callback_url(
            f"http://127.0.0.1:1234/auth/callback?error=access_denied&state={req.state}",
            expected_state=req.state,
        )
    with pytest.raises(ValueError):
        auth.parse_callback_url(
            f"http://127.0.0.1:1234/auth/callback?code=c&state={req.state}&client_id=bad",
            expected_state=req.state,
            require_client_id=True,
        )
    with pytest.raises(ValueError):
        auth.parse_callback_url(
            good,
            expected_state=req.state,
            expected_redirect_uri="http://127.0.0.1:9999/auth/callback",
        )


def test_exchange_code_validates_scope_and_id_token(monkeypatch):
    req = _request()
    token = _id_token(client_id="oaiapp_abc", nonce=req.nonce)
    payload = {
        "access_token": "a",
        "refresh_token": "r",
        "id_token": token,
        "scope": const.SCOPE,
        "expires_in": 3600,
    }
    monkeypatch.setattr(auth, "_post_token", lambda body, **kw: dict(payload))

    creds = auth.exchange_code(code="c", request=req, client_id="oaiapp_abc", ext_agent_host_id="urn:uuid:host")
    assert creds.client_id == "oaiapp_abc"
    assert creds.sub == "sub-1"
    assert creds.refresh_token == "r"

    payload["scope"] = "openid profile"
    with pytest.raises(ValueError):
        auth.exchange_code(code="c", request=req, client_id="oaiapp_abc")

    payload["scope"] = const.SCOPE
    payload.pop("id_token")
    with pytest.raises(ValueError):
        auth.exchange_code(code="c", request=req, client_id="oaiapp_abc")


def test_refresh_keeps_old_tokens_when_missing(monkeypatch):
    old = auth.Credentials(
        client_id="oaiapp_abc",
        sub="sub-1",
        access_token="a0",
        refresh_token="r0",
        id_token=_id_token(client_id="oaiapp_abc", nonce="n"),
        expires_at=auth.utcnow(),
    )
    monkeypatch.setattr(auth, "_post_token", lambda body, **kw: {"access_token": "a1", "expires_in": 100})
    new = auth.refresh(old)
    assert new.access_token == "a1"
    assert new.refresh_token == "r0"
    assert new.id_token == old.id_token


def test_refresh_rejects_account_change(monkeypatch):
    old = auth.Credentials(
        client_id="oaiapp_abc",
        sub="sub-1",
        access_token="a0",
        refresh_token="r0",
        id_token=_id_token(client_id="oaiapp_abc", nonce="n", sub="sub-1"),
        expires_at=auth.utcnow(),
    )
    changed = _id_token(client_id="oaiapp_abc", nonce="n", sub="sub-2")
    monkeypatch.setattr(
        auth, "_post_token", lambda body, **kw: {"access_token": "a1", "id_token": changed}
    )
    with pytest.raises(auth.RefreshFatal):
        auth.refresh(old)


@pytest.mark.parametrize("status,code,expected", [
    (400, "invalid_grant", auth.RefreshFatal),
    (401, "", auth.RefreshFatal),
    (503, "", auth.RefreshTransient),
])
def test_refresh_error_classification(monkeypatch, status, code, expected):
    old = auth.Credentials(
        client_id="oaiapp_abc",
        sub="sub-1",
        access_token="a0",
        refresh_token="r0",
        expires_at=auth.utcnow(),
    )

    def boom(body, **kw):
        raise auth.TokenEndpointError(status, code)

    monkeypatch.setattr(auth, "_post_token", boom)
    with pytest.raises(expected):
        auth.refresh(old)
