"""Web search tool - 使用浏览器进行网页搜索。

所有浏览器管理已委托给 BrowserService，本工具只负责：
1. 构建搜索 URL（支持 Bing 和 Google）
2. 调用 service 进行导航和提取
3. 解析和格式化搜索结果

Tab 管理：每次搜索自动创建新 tab 并返回 tab_index，后续操作使用该 tab_index。
旧 tab 由 BrowserService 10 分钟后自动关闭。
"""

from __future__ import annotations

import json
from urllib.parse import quote_plus

from core.tools.shared.base import Tool, ToolResult


# 搜索引擎配置
SEARCH_CONFIGS = {
    "bing": {
        "name": "Bing",
        "url_template": "https://cn.bing.com/search?q={query}",
        "wait_selector": ".b_algo",
        "error_domain": "Bing",
    },
    "google": {
        "name": "Google",
        "url_template": "https://www.google.com/search?q={query}",
        "wait_selector": "#rso",
        "error_domain": "Google",
    },
}


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web using Bing or Google. Returns search results with titles, URLs, "
        "and descriptions. Also returns the tab_index of the search results page, "
        "which can be used with browser tool for further exploration. "
        "The search engine is configured in system settings (default: Bing)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query string.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 10, max: 30).",
            },
        },
        "required": ["query"],
    }

    def execute(
        self,
        query: str,
        max_results: int = 10,
    ) -> ToolResult:
        """使用浏览器访问 cn.bing.com 进行搜索。"""
        if not query:
            return ToolResult("Error: query is required", error=True)

        # 限制结果数量（强制转int，防止LLM传字符串）
        try:
            max_results = int(max_results)
        except (ValueError, TypeError):
            max_results = 10
        max_results = min(max(1, max_results), 30)

        # 获取浏览器服务
        from core.browser_service import get_service
        from core.config import load_global_config

        service = get_service()
        if not service.is_running():
            return ToolResult("Error: Browser service not available", error=True)

        # 获取搜索引擎配置
        config_data = load_global_config()
        engine = config_data.get("system", {}).get("search_engine", "bing")
        search_config = SEARCH_CONFIGS.get(engine, SEARCH_CONFIGS["bing"])
        engine_name = search_config["name"]
        error_domain = search_config["error_domain"]

        # 导航到搜索结果页面（每次搜索开新 tab）
        search_url = search_config["url_template"].format(query=quote_plus(query))
        nav_result = service.navigate(search_url)

        if nav_result.error:
            return ToolResult(f"Error: failed to access {error_domain}: {nav_result.output}", error=True)

        # 从 navigate 结果中获取 tab_index
        tab_index = nav_result.meta.get("tab_index") if nav_result.meta else None

        # 使用 try/finally 确保所有路径都关闭 tab
        try:
            # 等待搜索结果加载（使用同一个 tab）
            service.wait_for(search_config["wait_selector"], timeout=15000, tab_index=tab_index)

            # 根据搜索引擎使用不同的 JS 提取代码
            if engine == "google":
                js_code = self._get_google_js(max_results)
            else:
                js_code = self._get_bing_js(max_results)

            # 执行 JS（使用同一个 tab）
            extract_result = service.execute_script(js_code, tab_index=tab_index)

            if extract_result.error:
                return ToolResult(f"Error: failed to extract results: {extract_result.output}", error=True)

            # 解析 JSON 结果
            try:
                output = extract_result.output
                if output.startswith("JavaScript result:"):
                    output = output[len("JavaScript result:"):].strip()
                if '{' in output:
                    json_start = output.index('{')
                    output = output[json_start:]
                data = json.loads(output)
            except (json.JSONDecodeError, ValueError) as e:
                return ToolResult(f"Error: failed to parse results: {e}\n\nRaw output: {extract_result.output[:500]}", error=True)

            # 格式化输出
            output_lines = [
                f"Search results for: {query}",
                f"Engine: {engine_name}",
                f"URL: {data.get('url', '')}",
                f"Found {len(data.get('results', []))} results",
            ]

            output_lines.append("")

            for i, result in enumerate(data.get('results', []), 1):
                output_lines.append(f"{i}. {result.get('title', 'No title')}")
                output_lines.append(f"   {result.get('url', '')}")
                if result.get('snippet'):
                    output_lines.append(f"   {result['snippet']}")
                output_lines.append("")

            # 如果结果为 0 且有调试信息，附加到输出
            debug = data.get('_debug')
            if not data.get('results') and debug:
                output_lines.append("[Debug Info]")
                output_lines.append(f"  #rso exists: {debug.get('rsoExists', 'N/A')}")
                output_lines.append(f"  .g elements: {debug.get('gCount', 'N/A')}")
                body_text = debug.get('bodyText', '')[:300]
                if body_text:
                    output_lines.append(f"  Page text preview: {body_text}")

            return ToolResult("\n".join(output_lines))

        finally:
            # 搜索完成，关闭 tab 释放资源（所有路径都会执行）
            if tab_index is not None:
                service.close_tab(tab_index)

    def _get_bing_js(self, max_results: int) -> str:
        """Generate JavaScript for extracting Bing search results."""
        return f"""
        (() => {{
            const results = [];
            const items = document.querySelectorAll('.b_algo');

            for (let i = 0; i < Math.min(items.length, {max_results}); i++) {{
                const item = items[i];
                const titleElem = item.querySelector('h2 a');
                const title = titleElem ? titleElem.textContent.trim() : '';
                const url = titleElem ? titleElem.href : '';
                const snippetElem = item.querySelector('.b_caption p');
                const snippet = snippetElem ? snippetElem.textContent.trim() : '';

                if (title && url) {{
                    results.push({{
                        title: title,
                        url: url,
                        snippet: snippet
                    }});
                }}
            }}

            return {{
                url: window.location.href,
                results: results,
                totalFound: items.length
            }};
        }})()
        """

    def _get_google_js(self, max_results: int) -> str:
        """Generate JavaScript for extracting Google search results."""
        return f"""
        (() => {{
            const results = [];

            // Method 1: standard .g class selector
            let items = document.querySelectorAll('#rso > div.g, #rso .g, div[data-hveid] > div.g');

            // Method 2: fallback - find all h3 in #rso and trace up to their result container
            if (items.length === 0) {{
                const rso = document.querySelector('#rso');
                if (rso) {{
                    // Each direct child div of #rso that contains an h3 is likely a result
                    items = rso.querySelectorAll(':scope > div');
                    items = Array.from(items).filter(el => el.querySelector('h3'));
                }}
            }}

            for (let i = 0; i < Math.min(items.length, {max_results}); i++) {{
                const item = items[i];

                // Title: always in h3
                const titleElem = item.querySelector('h3');
                if (!titleElem) continue;
                const title = titleElem.textContent.trim();
                if (!title) continue;

                // URL: anchor containing h3, or first non-google link
                let url = '';
                const anchorContainingH3 = titleElem.closest('a');
                if (anchorContainingH3) {{
                    url = anchorContainingH3.href;
                }} else {{
                    const allLinks = item.querySelectorAll('a[href]');
                    for (const link of allLinks) {{
                        if (link.href && !link.href.includes('google.com')) {{
                            url = link.href;
                            break;
                        }}
                    }}
                }}

                if (!url || url.includes('google.com/search')) continue;

                // Snippet: try multiple known selectors
                let snippet = '';
                const snippetSelectors = [
                    '[data-sncf]', '.VwiC3b', '.IsZvec', '.lEBKkf',
                    'span.aCOpRe', 'div[style*="-webkit-line-clamp"]'
                ];
                for (const sel of snippetSelectors) {{
                    const el = item.querySelector(sel);
                    if (el) {{
                        snippet = el.textContent.trim();
                        break;
                    }}
                }}

                results.push({{ title, url, snippet }});
            }}

            // Debug info when no results found
            const debugInfo = results.length === 0 ? {{
                rsoExists: !!document.querySelector('#rso'),
                gCount: document.querySelectorAll('.g').length,
                h3Count: document.querySelectorAll('#rso h3').length,
                bodyText: document.body.innerText.substring(0, 600)
            }} : null;

            return {{
                url: window.location.href,
                results: results,
                totalFound: results.length,
                _debug: debugInfo
            }};
        }})()
        """
