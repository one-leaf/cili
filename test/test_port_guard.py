"""core/port_guard.py 端口探测与冲突处理的单元测试（不启动真实服务）。"""

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import core.port_guard as pg


class _OpenAPIHandler(BaseHTTPRequestHandler):
    """返回带指定 title 的 OpenAPI JSON，用于 HTTP 指纹测试。"""

    title = "Cili Web API"

    def do_GET(self):
        body = (
            '{"openapi":"3.1.0","info":{"title":"' + self.title + '"}}'
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _ForeignOpenAPIHandler(_OpenAPIHandler):
    title = "Other App"


def _start_server(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _wait_free(host, port, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pg.is_port_free(host, port):
            return True
        time.sleep(0.05)
    return False


class TestPortFree:
    def test_occupied_then_free_after_close(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            assert pg.is_port_free("127.0.0.1", port) is False
        finally:
            listener.close()
        assert _wait_free("127.0.0.1", port) is True


class TestFindListenerPids:
    def test_cim_single_listener_row(self, monkeypatch):
        monkeypatch.setattr(pg.sys, "platform", "win32")
        payload = json.dumps({
            "LocalAddress": "127.0.0.1",
            "LocalPort": 18787,
            "RemoteAddress": "0.0.0.0",
            "RemotePort": 0,
            "State": None,
            "OwningProcess": 544,
        })
        monkeypatch.setattr(
            pg.subprocess, "run",
            lambda *args, **kwargs: SimpleNamespace(stdout=payload, returncode=0),
        )
        assert pg._cim_listener_pids(18787) == [544]

    def test_cim_filters_established_rows(self, monkeypatch):
        monkeypatch.setattr(pg.sys, "platform", "win32")
        payload = json.dumps([
            {
                "LocalPort": 18788, "RemoteAddress": "127.0.0.1",
                "RemotePort": 63271, "State": 5, "OwningProcess": 15924,
            },
            {
                "LocalPort": 18788, "RemoteAddress": "0.0.0.0",
                "RemotePort": 0, "State": None, "OwningProcess": 15924,
            },
            {
                "LocalPort": 18788, "RemoteAddress": "127.0.0.1",
                "RemotePort": 63272, "State": 5, "OwningProcess": 7777,
            },
        ])
        monkeypatch.setattr(
            pg.subprocess, "run",
            lambda *args, **kwargs: SimpleNamespace(stdout=payload, returncode=0),
        )
        assert pg._cim_listener_pids(18788) == [15924]

    def test_cim_empty_output(self, monkeypatch):
        monkeypatch.setattr(pg.sys, "platform", "win32")
        monkeypatch.setattr(
            pg.subprocess, "run",
            lambda *args, **kwargs: SimpleNamespace(stdout="", returncode=1),
        )
        assert pg._cim_listener_pids(8000) == []

    def test_cim_invalid_json(self, monkeypatch):
        monkeypatch.setattr(pg.sys, "platform", "win32")
        monkeypatch.setattr(
            pg.subprocess, "run",
            lambda *args, **kwargs: SimpleNamespace(stdout="{not json", returncode=0),
        )
        assert pg._cim_listener_pids(8000) == []

    def test_cim_query_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(pg.sys, "platform", "win32")

        def _boom(*args, **kwargs):
            raise OSError("powershell missing")

        monkeypatch.setattr(pg.subprocess, "run", _boom)
        assert pg._cim_listener_pids(8000) is None

    def test_netstat_fallback_parses_exact_port(self, monkeypatch):
        fake = (
            "  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       4321\n"
            "  TCP    127.0.0.1:18000        0.0.0.0:0              LISTENING       9999\n"
            "  TCP    [::1]:8000             [::]:0                 LISTENING       4321\n"
            "  TCP    127.0.0.1:8001         127.0.0.1:0            ESTABLISHED     7777\n"
            "  UDP    0.0.0.0:8000           *:*                                    1234\n"
        )
        monkeypatch.setattr(pg.sys, "platform", "win32")
        monkeypatch.setattr(
            pg.subprocess, "run",
            lambda *args, **kwargs: SimpleNamespace(stdout=fake),
        )
        assert pg._netstat_listener_pids(8000) == [4321]
        assert pg._netstat_listener_pids(18000) == [9999]
        assert pg._netstat_listener_pids(8001) == []
        assert pg._netstat_listener_pids(800) == []

    def test_cim_preferred_over_netstat(self, monkeypatch):
        monkeypatch.setattr(pg, "_cim_listener_pids", lambda port: [42])
        monkeypatch.setattr(pg, "_netstat_listener_pids", lambda port: [99])
        assert pg.find_listener_pids(8000) == [42]

    def test_cim_empty_result_does_not_use_netstat(self, monkeypatch):
        def _netstat(port):
            raise AssertionError("netstat fallback should not run")

        monkeypatch.setattr(pg, "_cim_listener_pids", lambda port: [])
        monkeypatch.setattr(pg, "_netstat_listener_pids", _netstat)
        assert pg.find_listener_pids(8000) == []

    def test_cim_query_failure_falls_back_to_netstat(self, monkeypatch):
        monkeypatch.setattr(pg, "_cim_listener_pids", lambda port: None)
        monkeypatch.setattr(pg, "_netstat_listener_pids", lambda port: [77])
        assert pg.find_listener_pids(8000) == [77]


class TestCiliCommandLine:
    def test_project_main_py(self):
        cmdline = f'"python.exe" {pg.PROJECT_ROOT / "main.py"}'
        assert pg._is_cili_command_line(cmdline) is True

    def test_other_cili_install(self):
        assert pg._is_cili_command_line(
            r'"C:\Tools\cili\python.exe" C:\Tools\cili\main.py'
        ) is True

    def test_foreign_python_script(self):
        assert pg._is_cili_command_line(
            r'"python.exe" C:\other\server.py'
        ) is False

    def test_non_python_main(self):
        assert pg._is_cili_command_line(
            r'"node.exe" C:\cili\main.py'
        ) is False


class TestIsCiliListener:
    def test_server_info_pid_port_match(self, tmp_path, monkeypatch):
        info = tmp_path / "server.json"
        info.write_text(
            json.dumps({"host": "127.0.0.1", "port": 8000, "pid": 4242}),
            encoding="utf-8",
        )
        monkeypatch.setattr(pg, "SERVER_INFO_PATH", info)
        monkeypatch.setattr(pg, "get_process_command_line", lambda pid: None)
        monkeypatch.setattr(pg, "probe_cili_http", lambda *args, **kwargs: False)
        assert pg.is_cili_listener(4242, 8000, "127.0.0.1") is True
        assert pg.is_cili_listener(4242, 8001, "127.0.0.1") is False
        assert pg.is_cili_listener(4243, 8000, "127.0.0.1") is False

    def test_command_line_match_without_server_info(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pg, "SERVER_INFO_PATH", tmp_path / "missing.json")
        monkeypatch.setattr(
            pg, "get_process_command_line",
            lambda pid: f'"python.exe" {pg.PROJECT_ROOT / "main.py"}',
        )
        assert pg.is_cili_listener(4242, 8000, "127.0.0.1") is True

    def test_relative_launch_needs_server_info_corroboration(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pg, "SERVER_INFO_PATH", tmp_path / "missing.json")
        monkeypatch.setattr(
            pg, "get_process_command_line", lambda pid: '"python.exe" main.py'
        )
        monkeypatch.setattr(pg, "probe_cili_http", lambda *args, **kwargs: False)
        assert pg.is_cili_listener(4242, 8000, "127.0.0.1") is False

    def test_http_fingerprint_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pg, "SERVER_INFO_PATH", tmp_path / "missing.json")
        monkeypatch.setattr(
            pg, "get_process_command_line", lambda pid: '"python.exe" main.py'
        )
        monkeypatch.setattr(
            pg, "probe_cili_http", lambda host, port, token="": True
        )
        assert pg.is_cili_listener(4242, 8000, "127.0.0.1") is True


class TestServerInfoFallback:
    def test_requires_matching_port_and_cili_cmdline(self, tmp_path, monkeypatch):
        info = tmp_path / "server.json"
        info.write_text(
            json.dumps({"port": 8000, "pid": 4242}), encoding="utf-8"
        )
        monkeypatch.setattr(pg, "SERVER_INFO_PATH", info)
        monkeypatch.setattr(
            pg, "get_process_command_line",
            lambda pid: f'"python.exe" {pg.PROJECT_ROOT / "main.py"}',
        )
        assert pg._server_info_fallback_pids(8000) == [4242]
        assert pg._server_info_fallback_pids(8001) == []

    def test_rejects_foreign_cmdline(self, tmp_path, monkeypatch):
        info = tmp_path / "server.json"
        info.write_text(
            json.dumps({"port": 8000, "pid": 4242}), encoding="utf-8"
        )
        monkeypatch.setattr(pg, "SERVER_INFO_PATH", info)
        monkeypatch.setattr(
            pg, "get_process_command_line",
            lambda pid: r'"python.exe" C:\other\server.py',
        )
        assert pg._server_info_fallback_pids(8000) == []


class TestHttpProbe:
    def test_detects_cili_title(self):
        server, thread = _start_server(_OpenAPIHandler)
        try:
            assert pg.probe_cili_http(
                "127.0.0.1", server.server_address[1]
            ) is True
        finally:
            _stop_server(server, thread)

    def test_rejects_foreign_title(self):
        server, thread = _start_server(_ForeignOpenAPIHandler)
        try:
            assert pg.probe_cili_http(
                "127.0.0.1", server.server_address[1]
            ) is False
        finally:
            _stop_server(server, thread)


class TestResolveWebPort:
    def test_free_port_used_as_is(self, monkeypatch):
        monkeypatch.setattr(pg, "is_port_free", lambda host, port: True)
        assert pg.resolve_web_port("127.0.0.1", 8000) == 8000

    def test_foreign_occupant_shifts_to_next_port(self, monkeypatch):
        killed = []
        monkeypatch.setattr(
            pg, "is_port_free", lambda host, port: port >= 8001
        )
        monkeypatch.setattr(pg, "find_listener_pids", lambda port: [5555])
        monkeypatch.setattr(
            pg, "is_cili_listener", lambda pid, port, host, token="": False
        )
        monkeypatch.setattr(
            pg, "kill_process", lambda pid: killed.append(pid) or True
        )
        assert pg.resolve_web_port("127.0.0.1", 8000) == 8001
        assert killed == []

    def test_cili_occupant_is_killed_and_port_reused(self, monkeypatch):
        state = {"free": False}
        killed = []

        def _free(host, port):
            return state["free"]

        def _kill(pid):
            killed.append(pid)
            state["free"] = True
            return True

        monkeypatch.setattr(pg, "is_port_free", _free)
        monkeypatch.setattr(pg, "find_listener_pids", lambda port: [4242])
        monkeypatch.setattr(
            pg, "is_cili_listener", lambda pid, port, host, token="": True
        )
        monkeypatch.setattr(pg, "kill_process", _kill)
        monkeypatch.setattr(
            pg, "wait_port_free", lambda host, port, timeout=0: state["free"]
        )
        assert pg.resolve_web_port("127.0.0.1", 8000) == 8000
        assert killed == [4242]

    def test_server_info_fallback_pid_is_killed(self, monkeypatch):
        state = {"free": False}
        killed = []

        def _kill(pid):
            killed.append(pid)
            state["free"] = True
            return True

        monkeypatch.setattr(pg, "is_port_free", lambda host, port: state["free"])
        monkeypatch.setattr(pg, "find_listener_pids", lambda port: [])
        monkeypatch.setattr(pg, "_server_info_fallback_pids", lambda port: [4242])
        monkeypatch.setattr(pg, "kill_process", _kill)
        monkeypatch.setattr(
            pg, "wait_port_free", lambda host, port, timeout=0: state["free"]
        )
        assert pg.resolve_web_port("127.0.0.1", 8000) == 8000
        assert killed == [4242]

    def test_kill_failure_shifts_port(self, monkeypatch):
        monkeypatch.setattr(
            pg, "is_port_free", lambda host, port: port >= 8001
        )
        monkeypatch.setattr(pg, "find_listener_pids", lambda port: [4242])
        monkeypatch.setattr(
            pg, "is_cili_listener", lambda pid, port, host, token="": True
        )
        monkeypatch.setattr(pg, "kill_process", lambda pid: False)
        monkeypatch.setattr(pg, "wait_port_free", lambda *args, **kwargs: False)
        assert pg.resolve_web_port("127.0.0.1", 8000) == 8001

    def test_unidentifiable_listener_shifts(self, monkeypatch):
        monkeypatch.setattr(
            pg, "is_port_free", lambda host, port: port >= 8001
        )
        monkeypatch.setattr(pg, "find_listener_pids", lambda port: [])
        monkeypatch.setattr(pg, "_server_info_fallback_pids", lambda port: [])
        assert pg.resolve_web_port("127.0.0.1", 8000) == 8001

    def test_os_assigned_fallback_when_scan_exhausted(self, monkeypatch):
        monkeypatch.setattr(pg, "is_port_free", lambda host, port: False)
        monkeypatch.setattr(pg, "find_listener_pids", lambda port: [])
        monkeypatch.setattr(pg, "_server_info_fallback_pids", lambda port: [])
        monkeypatch.setattr(pg, "_pick_os_port", lambda host: 54321)
        assert pg.resolve_web_port("127.0.0.1", 8000, max_scan=3) == 54321

    def test_own_pid_is_never_killed(self):
        assert pg.kill_process(os.getpid()) is False
        assert pg.kill_process(0) is False
