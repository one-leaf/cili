"""组织托管 Web API：状态、发现、登录与错误映射。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import web.routes_org as routes
from core.org.client import OrgNetworkError, OrgRateLimited


class FakeService:
    def __init__(self):
        self.status_data = {"joined": False}

    def status(self):
        return self.status_data

    def discover(self, server_url):
        return [{"id": "acme", "name": "Acme"}]

    def send_sms(self, server_url, org_id, phone):
        return {"cooldown_seconds": 45}

    def login(self, **kwargs):
        self.status_data = {"joined": True, "server_url": kwargs["server_url"]}
        return self.status_data

    def sync(self, **kwargs):
        return self.status_data

    def logout(self):
        self.status_data = {"joined": False}
        return self.status_data


@pytest.fixture
def fake(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(routes, "get_org_service", lambda: service)
    return service


def _run(coro):
    return asyncio.run(coro)


def test_status_and_discover(fake):
    assert _run(routes.org_status())["joined"] is False
    data = _run(
        routes.org_discover(routes.DiscoverRequest(server_url="https://org.example"))
    )
    assert data["orgs"][0]["id"] == "acme"


def test_sms_login_sync_logout(fake):
    sms = _run(
        routes.org_send_sms(
            routes.SmsSendRequest(
                server_url="https://org.example",
                org_id="acme",
                phone="13800000000",
            )
        )
    )
    assert sms == {"ok": True, "cooldown_seconds": 45}

    login = _run(
        routes.org_login(
            routes.OrgLoginRequest(
                server_url="https://org.example",
                org_id="acme",
                phone="13800000000",
                code="123456",
                bind_roles=["master"],
            )
        )
    )
    assert login["joined"] is True
    assert _run(routes.org_sync())["joined"] is True
    assert _run(routes.org_logout())["joined"] is False


def test_csrf_allows_same_lan_origin():
    from types import SimpleNamespace

    from web.deps import csrf_protect

    request = SimpleNamespace(
        headers={
            "origin": "http://192.168.1.5:8000",
            "host": "192.168.1.5:8000",
        }
    )
    csrf_protect(request)

    bad = SimpleNamespace(
        headers={
            "origin": "http://evil.example",
            "host": "192.168.1.5:8000",
        }
    )
    with pytest.raises(HTTPException) as exc:
        csrf_protect(bad)
    assert exc.value.status_code == 403


def test_error_mapping(monkeypatch):
    class BadService(FakeService):
        def discover(self, server_url):
            raise OrgNetworkError("offline")

    service = BadService()
    monkeypatch.setattr(routes, "get_org_service", lambda: service)
    with pytest.raises(HTTPException) as exc:
        _run(routes.org_discover(
            routes.DiscoverRequest(server_url="https://org.example")
        ))
    assert exc.value.status_code == 502

    class RateService(FakeService):
        def send_sms(self, server_url, org_id, phone):
            raise OrgRateLimited(9)

    monkeypatch.setattr(routes, "get_org_service", lambda: RateService())
    with pytest.raises(HTTPException) as exc:
        _run(routes.org_send_sms(
            routes.SmsSendRequest(
                server_url="https://org.example",
                org_id="acme",
                phone="13800000000",
            )
        ))
    assert exc.value.status_code == 429
    assert exc.value.detail["retry_after"] == 9
