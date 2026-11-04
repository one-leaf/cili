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

        def _uvicorn_run(*args, **kwargs):
            info_path = tmp_path / "data" / "cili" / "server.json"
            captured["server_info"] = json.loads(
                info_path.read_text(encoding="utf-8")
            )
            captured["uvicorn"] = kwargs

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
        monkeypatch.setattr(uvicorn, "run", _uvicorn_run)

        args = SimpleNamespace(host="127.0.0.1", port=8000)
        main_mod._start_web_interface(args)

        assert args.port == 8123
        assert captured["probe_token"] == "tok"
        assert captured["server_info"]["port"] == 8123
        assert captured["uvicorn"]["port"] == 8123
        assert not (tmp_path / "data" / "cili" / "server.json").exists()
