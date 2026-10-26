# -*- coding: utf-8 -*-
"""权限 API 测试：顶层 permissions 配置 + 全局规则 CRUD。"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.config import PermissionConfig
from web import routes_permissions as rp


def _fake_global_config(tmp_path, monkeypatch):
    """把 setting.json 读写重定向到内存，规则文件落到 tmp。"""
    cfg = PermissionConfig.from_dict({
        "global_rules_path": str(tmp_path / "approval_rules.json"),
    })
    state = {"providers": {"keep": {"x": 1}}, "permissions": cfg.to_dict()}

    monkeypatch.setattr(rp, "load_global_config", lambda: dict(state))

    def _save(data):
        state.clear()
        state.update(data)
        return True

    monkeypatch.setattr(rp, "save_global_config", _save)
    monkeypatch.setattr(rp, "_DEFAULT_GLOBAL_RULES", tmp_path / "approval_rules.json")
    return state


def _request():
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))


class TestConfigApi:
    def test_get_permissions_defaults(self, tmp_path, monkeypatch):
        _fake_global_config(tmp_path, monkeypatch)
        data = asyncio.run(rp.get_permissions())
        assert data["permissions"]["outside"]["read"] == "allow"
        assert data["permissions"]["default_mode"] == "allow"
        assert any(r["builtin"] for r in data["builtin_rules"])

    def test_put_permissions_keeps_other_keys(self, tmp_path, monkeypatch):
        state = _fake_global_config(tmp_path, monkeypatch)
        body = rp.PermissionUpdateRequest(permissions={
            "default_mode": "ask",
            "outside": {"read": "ai", "write": "deny", "delete": "ask", "navigate": "allow"},
            "readonly_bypass": False,
        })
        result = asyncio.run(rp.update_permissions(_request(), body))
        assert result["success"] is True
        assert state["permissions"]["default_mode"] == "ask"
        assert state["permissions"]["outside"]["read"] == "ai"
        assert state["permissions"]["outside"]["write"] == "deny"
        assert state["providers"] == {"keep": {"x": 1}}  # 其余键保留


class TestRuleApi:
    def test_global_rule_crud(self, tmp_path, monkeypatch):
        _fake_global_config(tmp_path, monkeypatch)
        body = rp.RuleCreateRequest(
            decision="deny", tool="bash", kind="command",
            match={"type": "prefix", "value": "rm -rf /"}, note="测试黑名单",
        )
        created = asyncio.run(rp.create_permission_rule(_request(), "global", body))
        assert created["success"] is True
        rule_id = created["rule"]["id"]

        listed = asyncio.run(rp.list_permission_rules("global", "all"))
        assert any(r["id"] == rule_id for r in listed["rules"])
        assert (tmp_path / "approval_rules.json").exists()

        updated = asyncio.run(rp.update_permission_rule(
            rule_id, _request(), "global", rp.RuleUpdateRequest(enabled=False)))
        assert updated["rule"]["enabled"] is False

        deleted = asyncio.run(rp.delete_permission_rule(rule_id, _request(), "global"))
        assert deleted["success"] is True
        remaining = asyncio.run(rp.list_permission_rules("global", "all"))
        assert all(r["id"] != rule_id for r in remaining["rules"])

    def test_invalid_rule_rejected(self, tmp_path, monkeypatch):
        _fake_global_config(tmp_path, monkeypatch)
        body = rp.RuleCreateRequest(
            decision="maybe", tool="bash", kind="command",
            match={"type": "prefix", "value": "x"},
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(rp.create_permission_rule(_request(), "global", body))
        assert exc.value.status_code == 400

    def test_decision_filter(self, tmp_path, monkeypatch):
        _fake_global_config(tmp_path, monkeypatch)
        allow = rp.RuleCreateRequest(decision="allow", kind="command",
                                     match={"type": "prefix", "value": "git status"})
        deny = rp.RuleCreateRequest(decision="deny", kind="command",
                                    match={"type": "prefix", "value": "rm -rf /"})
        asyncio.run(rp.create_permission_rule(_request(), "global", allow))
        asyncio.run(rp.create_permission_rule(_request(), "global", deny))
        only_deny = asyncio.run(rp.list_permission_rules("global", "deny"))
        assert only_deny["rules"] and all(r["decision"] == "deny" for r in only_deny["rules"])
