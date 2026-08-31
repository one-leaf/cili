"""Browser tool - 委托给 BrowserService 进行浏览器操作。

所有 Playwright 和 Chrome 进程管理已迁移到 core/browser_service.py。
本工具仅负责：
1. 提供 Tool 接口给 LLM 调用
2. 将操作委托给全局 BrowserService

Tab 管理：每次 navigate 创建新 tab 并返回 tab_index，后续操作可通过
tab_index 指定目标 tab。Inactive tab 在 10 分钟后自动关闭。
"""

from __future__ import annotations

import os

from core.tools.shared.base import Tool, ToolResult


class BrowserTool(Tool):
    name = "browser"
    description = """Connect to a browser to access websites that block bots.

This tool automatically connects to Chrome with remote debugging. If Chrome is not
running with debugging enabled, it will kill existing Chrome and restart with the
workspace profile (workspace/.chrome-profile).

Available actions:
- navigate: Go to a URL and get page content (opens a new tab, returns tab_index)
- screenshot: Take a screenshot of the current page
- save_pdf: Save the current page as a PDF file
- execute: Run JavaScript on the page
- get_text: Get all text content from the page
- get_links: Get all links from the page
- wait_for: Wait for a CSS selector to appear (useful for JS-rendered content)
- switch_tab: Switch to a specific tab by tab_index
- list_tabs: List all open tabs with their tab_index
- close_tab: Close a specific tab by tab_index

Tab management:
- Each 'navigate' opens a new tab and returns a tab_index.
- Use 'tab_index' parameter to specify which tab to operate on.
- If tab_index is omitted, the current active tab is used.
- Inactive tabs are automatically closed after 10 minutes.
- Use 'close_tab' to manually close a tab when done exploring.

Examples:
- {"action": "navigate", "url": "https://example.com"}  → returns tab_index
- {"action": "screenshot", "tab_index": 1}
- {"action": "switch_tab", "tab_index": 2}
- {"action": "close_tab", "tab_index": 1}
"""
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "screenshot", "save_pdf", "execute", "get_text",
                         "get_links", "wait_for", "switch_tab", "list_tabs", "close_tab"],
                "description": "Action to perform on the browser.",
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to (required for 'navigate' action).",
            },
            "script": {
                "type": "string",
                "description": "JavaScript code to execute (required for 'execute' action).",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector to wait for (used with 'wait_for' action).",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in milliseconds for wait_for action (default: 10000).",
            },
            "screenshot_path": {
                "type": "string",
                "description": "Path to save screenshot (default: screenshot.png).",
            },
            "pdf_path": {
                "type": "string",
                "description": "Path to save PDF (default: page.pdf).",
            },
            "tab_index": {
                "type": "integer",
                "description": "Tab index to operate on. If omitted, uses the current active tab. "
                               "Returned by 'navigate' action.",
            },
        },
        "required": ["action"],
    }

    def execute(
        self,
        action: str,
        url: str | None = None,
        script: str | None = None,
        screenshot_path: str | None = None,
        pdf_path: str | None = None,
        tab_index: int | None = None,
        **kwargs,
    ) -> ToolResult:
        """执行浏览器操作，委托给 BrowserService。"""
        from core.browser_service import get_service

        service = get_service()
        if not service.is_running():
            return ToolResult("Error: Browser service not available", error=True)

        # 根据 action 委托给 service 的对应方法
        if action == "navigate":
            if not url:
                return ToolResult("Error: 'url' is required for navigate action", error=True)
            return service.navigate(url, tab_index=tab_index)

        elif action == "screenshot":
            path = screenshot_path or os.path.join(self.cwd, "screenshot.png")
            if not os.path.isabs(path):
                path = os.path.join(self.cwd, path)
            return service.screenshot(path, tab_index=tab_index)

        elif action == "save_pdf":
            path = pdf_path or os.path.join(self.cwd, "page.pdf")
            if not os.path.isabs(path):
                path = os.path.join(self.cwd, path)
            return service.save_pdf(path, tab_index=tab_index)

        elif action == "execute":
            if not script:
                return ToolResult("Error: 'script' is required for execute action", error=True)
            return service.execute_script(script, tab_index=tab_index)

        elif action == "get_text":
            return service.get_text(tab_index=tab_index)

        elif action == "get_links":
            return service.get_links(tab_index=tab_index)

        elif action == "wait_for":
            selector = kwargs.get("selector")
            if not selector:
                return ToolResult("Error: 'selector' is required for wait_for action", error=True)
            timeout = kwargs.get("timeout", 10000)
            return service.wait_for(selector, timeout, tab_index=tab_index)

        elif action == "switch_tab":
            if tab_index is None:
                return ToolResult("Error: 'tab_index' is required for switch_tab action", error=True)
            return service.switch_tab(tab_index)

        elif action == "list_tabs":
            return service.list_tabs()

        elif action == "close_tab":
            if tab_index is None:
                return ToolResult("Error: 'tab_index' is required for close_tab action", error=True)
            return service.close_tab(tab_index)

        else:
            return ToolResult(f"Error: unknown action '{action}'", error=True)

    def close(self) -> None:
        """断开浏览器连接，保持 Chrome 进程运行。"""
        try:
            from core.browser_service import get_service
            get_service().disconnect()
        except Exception:
            pass

    def kill_chrome(self) -> None:
        """终止 Chrome 进程。"""
        try:
            from core.browser_service import get_service
            get_service().kill_chrome()
        except Exception:
            pass
