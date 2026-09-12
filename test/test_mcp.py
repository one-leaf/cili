"""MCP 服务器支持测试（streamableHttp echo server 端到端）。

依赖 test/fixtures/mcp_echo_server.py（MCPServer streamable-http），用系统
python 拉起 uvicorn 子进程。MCP SDK 为异步，本模块通过 MCPProvider 后台
asyncio loop 桥接为同步 execute。
"""

import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_dir)

from core.config import MCPConfig
from core.tools.base import UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END
from core.tools.mcp import (
    MCPProvider,
    _sanitize_mcp_tool_name,
    get_provider,
    stop_mcp_provider,
)

from test.conftest import make_dgx_config

FIXTURE_SERVER = os.path.join(project_dir, "test", "fixtures", "mcp_echo_server.py")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port: int, timeout: float = 20) -> None:
    """轮询 /mcp 端点直到应用就绪（4xx 响应也视为就绪）。"""
    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                resp.read()
            return
        except urllib.error.HTTPError:
            return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError(f"echo server 未在 {timeout}s 内就绪 (port {port})")


@pytest.fixture(scope="module")
def echo_server_url():
    """拉起 streamableHttp echo server 子进程，返回 /mcp 端点 URL。"""
    port = _find_free_port()
    proc = subprocess.Popen(
        [sys.executable, FIXTURE_SERVER, "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_ready(port)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def provider():
    p = MCPProvider(connect_timeout=15)
    p.start()
    yield p
    p.close()


def make_echo_config(echo_url: str, **overrides) -> MCPConfig:
    base = dict(
        url=echo_url,
        tool_timeout=10,
    )
    base.update(overrides)
    return MCPConfig(**base)


# ── MCPConfig 序列化 ──────────────────────────────────────────────────────

def test_mcp_config_roundtrip():
    cfg = MCPConfig(
        url="http://localhost:3000/mcp",
        headers={"Authorization": "Bearer abcdefghijkl"},
        tool_timeout=10,
        enabled_tools=["*"],
    )
    d = cfg.to_dict()
    assert d["headers"] == {"Authorization": "Bearer abcdefghijkl"}
    cfg2 = MCPConfig.from_dict(d)
    assert cfg2 == cfg
    # 旧配置里的 stdio 字段被忽略；默认白名单为全部
    auto = MCPConfig.from_dict({"command": "npx", "type": "stdio"})
    assert auto.url == ""
    assert auto.enabled_tools == ["*"]


def test_sanitize_tool_name():
    # - 是模型 API 合法字符，保留；空格等非法字符替换为 _
    assert _sanitize_mcp_tool_name("mcp_my-server_echo") == "mcp_my-server_echo"
    assert _sanitize_mcp_tool_name("mcp bad name") == "mcp_bad_name"
    assert _sanitize_mcp_tool_name("mcp_a_b_c") == "mcp_a_b_c"
    long = "mcp_" + "x" * 200 + "_echo"
    assert len(_sanitize_mcp_tool_name(long)) <= 64


# ── provider 连接 + wrapper 执行 ─────────────────────────────────────────

def test_provider_connect_and_wrappers(provider, echo_server_url):
    provider.ensure_connected({"echo": make_echo_config(echo_server_url)})
    assert provider.status()["echo"]["status"] == "connected"
    wrappers = provider.get_wrappers()
    assert len(wrappers) == 1
    w = wrappers[0]
    assert w.name == "mcp_echo_echo"
    assert w.parameters.get("type") == "object"
    assert "text" in w.parameters.get("properties", {})


def test_wrapper_execute(provider, echo_server_url):
    provider.ensure_connected({"echo": make_echo_config(echo_server_url)})
    w = provider.get_wrappers()[0]
    result = w.execute(text="hi")
    assert not result.is_error
    assert "echo:hi" in result.output
    # 提示注入防护包裹
    assert UNTRUSTED_DATA_BEGIN in result.output
    assert UNTRUSTED_DATA_END in result.output


def test_execute_disconnected(provider):
    # 未连接的 server → 直接报错而非崩溃
    result = provider.run_in_loop(provider.execute_tool("nope", "echo", {"text": "x"}, 5))
    assert result.is_error


def test_ensure_connected_noop(provider, echo_server_url):
    """配置签名无变化时 ensure_connected 不重连（避免每次 rebuild 都重连）。"""
    cfg = make_echo_config(echo_server_url)
    provider.ensure_connected({"echo": cfg})
    first = provider.get_wrappers()
    provider.ensure_connected({"echo": cfg})
    assert provider.get_wrappers() is first or provider.get_wrappers() == first


def test_open_session_requires_url(provider):
    # 未配置 url → 报错而非崩溃
    cfg = MCPConfig()
    result = provider.test_connect(cfg)
    assert result["status"] == "failed"
    assert "url" in result["error"]


# ── agent 注入（deferred） ───────────────────────────────────────────────

def test_agent_injection_deferred(tmp_path, echo_server_url):
    try:
        cfg = make_dgx_config()
        cfg.mcp_servers = {"echo": make_echo_config(echo_server_url)}

        from core.agent import Agent

        ws_uuid = secrets.token_hex(4)
        agent = Agent(cfg, role="master", cwd=str(tmp_path), workspace_uuid=ws_uuid)
        try:
            # MCP 工具默认进 deferred，不污染 LLM 上下文
            assert any(t.name == "mcp_echo_echo" for t in agent._deferred_tools)
            assert not any(t.name == "mcp_echo_echo" for t in agent._active_tools)

            # tool_search 激活后进入 active，schema 出现在 tool_schemas
            agent._activate_tools(["mcp_echo_echo"])
            assert any(t.name == "mcp_echo_echo" for t in agent._active_tools)
            names = [s["name"] for s in agent.tool_schemas]
            assert "mcp_echo_echo" in names
        finally:
            agent.cleanup()
            base = os.path.join(project_dir, "data", "agents", ws_uuid)
            if os.path.exists(base):
                shutil.rmtree(base, ignore_errors=True)
    finally:
        stop_mcp_provider()
