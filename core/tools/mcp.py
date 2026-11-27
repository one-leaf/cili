"""MCP 工具包装：把单个 MCP 工具桥接为 Cili 的同步 Tool。

连接管理在 ``core/mcp_client.py``；本模块只保留 ``MCPToolWrapper``。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from core.mcp_naming import _sanitize_mcp_tool_name
from core.tools.base import (
    UNTRUSTED_DATA_BEGIN,
    UNTRUSTED_DATA_END,
    Tool,
    ToolResult,
)

if TYPE_CHECKING:
    from core.mcp_client import MCPProvider

class MCPToolWrapper(Tool):
    """把单个 MCP 工具包装为 Cili 的同步 Tool。

    关键设计：wrapper 只持 provider 引用 + 所属工作区、不持 session。每次
    execute 经 provider 取当前 session，因此 server 重连后 wrapper 自动使用新连接。
    """

    def __init__(
        self,
        provider: "MCPProvider",
        workspace_uuid: str,
        server_name: str,
        tool_name: str,
        description: str,
        parameters: dict,
        tool_timeout: int = 30,
        cwd: str = ".",
        **kwargs,
    ):
        # 有意例外：不接收 session/workspace_uuid（见类 docstring，session 每次经
        # provider 现取）。**kwargs 仅用于容忍统一构造协议注入的额外参数。
        super().__init__(cwd=cwd)
        self._provider = provider
        self._workspace_uuid = workspace_uuid
        self._server_name = server_name
        self._tool_name = tool_name  # 原始 MCP 工具名（call_tool 用）
        self._tool_timeout = tool_timeout
        self.name = _sanitize_mcp_tool_name(f"mcp_{server_name}_{tool_name}")
        base_description = description or tool_name
        self.description = (
            f"{base_description}\n\nLarge results are saved to a file; "
            "if the result is truncated, read the full text with `read_tool_result`."
        )
        self.parameters = parameters or {"type": "object", "properties": {}}

    def execute(self, **kwargs: Any) -> ToolResult:
        try:
            result = self._provider.run_in_loop(
                self._provider.execute_tool(
                    self._workspace_uuid, self._server_name, self._tool_name,
                    kwargs, self._tool_timeout,
                ),
                timeout=self._tool_timeout + 10,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                f"Error: MCP tool '{self.name}' 调用超时（{self._tool_timeout} 秒）",
                error=True,
            )
        except Exception as exc:
            return ToolResult(f"Error: MCP tool '{self.name}' 调用失败: {exc}", error=True)

        if result.is_error:
            return result
        # 提示注入防护（SEC-18）：MCP 结果视为外部不可信数据
        return ToolResult(
            output=f"{UNTRUSTED_DATA_BEGIN}{result.output}{UNTRUSTED_DATA_END}"
        )
