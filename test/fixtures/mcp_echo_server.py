"""MCP 测试回显服务器：streamableHttp 传输，暴露 echo(text) -> str。

用 MCPServer（mcp 2.x）自带 streamable-http 运行（uvicorn），供 test_mcp.py
做端到端验证。运行方式：python mcp_echo_server.py --port 8765
"""

import argparse

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="echo-server")


@mcp.tool()
def echo(text: str) -> str:
    """把输入文本原样回显，前缀 echo:。"""
    return f"echo:{text}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP echo server (streamableHttp)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=args.port,
        streamable_http_path="/mcp",
    )
