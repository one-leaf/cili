"""main.py 启动端口解析的接线测试（不启动真实服务）。"""

import json
from types import SimpleNamespace

import main as main_mod


class TestDefaultPort:
    def test_default_is_1618(self, monkeypatch):
        monkeypatch.setattr(main_mod.sys, "argv", ["main.py"])
        assert main_mod.parse_args().port == 1618

    def test_cli_port_overrides_default(self, monkeypatch):
        monkeypatch.setattr(main_mod.sys, "argv", ["main.py", "--port", "9000"])
        assert main_mod.parse_args().port == 9000

    def test_no_browser_flag(self, monkeypatch):
        monkeypatch.setattr(main_mod.sys, "argv", ["main.py", "--no-browser"])
        assert main_mod.parse_args().no_browser is True


class TestAutoOpenBrowser:
    def test_no_browser_flag_suppresses(self):
        assert main_mod._should_open_browser(SimpleNamespace(no_browser=True)) is False

    def test_restart_env_suppresses(self, monkeypatch):
        monkeypatch.setenv("CILI_LAUNCH_RESTART", "1")
        assert main_mod._should_open_browser(SimpleNamespace(no_browser=False)) is False

    def test_opens_on_first_launch(self, monkeypatch):
        monkeypatch.delenv("CILI_LAUNCH_RESTART", raising=False)
        assert main_mod._should_open_browser(SimpleNamespace(no_browser=False)) is True


class TestStartWebInterfacePortResolution:
    def test_resolved_port_reaches_args_server_info_and_uvicorn(
        self, tmp_path, monkeypatch
    ):
        import uvicorn

        import core.port_guard
        import web.deps

        captured = {}

        def _resolve(host, port, probe_token=""):
            captured["probe_token"] = probe_token
            return 8123

        class _FakeServer:
            """替身 uvicorn.Server：捕获配置，不真正启动服务。"""

            def __init__(self, config):
                self.config = config
                self.should_exit = False
                captured["config"] = config

            def run(self):
                info_path = tmp_path / "data" / "cili" / "server.json"
                captured["server_info"] = json.loads(
                    info_path.read_text(encoding="utf-8")
                )

        monkeypatch.setattr(core.port_guard, "resolve_web_port", _resolve)
        monkeypatch.setattr(main_mod, "_check_web_auth", lambda host: None)
        monkeypatch.setattr(
            main_mod,
            "_load_settings_cached",
            lambda: {"system": {"access_token": "tok"}},
        )
        monkeypatch.setattr(main_mod, "_PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr(web.deps, "init_web_deps", lambda: None)
        monkeypatch.setattr(
            main_mod.threading,
            "Thread",
            lambda *args, **kwargs: SimpleNamespace(start=lambda: None),
        )
        monkeypatch.setattr(main_mod.webbrowser, "open", lambda url: None)
        monkeypatch.setattr(uvicorn, "Server", _FakeServer)
        # 端口解析发生在 _start_services 里（核心服务启动前），这里验证整条接线
        monkeypatch.setattr(main_mod, "_start_core_services", lambda: None)

        args = SimpleNamespace(host="127.0.0.1", port=8000, no_browser=True)
        main_mod._start_services(args)

        assert args.port == 8123
        assert captured["probe_token"] == "tok"
        assert captured["server_info"]["port"] == 8123
        assert captured["config"].port == 8123
        assert captured["config"].timeout_graceful_shutdown == 15
        assert not (tmp_path / "data" / "cili" / "server.json").exists()
