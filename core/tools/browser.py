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

from core.tools.base import Tool, ToolResult


class BrowserTool(Tool):
    name = "browser"
    description = """Connect to a real Chrome browser for sites that block bots or need JS rendering.

Automatically connects to Chrome with remote debugging (restarts Chrome with the workspace
profile if not running with debugging).

Available actions:
- navigate: open a URL and get page content (opens a new tab, returns tab_index)
- snapshot: get the accessibility tree with refs (r1, r2, ...) for element interaction
- find: search the snapshot for elements matching a role or name pattern
- click / fill / type / press: interact with elements by their ref from snapshot
- go_back / go_forward / reload: browser navigation
- console / requests: inspect console messages and network requests
- screenshot: capture the page
- save_pdf: save the page as a PDF
- execute: run JavaScript on the page
- get_text / get_links: extract page text or links
- wait_for: wait for a CSS selector (for JS-rendered content)
- switch_tab / list_tabs / close_tab: manage tabs by tab_index

Element interaction flow:
1. Run 'snapshot' to get the accessibility tree (each interactive element has a ref like r3).
2. Interact with elements by ref: click(ref="r3"), fill(ref="r2", text="..."),
   type(ref="r2", text="..."), press(ref="r3", key="Enter").
3. Re-run 'snapshot' after the page changes (refs are only valid for the snapshot that produced them).

Tab management:
- Each 'navigate' opens a new tab; pass its tab_index to operate on it (defaults to the active tab).
- Inactive tabs auto-close after 10 minutes; call close_tab when done exploring.

Use web_search for simple lookups; use browser when search results are insufficient or the site needs a real browser.
"""
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "snapshot", "find", "click", "fill", "type", "press",
                         "go_back", "go_forward", "reload", "console", "requests",
                         "screenshot", "save_pdf", "execute", "get_text",
                         "get_links", "wait_for", "switch_tab", "list_tabs", "close_tab"],
                "description": "Action to perform on the browser.",
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to (required for 'navigate' action).",
            },
            "ref": {
                "type": "string",
                "description": "Element ref (like 'r3') from the 'snapshot' action. Used by "
                               "click/fill/type/press to target a specific element.",
            },
            "text": {
                "type": "string",
                "description": "Text to fill or type into the element (used with 'fill'/'type').",
            },
            "pattern": {
                "type": "string",
                "description": "Pattern (regex or substring) to match element role or name "
                               "in the snapshot (used with 'find').",
            },
            "key": {
                "type": "string",
                "description": "Key to press (Enter/Tab/Escape/ArrowDown/Backspace, etc.). "
                               "Used with 'press'. Omit ref to press at page level.",
            },
            "clear": {
                "type": "boolean",
                "description": "Clear the console/request buffer after reading "
                               "(used with 'console'/'requests').",
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

        elif action == "snapshot":
            return service.snapshot(tab_index=tab_index)

        elif action == "find":
            pattern = kwargs.get("pattern")
            if not pattern:
                return ToolResult("Error: 'pattern' is required for find action", error=True)
            return service.find(pattern, tab_index=tab_index)

        elif action == "click":
            ref = kwargs.get("ref")
            if not ref:
                return ToolResult("Error: 'ref' is required for click action", error=True)
            return service.click(ref, tab_index=tab_index)

        elif action == "fill":
            ref = kwargs.get("ref")
            text = kwargs.get("text")
            if not ref:
                return ToolResult("Error: 'ref' is required for fill action", error=True)
            if text is None:
                return ToolResult("Error: 'text' is required for fill action", error=True)
            return service.fill(ref, text, tab_index=tab_index)

        elif action == "type":
            ref = kwargs.get("ref")
            text = kwargs.get("text")
            if not ref:
                return ToolResult("Error: 'ref' is required for type action", error=True)
            if text is None:
                return ToolResult("Error: 'text' is required for type action", error=True)
            return service.type(ref, text, tab_index=tab_index)

        elif action == "press":
            key = kwargs.get("key")
            if not key:
                return ToolResult("Error: 'key' is required for press action", error=True)
            return service.press(kwargs.get("ref"), key, tab_index=tab_index)

        elif action == "go_back":
            return service.go_back(tab_index=tab_index)

        elif action == "go_forward":
            return service.go_forward(tab_index=tab_index)

        elif action == "reload":
            return service.reload(tab_index=tab_index)

        elif action == "console":
            return service.console(clear=bool(kwargs.get("clear")), tab_index=tab_index)

        elif action == "requests":
            return service.requests(clear=bool(kwargs.get("clear")), tab_index=tab_index)

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
