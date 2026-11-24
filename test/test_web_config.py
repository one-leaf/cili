"""web 配置域测试：API key 掩码、配置更新、restart 与建议路由。"""


import pytest

import core.session_control as session_control_mod
from core import restart as restart_mod


class TestMaskProviders:
    """_mask_api_key() providers 接入层 API key 掩码。"""

    def test_long_key_masked(self):
        from web.routes_config import _mask_api_key
        config = {"providers": {"bg": {"api_key": "sk-ant-1234567890abcdef", "models": {}}}}
        result = _mask_api_key(config)
        provider = result["providers"]["bg"]
        assert "api_key" not in provider
        assert provider["api_key_masked"] == "sk-a...cdef"

    def test_short_key_masked(self):
        from web.routes_config import _mask_api_key
        config = {"providers": {"bg": {"api_key": "shortkey"}}}
        result = _mask_api_key(config)
        assert result["providers"]["bg"]["api_key_masked"] == "***"

    def test_empty_key_no_masked_field(self):
        from web.routes_config import _mask_api_key
        config = {"providers": {"bg": {"api_key": ""}}}
        result = _mask_api_key(config)
        assert "api_key" not in result["providers"]["bg"]
        assert "api_key_masked" not in result["providers"]["bg"]

    def test_no_providers_field(self):
        from web.routes_config import _mask_api_key
        config = {"system": {"pip_mirror": "https://..."}}
        result = _mask_api_key(config)
        assert result["system"]["pip_mirror"] == "https://..."


class TestUpdateRoutes:
    """系统升级路由（routes_update）— 版本检查与升级包状态查询。"""

    def test_check_update(self, monkeypatch):
        """远端版本可用时返回比较结果，error 为空"""
        import asyncio

        from core import updater
        from web.routes_update import check_update

        monkeypatch.setattr(updater, "check_update", lambda: (True, "v20261017", "v20261018"))
        result = asyncio.run(check_update())
        assert result == {
            "has_update": True,
            "local_version": "v20261017",
            "remote_version": "v20261018",
            "error": None,
        }

    def test_check_update_network_error(self, monkeypatch):
        """远端版本不可用时 has_update 为 False 且给出错误说明"""
        import asyncio

        from core import updater
        from web.routes_update import check_update

        monkeypatch.setattr(updater, "check_update", lambda: (False, "v20261017", None))
        result = asyncio.run(check_update())
        assert result["has_update"] is False
        assert result["remote_version"] is None
        assert result["error"]

    def test_update_status_with_zip(self, tmp_path, monkeypatch):
        """脚本目录有升级包时状态返回包内版本与最近下载结果"""
        import asyncio
        import zipfile

        from core import updater
        from web import routes_update

        zip_path = tmp_path / updater.UPGRADE_ZIP_NAME
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("cili-main/web/static/version.json", '{"version": "v20261018"}')
        monkeypatch.setattr(updater, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(
            updater, "VERSION_FILE", tmp_path / "web" / "static" / "version.json")
        # 下载结果已下沉到 core.updater（自动/手动共用同一份）
        monkeypatch.setattr(
            updater, "_last_download_result", {"success": True, "message": "ok"})

        result = asyncio.run(routes_update.update_status())
        assert result["zip_present"] is True
        assert result["zip_version"] == "v20261018"
        assert result["downloading"] is False
        assert result["last_result"]["success"] is True
        assert result["local_version"] == "0.0.0"  # VERSION_FILE 不存在时按 0.0.0

    def test_download_rejects_when_busy(self, monkeypatch):
        """已有下载任务进行中时拒绝新的下载请求"""
        import asyncio

        from core import updater
        from web import routes_update

        monkeypatch.setattr(updater, "is_downloading", lambda: True)
        result = asyncio.run(routes_update.download_update())
        assert result["success"] is False
        assert "进行中" in result["error"]

    def test_download_start_guard_blocks_double_start(self, monkeypatch):
        """POST 到线程真正置位之间的窗口也视为「下载中」，连点不会起两个线程"""
        import asyncio

        from core import updater
        from web import routes_update

        monkeypatch.setattr(updater, "is_downloading", lambda: False)
        monkeypatch.setattr(routes_update, "_starting", True)

        result = asyncio.run(routes_update.download_update())
        assert result["success"] is False
        assert "进行中" in result["error"]

        # 状态接口同样把它算作下载中——前端轮询因此在窗口期不会提前停止
        status = asyncio.run(routes_update.update_status())
        assert status["downloading"] is True

    def test_update_status_reports_launcher(self, tmp_path, monkeypatch):
        """状态接口透出 launcher_managed（前端据此显示自动重启提示与重启按钮）"""
        import asyncio

        from core import updater
        from web import routes_update

        monkeypatch.setattr(updater, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(restart_mod, "is_launcher_managed", lambda: True)

        result = asyncio.run(routes_update.update_status())
        assert result["zip_present"] is False
        assert result["launcher_managed"] is True

    def test_restart_rejected_without_launcher(self, monkeypatch):
        """直接启动（python main.py）时手动重启不可用"""
        import asyncio

        from web import routes_update

        monkeypatch.setattr(restart_mod, "is_launcher_managed", lambda: False)
        result = asyncio.run(routes_update.restart_service())
        assert result["success"] is False
        assert "直接启动" in result["error"]

    def test_restart_rejected_without_zip(self, tmp_path, monkeypatch):
        """没有待应用的升级包时拒绝重启"""
        import asyncio

        from core import updater
        from web import routes_update

        monkeypatch.setattr(restart_mod, "is_launcher_managed", lambda: True)
        monkeypatch.setattr(updater, "PROJECT_ROOT", tmp_path)
        result = asyncio.run(routes_update.restart_service())
        assert result["success"] is False
        assert "升级包" in result["error"]

    def test_restart_rejected_when_busy(self, tmp_path, monkeypatch):
        """有会话运行中时拒绝重启并带 busy 标记"""
        import asyncio
        import zipfile

        from core import updater
        from web import routes_update

        zip_path = tmp_path / updater.UPGRADE_ZIP_NAME
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("cili-main/web/static/version.json", '{"version": "v20261018"}')
        monkeypatch.setattr(updater, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(restart_mod, "is_launcher_managed", lambda: True)
        monkeypatch.setattr(session_control_mod, "all_sessions_idle", lambda: False)

        result = asyncio.run(routes_update.restart_service())
        assert result["success"] is False
        assert result["busy"] is True

    def test_restart_success_requests_restart(self, tmp_path, monkeypatch):
        """升级包就绪且空闲时发起重启：调用 request_restart 并返回成功"""
        import asyncio
        import zipfile

        from core import updater
        from web import routes_update

        zip_path = tmp_path / updater.UPGRADE_ZIP_NAME
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("cili-main/web/static/version.json", '{"version": "v20261018"}')
        monkeypatch.setattr(updater, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(restart_mod, "is_launcher_managed", lambda: True)
        monkeypatch.setattr(session_control_mod, "all_sessions_idle", lambda: True)
        requested = []
        monkeypatch.setattr(
            restart_mod, "request_restart", lambda reason: requested.append(reason) or True)

        result = asyncio.run(routes_update.restart_service())
        assert result["success"] is True
        assert requested


class TestSuggestRoute:
    """suggest 端点：runner 经 _get_or_create_runner 获取、404/503、不 claim。"""

    def test_returns_generation_result(self, tmp_path, monkeypatch):
        import asyncio

        from web import routes_suggest

        (tmp_path / "sessions" / "sid").mkdir(parents=True)
        calls = {}

        async def fake_get_runner(workspace_uuid, session_id):
            calls["runner"] = (workspace_uuid, session_id)
            return "runner-stub"

        def fake_generate(runner, refresh, exclude, user_input=""):
            calls["generate"] = (runner, refresh, exclude)
            return {"suggestions": ["aa", "bb"], "source": "context", "route": "prefixed"}

        monkeypatch.setattr(routes_suggest, "_get_or_create_runner", fake_get_runner)
        monkeypatch.setattr(routes_suggest, "generate_suggestions", fake_generate)

        def _claim_forbidden(*args, **kwargs):
            raise AssertionError("suggest 不应 claim 运行权")

        monkeypatch.setattr("web.deps._claim_session_run", _claim_forbidden)

        body = routes_suggest.SuggestRequest(refresh=True, exclude=["旧建议"])
        result = asyncio.run(
            routes_suggest.suggest("ws-uuid", "sid", body, ws_dir=tmp_path)
        )

        assert result["route"] == "prefixed"
        assert calls["runner"] == ("ws-uuid", "sid")
        assert calls["generate"][1:] == (True, ["旧建议"])

    def test_missing_session_returns_404(self, tmp_path):
        import asyncio

        from fastapi import HTTPException

        from web import routes_suggest

        body = routes_suggest.SuggestRequest()
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(routes_suggest.suggest("ws-uuid", "nope", body, ws_dir=tmp_path))
        assert exc_info.value.status_code == 404

    def test_suggest_error_returns_503(self, tmp_path, monkeypatch):
        import asyncio

        from fastapi import HTTPException

        from core.suggest_service import SuggestError
        from web import routes_suggest

        (tmp_path / "sessions" / "sid").mkdir(parents=True)

        async def fake_get_runner(workspace_uuid, session_id):
            return "runner-stub"

        def fake_generate(runner, refresh, exclude, user_input=""):
            raise SuggestError("生成建议失败")

        monkeypatch.setattr(routes_suggest, "_get_or_create_runner", fake_get_runner)
        monkeypatch.setattr(routes_suggest, "generate_suggestions", fake_generate)

        body = routes_suggest.SuggestRequest()
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(routes_suggest.suggest("ws-uuid", "sid", body, ws_dir=tmp_path))
        assert exc_info.value.status_code == 503


# ─── 审批卡路由（decision / answers / 过期 / 终止） ──────────────────────
