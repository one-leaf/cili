"""微信接入端：账号持久化、context_token 台账、扫码登录状态机、路由端点。

沿用 ``test_qq_settings_api.py`` 的做法：**直接调端点函数**，不用 TestClient——
它和 ``asyncio.to_thread`` 组合会死锁，且 client IP 不在 localhost 白名单会 403。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json

import httpx
import pytest

# ─── 隔离 ─────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    """把账号文件重定向到临时目录。

    ``account.py`` 是 ``from core.config import DATA_DIR``（模块级绑定），所以必须
    patch 它自己的模块属性——patch ``core.config.DATA_DIR`` 是没用的。
    """
    import interfaces.wechat.account as account_mod
    monkeypatch.setattr(account_mod, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def isolated_cfg():
    """（历史兼容占位）登录流程已改用固定默认配置，不再需要隔离全局配置。"""
    from interfaces.wechat.config import WechatConfig
    return WechatConfig()


@pytest.fixture(autouse=True)
def _isolated_data_dirs(tmp_path, monkeypatch):
    """把微信接入端的两个持久化位置都指到临时目录（模块级兜底）。

    ``isolated_data`` 只覆盖了显式声明它的用例；登录/登出链路里任何一处
    ``save_account``/``save_state`` 漏隔离都会写进真实的
    ``data/cili/tools/wechat/``。这里对整个模块统一封死。
    """
    import interfaces.wechat.account as account_mod
    import interfaces.wechat.state as state_mod
    monkeypatch.setattr(account_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(state_mod, "DATA_DIR", tmp_path)


def _make_store(handler):
    """造一个用 MockTransport 的 login store（不碰真实网络）。"""
    from interfaces.wechat.client import WechatClient
    from interfaces.wechat.config import WechatConfig
    from interfaces.wechat.login import WechatLoginStore

    def factory(cfg: WechatConfig) -> WechatClient:
        return WechatClient(
            cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )

    return WechatLoginStore(client_factory=factory)


def _qr_handler(statuses: list[dict], calls: list | None = None):
    """按顺序返回扫码状态；同时记下每次请求的 URL 与 body。"""
    queue = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append({
                "url": str(request.url),
                "body": json.loads(request.content.decode()) if request.content else {},
            })
        if "/get_bot_qrcode" in str(request.url):
            return httpx.Response(200, json={
                "ret": 0, "qrcode": "qr-1", "qrcode_img_content": "https://qr.example/1",
            })
        payload = queue.pop(0) if queue else {"status": "wait"}
        return httpx.Response(200, json=payload)

    return handler


# ─── 账号持久化 ───────────────────────────────────────────────────────


class TestAccount:
    def test_from_dict_tolerates_junk(self):
        from interfaces.wechat.account import WechatAccount
        assert WechatAccount.from_dict(None) == WechatAccount()
        assert WechatAccount.from_dict({"bot_token": 123}).bot_token == "123"

    def test_save_and_load_per_account(self, isolated_data):
        import interfaces.wechat.account as account_mod
        from interfaces.wechat.account import WechatAccount
        acc = WechatAccount(
            bot_token="tok", base_url="https://b", ilink_user_id="u1",
            ilink_bot_id="b1", get_updates_buf="cur",
        )
        assert account_mod.save_account("acc1", acc)
        assert account_mod.load_account("acc1") == acc
        assert account_mod.load_account("acc1").has_token() is True
        # 其它账号互不可见（设计 §5.1：每账号一个文件）
        assert account_mod.load_account("acc2").has_token() is False

    def test_load_missing_returns_empty(self, isolated_data):
        import interfaces.wechat.account as account_mod
        from interfaces.wechat.account import WechatAccount
        assert account_mod.load_account("acc1") == WechatAccount()
        assert account_mod.load_account("acc1").has_token() is False

    def test_clear_account(self, isolated_data):
        import interfaces.wechat.account as account_mod
        from interfaces.wechat.account import WechatAccount
        account_mod.save_account("acc1", WechatAccount(bot_token="tok"))
        assert account_mod.clear_account("acc1")
        assert account_mod.load_account("acc1").bot_token == ""
        assert account_mod.clear_account("acc1")          # 再删一次也不报错

    def test_list_account_ids(self, isolated_data):
        import interfaces.wechat.account as account_mod
        from interfaces.wechat.account import WechatAccount
        assert account_mod.list_account_ids() == []
        account_mod.save_account("b2", WechatAccount(bot_token="t"))
        account_mod.save_account("a1", WechatAccount(bot_token="t"))
        assert account_mod.list_account_ids() == ["a1", "b2"]   # 排序稳定

    def test_legacy_single_file_compat(self, isolated_data):
        """迁移前布局：账号目录不存在时回退旧单文件（设计 §5.1 兼容读）。"""
        import interfaces.wechat.account as account_mod
        from core.fs_utils import atomic_write_json
        atomic_write_json(
            account_mod.legacy_account_path(),
            {"bot_token": "legacy-token", "ilink_user_id": "u1"},
        )
        assert account_mod.load_account("acc1").bot_token == "legacy-token"

    def test_no_legacy_fallback_once_dir_exists(self, isolated_data):
        """账号目录已存在时不再回退旧文件——已登出的账号不能从旧文件复活。"""
        import interfaces.wechat.account as account_mod
        from core.fs_utils import atomic_write_json
        from interfaces.wechat.account import WechatAccount
        atomic_write_json(
            account_mod.legacy_account_path(),
            {"bot_token": "legacy-token"},
        )
        account_mod.save_account("other", WechatAccount(bot_token="t"))   # 建目录
        assert account_mod.load_account("acc1").bot_token == ""

# ─── context_token 台账 ───────────────────────────────────────────────


class TestContextTokenStore:
    def _store(self, **kw):
        from interfaces.wechat.account import ContextTokenStore
        return ContextTokenStore(
            max_age_seconds=kw.pop("max_age_seconds", 60.0),
            budget=kw.pop("budget", 8),
        )

    def test_new_token_resets_used_count(self):
        """配额是按 context_token 算的——token 换了就该重新计数。"""
        s = self._store()
        s.update("u1", "t1", now=100.0)
        s.record_send("u1")
        s.record_send("u1")
        assert s.budget_left("u1") == 6
        s.update("u1", "t2", now=200.0)
        assert s.budget_left("u1") == 8

    def test_same_token_keeps_used_count(self):
        s = self._store()
        s.update("u1", "t1", now=100.0)
        s.record_send("u1")
        s.update("u1", "t1", now=110.0)
        assert s.budget_left("u1") == 7

    def test_needs_refresh_by_age(self):
        s = self._store(max_age_seconds=60.0)
        assert s.needs_refresh("u1", now=100.0) is False     # 没有 token
        s.update("u1", "t1", now=100.0)
        assert s.needs_refresh("u1", now=150.0) is False
        assert s.needs_refresh("u1", now=160.0) is True

    def test_mark_refreshed_resets_age_not_usage(self):
        s = self._store(max_age_seconds=60.0)
        s.update("u1", "t1", now=100.0)
        s.record_send("u1")
        s.mark_refreshed("u1", "t2", now=200.0)
        assert s.needs_refresh("u1", now=210.0) is False
        assert s.token("u1") == "t2"
        assert s.budget_left("u1") == 7          # 已用计数不重置

    def test_reserve_reduces_budget(self):
        s = self._store(budget=3)
        s.update("u1", "t1", now=0.0)
        assert s.can_send("u1") is True
        assert s.budget_left("u1", reserve=1) == 2
        s.record_send("u1")
        s.record_send("u1")
        s.record_send("u1")
        assert s.can_send("u1") is False
        assert s.budget_left("u1") == 0

    def test_update_ignores_empty(self):
        s = self._store()
        s.update("u1", "", now=0.0)
        s.update("", "t1", now=0.0)
        assert s.token("u1") == ""


# ─── 扫码登录状态机 ───────────────────────────────────────────────────


class TestQrPng:
    def test_empty_content(self):
        from interfaces.wechat.login import qr_png_data_url
        assert qr_png_data_url("") == ""

    def test_png_data_url(self):
        from interfaces.wechat.login import qr_png_data_url
        out = qr_png_data_url("https://example.com/scan")
        if importlib.util.find_spec("qrcode") is None:
            assert out == ""        # 降级：调用方展示原始内容串
        else:
            assert out.startswith("data:image/png;base64,")


class TestLoginStart:
    def test_returns_pending_with_qr(self, isolated_data):
        store = _make_store(_qr_handler([]))
        result = asyncio.run(store.start())
        assert result["status"] == "pending"
        assert result["qrcode_id"] == "qr-1"
        assert result["qr_content"] == "https://qr.example/1"
        assert result["session_id"]
        assert result["interval_ms"] == 2000

    def test_already_logged_in_short_circuits(self, isolated_data):
        """目标账号已有凭证时不取二维码（设计 §5.2 的按账号短路）。"""
        import interfaces.wechat.account as account_mod
        from interfaces.wechat.account import WechatAccount
        account_mod.save_account("acc1", WechatAccount(bot_token="tok", ilink_user_id="u1"))

        def boom(request):
            raise AssertionError("已登录就不该再取二维码")

        result = asyncio.run(_make_store(boom).start("acc1"))
        assert result["status"] == "succeeded"
        assert result["account_id"] == "acc1"
        assert result["ilink_user_id"] == "u1"

    def test_force_ignores_existing_token(self, isolated_data):
        """force 时不能把旧 token 交给服务端——否则会走 binded_redirect 拿不到新凭证。"""
        import interfaces.wechat.account as account_mod
        from interfaces.wechat.account import WechatAccount
        account_mod.save_account("acc1", WechatAccount(bot_token="old-token"))

        calls: list = []
        result = asyncio.run(_make_store(_qr_handler([], calls)).start("acc1", force=True))
        assert result["status"] == "pending"
        assert calls[0]["body"]["local_token_list"] == []

class TestLoginPoll:
    def _started(self, statuses, isolated_data, isolated_cfg, calls=None, force=False,
                 target="acc1"):
        store = _make_store(_qr_handler(statuses, calls))
        start = asyncio.run(store.start(target, force=force))
        return store, start["session_id"]

    def test_wait_stays_pending(self, isolated_data, isolated_cfg):
        store, sid = self._started([{"status": "wait"}], isolated_data, isolated_cfg)
        assert asyncio.run(store.poll(sid))["status"] == "pending"

    def test_unknown_session_is_expired(self, isolated_data, isolated_cfg):
        store = _make_store(_qr_handler([]))
        assert asyncio.run(store.poll("nope"))["status"] == "expired"

    def test_confirmed_saves_account(self, isolated_data, isolated_cfg):
        import interfaces.wechat.account as account_mod
        store, sid = self._started([{
            "status": "confirmed",
            "bot_token": "new-token",
            "baseurl": "https://redirected.example",
            "ilink_user_id": "u9",
            "ilink_bot_id": "b9",
        }], isolated_data, isolated_cfg)

        result = asyncio.run(store.poll(sid))

        assert result["status"] == "succeeded"
        assert result["account_id"] == "acc1"
        assert result["ilink_user_id"] == "u9"
        acc = account_mod.load_account("acc1")
        assert acc.bot_token == "new-token"
        assert acc.base_url == "https://redirected.example"
        assert acc.ilink_bot_id == "b9"
        assert acc.get_updates_buf == ""        # 新登录 → 游标从头开始
        assert store.session_count() == 0        # 会话已收尾

    def test_confirmed_without_token_fails(self, isolated_data, isolated_cfg):
        store, sid = self._started([{"status": "confirmed"}], isolated_data, isolated_cfg)
        assert asyncio.run(store.poll(sid))["status"] == "failed"

    def test_scaned_but_redirect_switches_host(self, isolated_data, isolated_cfg):
        calls: list = []
        store, sid = self._started(
            [{"status": "scaned_but_redirect", "redirect_host": "other.example.com"}],
            isolated_data, isolated_cfg, calls,
        )
        assert asyncio.run(store.poll(sid))["status"] == "pending"
        asyncio.run(store.poll(sid))
        assert calls[-1]["url"].startswith("https://other.example.com/")

    def test_need_verifycode_asks_for_challenge(self, isolated_data, isolated_cfg):
        store, sid = self._started([{"status": "need_verifycode"}], isolated_data, isolated_cfg)
        result = asyncio.run(store.poll(sid))
        assert result["status"] == "pending"
        assert result["challenge"] == "verify_code"
        assert result["verification_failed"] is False

    def test_wrong_verifycode_marks_failure(self, isolated_data, isolated_cfg):
        store, sid = self._started([{"status": "need_verifycode"}], isolated_data, isolated_cfg)
        result = asyncio.run(store.poll(sid, verify_code="1234"))
        assert result["verification_failed"] is True

    def test_expired_refreshes_qr(self, isolated_data, isolated_cfg):
        store, sid = self._started([{"status": "expired"}], isolated_data, isolated_cfg)
        result = asyncio.run(store.poll(sid))
        assert result["status"] == "pending"
        assert result["qrcode_id"] == "qr-1"      # handler 每次都给同一个码
        assert "刷新" in result["message"]

    def test_too_many_refreshes_fails(self, isolated_data, isolated_cfg, monkeypatch):
        import interfaces.wechat.login as login_mod
        monkeypatch.setattr(login_mod, "MAX_QR_REFRESH_COUNT", 1)
        store, sid = self._started(
            [{"status": "expired"}, {"status": "expired"}], isolated_data, isolated_cfg,
        )
        assert asyncio.run(store.poll(sid))["status"] == "pending"   # 第 1 次刷新
        assert asyncio.run(store.poll(sid))["status"] == "failed"    # 第 2 次超限

    def test_binded_redirect_with_local_token_succeeds(self, isolated_data, isolated_cfg):
        """扫的是本实例已绑定的号 → 本地有凭证就算成功（不区分是否 force）。

        必须 force=True 才走得到轮询——force=False 时已有凭证会被 start 短路。
        """
        import interfaces.wechat.account as account_mod
        from interfaces.wechat.account import WechatAccount
        account_mod.save_account("acc1", WechatAccount(bot_token="tok", ilink_user_id="u1"))
        store, sid = self._started(
            [{"status": "binded_redirect"}], isolated_data, isolated_cfg, force=True,
        )
        result = asyncio.run(store.poll(sid))
        assert result["status"] == "succeeded"
        assert result["account_id"] == "acc1"
        assert result["ilink_user_id"] == "u1"

    def test_binded_redirect_without_local_token_fails(self, isolated_data, isolated_cfg):
        store = _make_store(_qr_handler([{"status": "binded_redirect"}]))
        sid = asyncio.run(store.start(force=True))["session_id"]
        assert asyncio.run(store.poll(sid))["status"] == "failed"

    def test_retryable_error_keeps_waiting(self, isolated_data, isolated_cfg):
        def handler(request):
            if "/get_bot_qrcode" in str(request.url):
                return httpx.Response(200, json={"ret": 0, "qrcode": "qr-1", "qrcode_img_content": "u"})
            return httpx.Response(503, text="unavailable")

        store = _make_store(handler)
        sid = asyncio.run(store.start())["session_id"]
        assert asyncio.run(store.poll(sid))["status"] == "pending"

    def test_expired_session_is_cleaned(self, isolated_data, isolated_cfg, monkeypatch):
        import interfaces.wechat.login as login_mod
        monkeypatch.setattr(login_mod, "SESSION_TTL_S", -1.0)   # 立即过期
        store = _make_store(_qr_handler([]))
        sid = asyncio.run(store.start())["session_id"]
        assert asyncio.run(store.poll(sid))["status"] == "expired"
        assert store.session_count() == 0

    def test_cancel(self, isolated_data, isolated_cfg):
        store, sid = self._started([], isolated_data, isolated_cfg)
        assert asyncio.run(store.cancel(sid))["status"] == "cancelled"
        assert store.session_count() == 0


# ─── 路由端点（直接调函数） ───────────────────────────────────────────


class TestWechatRoutes:
    def _patch_store(self, monkeypatch, handler):
        import web.routes_wechat as routes
        monkeypatch.setattr(routes, "_store", _make_store(handler))
        return routes

    @staticmethod
    def _one_account():
        from interfaces.wechat.config import WechatConfig
        return [WechatConfig(id="acc1", label="主号")]

    def test_status_reports_logged_out(self, isolated_data, monkeypatch):
        import web.routes_wechat as routes
        monkeypatch.setattr(routes, "load_wechat_accounts", self._one_account)

        data = asyncio.run(routes.wechat_status())

        assert data["accounts"] and data["accounts"][0]["logged_in"] is False
        assert data["accounts"][0]["running"] is False
        assert data["accounts"][0]["enabled"] is False
        assert data["accounts"][0]["id"] == "acc1"
        assert "token" not in json.dumps(data)     # 凭证绝不出网

    def test_status_reports_logged_in(self, isolated_data, monkeypatch):
        import interfaces.wechat.account as account_mod
        import web.routes_wechat as routes
        from interfaces.wechat.account import WechatAccount
        monkeypatch.setattr(routes, "load_wechat_accounts", self._one_account)
        account_mod.save_account("acc1", WechatAccount(bot_token="secret-token", ilink_user_id="u1"))

        data = asyncio.run(routes.wechat_status())

        assert data["accounts"][0]["logged_in"] is True
        assert data["accounts"][0]["ilink_user_id"] == "u1"
        assert "secret-token" not in json.dumps(data)

    def test_login_status_requires_session_id(self, isolated_data, monkeypatch):
        from fastapi import HTTPException
        routes = self._patch_store(monkeypatch, _qr_handler([]))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(routes.wechat_login_status("acc1", session_id=""))
        assert exc.value.status_code == 400

    def test_login_start_and_poll(self, isolated_data, isolated_cfg, monkeypatch):
        routes = self._patch_store(monkeypatch, _qr_handler([{"status": "wait"}]))
        started = asyncio.run(routes.wechat_login_start("acc1", force=False))
        assert started["status"] == "pending"
        polled = asyncio.run(routes.wechat_login_status("acc1", session_id=started["session_id"]))
        assert polled["status"] == "pending"

    def test_login_start_cancels(self, isolated_data, isolated_cfg, monkeypatch):
        routes = self._patch_store(monkeypatch, _qr_handler([]))
        started = asyncio.run(routes.wechat_login_start("acc1", force=True))
        cancelled = asyncio.run(
            routes.wechat_login_cancel("acc1", session_id=started["session_id"])
        )
        assert cancelled["status"] == "cancelled"

    def test_logout_clears_credentials(self, isolated_data, monkeypatch):
        import interfaces.wechat.account as account_mod
        import web.routes_wechat as routes
        from interfaces.wechat.account import WechatAccount
        account_mod.save_account("acc1", WechatAccount(bot_token="tok"))

        result = asyncio.run(routes.wechat_logout("acc1"))

        assert result["success"] is True
        assert account_mod.load_account("acc1").bot_token == ""
