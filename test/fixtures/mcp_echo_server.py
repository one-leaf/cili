"""MCP 测试回显服务器：stdio 传输，暴露 echo(text) -> str。

用 MCPServer（mcp 2.x，FastMCP 的继任）自带 initialize/list_tools/call_tool
协议处理，供 test_mcp.py 做端到端验证。运行方式：python mcp_echo_server.py
"""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="echo-server")


@mcp.tool()
def echo(text: str) -> str:
    """把输入文本原样回显，前缀 echo:。"""
    return f"echo:{text}"


if __name__ == "__main__":
    mcp.run()
