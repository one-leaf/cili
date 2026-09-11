"""MCP 服务器支持测试（stdio echo server 端到端）。

依赖 test/fixtures/mcp_echo_server.py（FastMCP stdio 服务器），用系统
python 直接拉起子进程，避免 npx 下载。MCP SDK 为异步，本模块通过
MCPProvider 后台 asyncio loop 桥接为同步 execute。
"""

import os
import secrets
import shutil
import sys

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


def make_echo_config(**overrides) -> MCPConfig:
    """echo server 配置（.exe 无需 cmd 包装，避开 npx 下载）。"""
    base = dict(
        type="stdio",
        command=sys.executable,
        args=[FIXTURE_SERVER],
        tool_timeout=10,
    )
    base.update(overrides)
    return MCPConfig(**base)


@pytest.fixture
def provider():
    p = MCPProvider(connect_timeout=15)
    p.start()
    yield p
    p.close()


# ── MCPConfig 序列化 ──────────────────────────────────────────────────────

def test_mcp_config_roundtrip():
    cfg = make_echo_config(
        url="http://localhost:3000/mcp",
        headers={"Authorization": "Bearer abcdefghijkl"},
        env={"FOO": "bar"},
        enabled_tools=["*"],
    )
    d = cfg.to_dict()
    assert d["headers"] == {"Authorization": "Bearer abcdefghijkl"}
    cfg2 = MCPConfig.from_dict(d)
    assert cfg2 == cfg
    # 自动检测 type：command/url 均未设置时留空
    auto = MCPConfig.from_dict({"command": "npx"})
    assert auto.type == ""
    assert auto.command == "npx"
    assert auto.enabled_tools == ["*"]


def test_sanitize_tool_name():
    # - 是模型 API 合法字符，保留；空格等非法字符替换为 _
    assert _sanitize_mcp_tool_name("mcp_my-server_echo") == "mcp_my-server_echo"
    assert _sanitize_mcp_tool_name("mcp bad name") == "mcp_bad_name"
    assert _sanitize_mcp_tool_name("mcp_a_b_c") == "mcp_a_b_c"
    long = "mcp_" + "x" * 200 + "_echo"
    assert len(_sanitize_mcp_tool_name(long)) <= 64


# ── provider 连接 + wrapper 执行 ─────────────────────────────────────────

def test_provider_connect_and_wrappers(provider):
    provider.ensure_connected({"echo": make_echo_config()})
    assert provider.status()["echo"]["status"] == "connected"
    wrappers = provider.get_wrappers()
    assert len(wrappers) == 1
    w = wrappers[0]
    assert w.name == "mcp_echo_echo"
    assert w.parameters.get("type") == "object"
    assert "text" in w.parameters.get("properties", {})


def test_wrapper_execute(provider):
    provider.ensure_connected({"echo": make_echo_config()})
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


def test_ensure_connected_noop(provider):
    """配置签名无变化时 ensure_connected 不重连（避免每次 rebuild 都重连）。"""
    cfg = make_echo_config()
    provider.ensure_connected({"echo": cfg})
    first = provider.get_wrappers()
    provider.ensure_connected({"echo": cfg})
    assert provider.get_wrappers() is first or provider.get_wrappers() == first


# ── agent 注入（deferred） ───────────────────────────────────────────────

def test_agent_injection_deferred(tmp_path):
    try:
        cfg = make_dgx_config()
        cfg.mcp_servers = {"echo": make_echo_config()}

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
