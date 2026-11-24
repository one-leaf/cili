"""青少年模式路由层测试：状态/策略/解锁端点与白名单校验。"""

import asyncio

import pytest

from core.teen_mode import TeenModeDenied


def _fake_request():
    """csrf_protect 只读 headers.get；client.host 供审计。"""
    from types import SimpleNamespace

    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                           headers={"origin": "http://localhost:8000"})


class TestTeenRoutes:
    """直接调用路由函数：任何 TypeError（签名不匹配）/ 异常都算失败。

    教训：unlock 路由曾传 ip= 而管理器无此参数，所有解锁请求 500，
    且管理器层单测完全测不到——路由层必须有冒烟。
    """

    def test_enable_status_unlock_policy_lock_disable_roundtrip(self, teen):

        from web.routes_teen import (
            TeenDisableRequest,
            TeenEnableRequest,
            TeenLockRequest,
            TeenPolicyRequest,
            TeenUnlockRequest,
            teen_disable,
            teen_enable,
            teen_lock,
            teen_status,
            teen_unlock,
            teen_update_policy,
        )

        req = _fake_request()

        # enable
        body = TeenEnableRequest(password="parent123", window_minutes=15)
        result = asyncio.run(teen_enable(req, body))
        assert result["success"] is True

        # status（公开，无 request 参数）
        status = asyncio.run(teen_status())
        assert status["enabled"] is True
        assert status["unlocked"] is False

        # unlock（正确密码 + ws 作用域）
        body = TeenUnlockRequest(password="parent123", scope="ws:abc")
        result = asyncio.run(teen_unlock(req, body))
        assert result["success"] is True
        assert result["scope"] == "ws:abc"

        # unlock（错误密码 → 401）
        import fastapi

        body = TeenUnlockRequest(password="badpass")
        try:
            asyncio.run(teen_unlock(req, body))
            raised = False
        except fastapi.HTTPException as e:
            raised = True
            assert e.status_code == 401
        assert raised

        # policy（已 unlock? ws 作用域不够改全局策略 → 403）
        body = TeenPolicyRequest(policy={"deny_hide": False})
        try:
            asyncio.run(teen_update_policy(req, body))
            policy_denied = False
        except TeenModeDenied:
            policy_denied = True
        assert policy_denied

        # 全局解锁后改策略
        asyncio.run(teen_unlock(req, TeenUnlockRequest(password="parent123")))
        body = TeenPolicyRequest(policy={"deny_hide": False}, limits={"daily_minutes": 30})
        result = asyncio.run(teen_update_policy(req, body))
        assert result["success"] is True
        assert teen.policy()["deny_hide"] is False
        assert teen.limits()["daily_minutes"] == 30

        # lock（立即回锁）
        result = asyncio.run(teen_lock(req, TeenLockRequest()))
        assert result["success"] is True
        assert teen.is_unlocked() is False

        # disable（未解锁 → 403；解锁+密码 → 成功）
        try:
            asyncio.run(teen_disable(req, TeenDisableRequest(password="parent123")))
            disable_denied = False
        except TeenModeDenied:
            disable_denied = True
        assert disable_denied

        asyncio.run(teen_unlock(req, TeenUnlockRequest(password="parent123")))
        result = asyncio.run(teen_disable(req, TeenDisableRequest(password="parent123")))
        assert result["success"] is True
        assert teen.is_enabled() is False

    def test_password_change_route(self, teen):

        from web.routes_teen import (
            TeenPasswordRequest,
            TeenUnlockRequest,
            teen_change_password,
            teen_enable,
            teen_unlock,
        )

        req = _fake_request()
        asyncio.run(teen_enable(req, _enable_body("parent123")))
        asyncio.run(teen_unlock(req, TeenUnlockRequest(password="parent123")))
        body = TeenPasswordRequest(old_password="parent123", new_password="newpass99")
        result = asyncio.run(teen_change_password(req, body))
        assert result["success"] is True
        assert teen.verify_password("newpass99") is True


def _enable_body(password):
    from web.routes_teen import TeenEnableRequest

    return TeenEnableRequest(password=password, window_minutes=15)


class TestAllowedWorkspacesValidation:
    """白名单嵌套校验：跨盘符不误伤（D: vs E: 的 commonpath 会抛 ValueError）。"""

    def _patch_index(self, monkeypatch, entries):
        import core.teen_mode as teen_mode_mod
        from web import routes_teen

        monkeypatch.setattr(teen_mode_mod, "load_workspaces_index", lambda: entries)
        monkeypatch.setattr(
            teen_mode_mod, "find_workspace_entry",
            lambda u: next((e for e in entries if e["uuid"] == u), None))
        return routes_teen

    def test_cross_drive_ok(self, monkeypatch):
        rt = self._patch_index(monkeypatch, [
            {"uuid": "wsd", "workspace_name": "d", "directory": "D:/a"},
            {"uuid": "wse", "workspace_name": "e", "directory": "E:/somewhere"},
        ])
        assert rt._validate_allowed_workspaces(["wsd", "wse"]) == ["wsd", "wse"]

    def test_same_drive_nesting_rejected(self, monkeypatch):
        import fastapi

        rt = self._patch_index(monkeypatch, [
            {"uuid": "parent", "workspace_name": "p", "directory": "E:/parent"},
            {"uuid": "child", "workspace_name": "c", "directory": "E:/parent/child"},
        ])
        with pytest.raises(fastapi.HTTPException) as ei:
            rt._validate_allowed_workspaces(["parent"])
        assert "嵌套" in ei.value.detail

    def test_system_and_missing_rejected(self, monkeypatch):
        import fastapi

        rt = self._patch_index(monkeypatch, [
            {"uuid": "wsx", "workspace_name": "x", "directory": "E:/x"},
        ])
        with pytest.raises(fastapi.HTTPException):
            rt._validate_allowed_workspaces(["system"])
        with pytest.raises(fastapi.HTTPException):
            rt._validate_allowed_workspaces(["no-such-uuid"])

    def test_policy_route_saves_cross_drive_whitelist(self, teen, monkeypatch):
        """端到端：跨盘符白名单经 PUT /api/teen/policy 正常落盘。"""

        from web.routes_teen import TeenPolicyRequest, teen_update_policy

        self._patch_index(monkeypatch, [
            {"uuid": "wsd", "workspace_name": "d", "directory": "D:/a"},
            {"uuid": "wse", "workspace_name": "e", "directory": "E:/somewhere"},
        ])
        teen.enable("parent123")
        teen.unlock("parent123")
        body = TeenPolicyRequest(policy={"allowed_workspaces": ["wsd", "wse"]})
        result = asyncio.run(teen_update_policy(_fake_request(), body))
        assert result["success"] is True
        assert teen.policy()["allowed_workspaces"] == ["wsd", "wse"]
