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

from core.tools.approval import (
    META_KEY,
    approval_decision_id,
    approval_placeholder_text,
)
from core.tools.base import Tool, ToolResult


class BrowserTool(Tool):
    name = "browser"
    description = """Connect to a real Chrome browser for sites that block bots or need JS rendering.

Automatically connects to Chrome with remote debugging (restarts Chrome with the workspace
profile if not running with debugging).

Available actions:
- navigate: open a URL and get page content (opens a new tab, returns tab_index).
  Non-public (localhost/private IP) URLs require user approval first.
- snapshot / find: get the accessibility tree with refs (r1, r2, ...) for element interaction
- click / fill / type / press: interact with elements by their ref from snapshot
  (click supports button='right'/'middle' for right/middle click)
- hover / drag: hover an element, or drag one ref onto another
- upload: upload a local file to a file input (ref)
- fill_form: batch-fill multiple fields: fields=[{ref, value}, ...]
- scroll: scroll down/up/top/bottom, or to_element (by ref)
- extract: extract text/attributes from elements matching a CSS selector
- extract_table: convert the page's Nth <table> to a markdown table
- download: download a file by clicking a ref or navigating to a URL (save_path)
- dialogs: read JS dialogs (alert/confirm/prompt) that were auto-dismissed
- get_frames: list iframes; pass frame=<index> to any ref/scroll action to target one
- get_cookies / clear_cookies / set_cookie: manage browser cookies
- go_back / go_forward / reload: browser navigation
- console / requests: inspect console messages and network requests
  (requests supports details/body to expand headers and response bodies)
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

Frames:
- Run 'get_frames' to list frames; frame 0 is the main frame.
- Pass frame=<index> to snapshot/find/click/fill/type/press/hover/drag/upload/extract/scroll
  to operate inside an iframe (refs are per frame).

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
                         "get_links", "wait_for", "switch_tab", "list_tabs", "close_tab",
                         "scroll", "download", "fill_form", "extract", "extract_table",
                         "dialogs", "upload", "hover", "drag", "get_frames",
                         "get_cookies", "clear_cookies", "set_cookie"],
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
            "frame": {
                "type": "integer",
                "description": "Frame index to operate in (see 'get_frames'). 0 or omitted = main frame. "
                               "Used by snapshot/find/click/fill/type/press/hover/drag/upload/"
                               "fill_form/extract/extract_table/scroll.",
            },
            "button": {
                "type": "string",
                "enum": ["left", "right", "middle"],
                "description": "Mouse button for 'click' (default: left; right = right-click).",
            },
            "direction": {
                "type": "string",
                "enum": ["down", "up", "top", "bottom", "to_element"],
                "description": "Scroll direction for 'scroll' action (default: down). "
                               "'to_element' scrolls the ref element into view.",
            },
            "amount": {
                "type": "integer",
                "description": "Pixels to scroll for 'scroll' down/up (default: 800).",
            },
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string"},
                        "value": {"type": "string"},
                    },
                },
                "description": "List of {ref, value} to fill for 'fill_form' action.",
            },
            "attribute": {
                "type": "string",
                "description": "Attribute to extract for 'extract' action (default: 'text').",
            },
            "limit": {
                "type": "integer",
                "description": "Max elements for 'extract' action (default: 50).",
            },
            "index": {
                "type": "integer",
                "description": "Table index for 'extract_table' action (default: 0).",
            },
            "cookie": {
                "type": "object",
                "description": "Cookie to set for 'set_cookie' action: {name, value, domain, path, ...}.",
            },
            "details": {
                "type": "boolean",
                "description": "Expand request/response headers for 'requests' action.",
            },
            "body": {
                "type": "boolean",
                "description": "Include captured response bodies for 'requests' action (with details).",
            },
            "save_path": {
                "type": "string",
                "description": "File path to save the download to (default: suggested filename in workspace). "
                               "Relative paths resolve against the working directory.",
            },
            "target_ref": {
                "type": "string",
                "description": "Target element ref for 'drag' action (drop target).",
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
        # 懒导入避免 core.browser_service → core.tools.base 触发 registry 的循环依赖
        from core.browser_service import _validate_navigate_url, get_service

        service = get_service()
        if not service.is_running():
            return ToolResult("Error: Browser service not available", error=True)

        # frame 索引 int 化兜底（schema 声明 integer，但防止模型传字符串）
        frame = kwargs.get("frame")
        if frame is not None:
            try:
                frame = int(frame)
            except (TypeError, ValueError):
                return ToolResult("Error: 'frame' must be an integer", error=True)

        # 根据 action 委托给 service 的对应方法
        if action == "navigate":
            if not url:
                return ToolResult("Error: 'url' is required for navigate action", error=True)
            # SSRF 防护：非公网地址导航走审批门（ask 档）——master 弹卡询问、
            # worker/lite 由循环降级为硬拒绝；已批准（含「允许并记住」）后放行。
            block_reason = _validate_navigate_url(url)
            if block_reason:
                approval = {
                    "kind": "browser:navigate",
                    "decision_id": approval_decision_id(url),
                    "command": url,
                    "reason": block_reason,
                }
                if not self.approval_store or not self.approval_store.is_approved(approval["decision_id"]):
                    return ToolResult(
                        approval_placeholder_text(approval),
                        completed=False,
                        meta={META_KEY: approval},
                    )
                return service.navigate(url, tab_index=tab_index, skip_ssrf=True)
            return service.navigate(url, tab_index=tab_index)

        elif action == "snapshot":
            return service.snapshot(tab_index=tab_index, frame=frame)

        elif action == "find":
            pattern = kwargs.get("pattern")
            if not pattern:
                return ToolResult("Error: 'pattern' is required for find action", error=True)
            return service.find(pattern, tab_index=tab_index, frame=frame)

        elif action == "click":
            ref = kwargs.get("ref")
            if not ref:
                return ToolResult("Error: 'ref' is required for click action", error=True)
            button = kwargs.get("button", "left") or "left"
            return service.click(ref, button=button, tab_index=tab_index, frame=frame)

        elif action == "fill":
            ref = kwargs.get("ref")
            text = kwargs.get("text")
            if not ref:
                return ToolResult("Error: 'ref' is required for fill action", error=True)
            if text is None:
                return ToolResult("Error: 'text' is required for fill action", error=True)
            return service.fill(ref, text, tab_index=tab_index, frame=frame)

        elif action == "type":
            ref = kwargs.get("ref")
            text = kwargs.get("text")
            if not ref:
                return ToolResult("Error: 'ref' is required for type action", error=True)
            if text is None:
                return ToolResult("Error: 'text' is required for type action", error=True)
            return service.type(ref, text, tab_index=tab_index, frame=frame)

        elif action == "press":
            key = kwargs.get("key")
            if not key:
                return ToolResult("Error: 'key' is required for press action", error=True)
            return service.press(kwargs.get("ref"), key, tab_index=tab_index, frame=frame)

        elif action == "go_back":
            return service.go_back(tab_index=tab_index)

        elif action == "go_forward":
            return service.go_forward(tab_index=tab_index)

        elif action == "reload":
            return service.reload(tab_index=tab_index)

        elif action == "console":
            return service.console(clear=bool(kwargs.get("clear")), tab_index=tab_index)

        elif action == "requests":
            return service.requests(
                clear=bool(kwargs.get("clear")),
                details=bool(kwargs.get("details")),
                body=bool(kwargs.get("body")),
                tab_index=tab_index,
            )

        elif action == "scroll":
            direction = kwargs.get("direction", "down") or "down"
            ref = kwargs.get("ref")
            return service.scroll(
                direction=direction,
                amount=int(kwargs.get("amount") or 800),
                ref=ref,
                frame=frame,
                tab_index=tab_index,
            )

        elif action == "hover":
            ref = kwargs.get("ref")
            if not ref:
                return ToolResult("Error: 'ref' is required for hover action", error=True)
            return service.hover(ref, frame=frame, tab_index=tab_index)

        elif action == "drag":
            ref = kwargs.get("ref")
            target_ref = kwargs.get("target_ref")
            if not ref or not target_ref:
                return ToolResult("Error: drag requires 'ref' and 'target_ref'", error=True)
            return service.drag(ref, target_ref, frame=frame, tab_index=tab_index)

        elif action == "upload":
            ref = kwargs.get("ref")
            path = kwargs.get("path")
            if not ref:
                return ToolResult("Error: 'ref' is required for upload action", error=True)
            if not path:
                return ToolResult("Error: 'path' is required for upload action", error=True)
            if not os.path.isabs(path):
                path = os.path.join(self.cwd, path)
            return service.upload(ref, path, frame=frame, tab_index=tab_index)

        elif action == "fill_form":
            fields = kwargs.get("fields")
            if not fields:
                return ToolResult("Error: 'fields' (list of {ref, value}) is required for fill_form", error=True)
            return service.fill_form(fields, frame=frame, tab_index=tab_index)

        elif action == "extract":
            selector = kwargs.get("selector")
            if not selector:
                return ToolResult("Error: 'selector' is required for extract action", error=True)
            return service.extract(
                selector,
                attribute=kwargs.get("attribute", "text") or "text",
                limit=int(kwargs.get("limit") or 50),
                frame=frame,
                tab_index=tab_index,
            )

        elif action == "extract_table":
            return service.extract_table(
                index=int(kwargs.get("index") or 0),
                frame=frame,
                tab_index=tab_index,
            )

        elif action == "download":
            ref = kwargs.get("ref")
            dl_url = kwargs.get("url")
            save_path = kwargs.get("save_path")
            if not ref and not dl_url:
                return ToolResult("Error: download requires 'ref' (trigger) or 'url'", error=True)
            if save_path and not os.path.isabs(save_path):
                save_path = os.path.join(self.cwd, save_path)
            return service.download(ref=ref, url=dl_url, save_path=save_path, tab_index=tab_index)

        elif action == "dialogs":
            return service.dialogs(clear=bool(kwargs.get("clear")), tab_index=tab_index)

        elif action == "get_frames":
            return service.get_frames(tab_index=tab_index)

        elif action == "get_cookies":
            return service.get_cookies(url=url, tab_index=tab_index)

        elif action == "clear_cookies":
            return service.clear_cookies(tab_index=tab_index)

        elif action == "set_cookie":
            cookie = kwargs.get("cookie")
            if not cookie:
                return ToolResult("Error: 'cookie' object is required for set_cookie action", error=True)
            return service.set_cookie(cookie, tab_index=tab_index)

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
