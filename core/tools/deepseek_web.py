"""DeepSeek 服务端联网工具（客户端包装）。

DeepSeek 的 ``deepseek_web_search`` / ``deepseek_read_url`` 是**服务端工具**：
搜索与抓取在 DeepSeek 侧完成，结果以 ``server_tool_use`` / ``*_tool_result``
内容块随响应返回。这里把它们包装成普通客户端工具——工具内部另发一条辅助
Messages 请求（只声明服务端工具，不声明任何客户端工具），解析结构化结果，
再作为普通 ``tool_result`` 返回主模型。

这样主对话历史里不会出现服务端块，不需要改持久化 / 回放 / 压缩 / 跨模型清洗；
代价是每次工具调用多一次模型往返（与 Claude Code 的 WebSearch 同模式）。

工具可见性由 ``core.llm.capabilities.tools_visible`` 按当前模型判定，runner 在
``_eligible_tools`` 中过滤；模型注入与能力缺失回调由 runner 在工具重建时接线。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Callable
from urllib.parse import urlparse

from core.llm import Message, TextBlock, create_llm_client
from core.llm.capabilities import mark_unsupported, tools_visible
from core.llm.types import ServerBlock
from core.tools.base import UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END, Tool, ToolResult

logger = logging.getLogger(__name__)

# 角色白名单 / runner 门控使用的工具名集合
SERVER_TOOL_NAMES = frozenset({"deepseek_web_search", "deepseek_read_url"})


def _exception_text(exc: BaseException) -> str:
    """异常文本：附上 HTTP 响应体（能力缺失错误在 body 里，不在 str(e) 里）。"""
    parts = [str(exc)]
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            parts.append(response.text)
        except Exception:  # noqa: BLE001 —— 读 body 失败不影响错误上报
            pass
    return "\n".join(p for p in parts if p)


def _wrap_untrusted(text: str) -> str:
    return f"{UNTRUSTED_DATA_BEGIN}\n{text}\n{UNTRUSTED_DATA_END}"


class _ServerToolBase(Tool):
    """服务端工具包装基类：辅助请求 + 结果解析。"""

    is_deepseek_server_tool = True
    server_type = ""
    system_prompt = ""
    max_output_tokens = 1024
    max_results = 30
    max_snippet_chars = 500

    # 上游明确表示「该模型/端点不支持搜索」的两类错误
    _CAPABILITY_MARKERS = ("unsupported server tool", "access denied for web search")

    def __init__(
        self,
        *,
        model_provider: Callable[[], Any] | None = None,
        capability_missing_handler: Callable[[], None] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._model_provider = model_provider
        self._capability_missing_handler = capability_missing_handler

    # ── runner 接线 ──────────────────────────────────────────────

    def set_model_provider(self, provider: Callable[[], Any]) -> None:
        """注入「当前实时模型」读取器（runner 切模型后立即生效）。"""
        self._model_provider = provider

    def set_capability_missing_handler(self, handler: Callable[[], None]) -> None:
        """注入能力缺失回调：runner 收到后重算工具可见性，后续轮次不再下发。"""
        self._capability_missing_handler = handler

    # ── 执行 ────────────────────────────────────────────────────

    def execute(self, **kwargs) -> ToolResult:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def _run(self, user_text: str, **format_kwargs) -> ToolResult:
        model = self._model_provider() if callable(self._model_provider) else None
        if model is None:
            return ToolResult("Error: no model is available for the current session", error=True)
        if not tools_visible(model):
            return ToolResult(
                "Error: DeepSeek web tools are not available for the current model/protocol.",
                error=True,
            )

        # 辅助调用只消费摘要文本，思考没有消费者：固定关闭 thinking（普通搜索，
        # 避免每次调用都触发最多约 20 步的深度搜索）。
        try:
            client = create_llm_client(replace(model, reasoning="off"))
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"Error: failed to create DeepSeek client: {e}", error=True)

        try:
            response = client.chat(
                messages=[Message(role="user", content=user_text)],
                system=self.system_prompt,
                tools=[{"type": self.server_type, "name": self.server_type}],
                max_tokens=self.max_output_tokens,
                session_id=getattr(self.session, "session_id", "") or "",
            )
        except Exception as e:  # noqa: BLE001 —— 统一转成工具错误结果，不打断回合
            return self._error_result(e, model)
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

        return self._format_response(response, **format_kwargs)

    def _error_result(self, exc: BaseException, model) -> ToolResult:
        text = _exception_text(exc)
        lowered = text.lower()
        if any(marker in lowered for marker in self._CAPABILITY_MARKERS):
            mark_unsupported(model.name)
            logger.warning(
                f"[{self.name}] 模型 {model.name!r} 不支持 DeepSeek 服务端联网工具，已禁用: {text[:300]}"
            )
            handler = self._capability_missing_handler
            if callable(handler):
                try:
                    handler()
                except Exception:  # noqa: BLE001
                    logger.exception(f"[{self.name}] 刷新工具可见性失败")
            return ToolResult(
                f"Error: model {model.name!r} does not support DeepSeek server web tools. "
                "Switch to a supported model, or use the web_search/browser tools instead.",
                error=True,
            )
        logger.warning(f"[{self.name}] DeepSeek 联网请求失败: {text[:300]}")
        return ToolResult(f"Error: DeepSeek web request failed: {text[:800]}", error=True)

    @staticmethod
    def _collect(response) -> tuple[list[dict], list[dict], list[str], str]:
        """从辅助响应中提取搜索结果、抓取结果、文本摘要与错误码。"""
        hits: list[dict] = []
        fetched: list[dict] = []
        texts: list[str] = []
        error = ""

        for block in response.content:
            if isinstance(block, TextBlock):
                if block.text:
                    texts.append(block.text)
                continue
            if not isinstance(block, ServerBlock):
                continue
            raw = block.raw if isinstance(block.raw, dict) else {}
            block_type = raw.get("type")
            content = raw.get("content")
            if block_type == "web_search_tool_result":
                # 成功时 content 是数组；失败时是错误对象（HTTP 仍 200）
                if isinstance(content, list):
                    hits.extend(item for item in content if isinstance(item, dict))
                elif isinstance(content, dict):
                    error = error or str(content.get("error_code") or "")
            elif block_type == "web_fetch_tool_result":
                if isinstance(content, dict):
                    if content.get("type") == "web_fetch_result":
                        fetched.append(content)
                    elif content.get("error_code"):
                        error = error or str(content["error_code"])

        return hits, fetched, texts, error

    @staticmethod
    def _summary(texts: list[str]) -> str:
        return "\n\n".join(t.strip() for t in texts if t.strip()).strip()

    def _format_response(self, response, **kwargs) -> ToolResult:  # pragma: no cover
        raise NotImplementedError


class DeepSeekWebSearchTool(_ServerToolBase):
    """DeepSeek 服务端联网搜索（客户端包装）。"""

    name = "deepseek_web_search"
    server_type = "deepseek_web_search"
    system_prompt = (
        "You are an assistant performing a web search tool use. "
        "Use the available web search tool for the query instead of answering "
        "from prior knowledge, then answer concisely, grounded in the search results."
    )
    description = (
        "Search the live web with DeepSeek's server-side search. "
        "Returns result titles, URLs and snippets plus a short summary. "
        "Cite sources as markdown links when using the results."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query string.",
            },
        },
        "required": ["query"],
    }
    concurrency_safe = True

    def execute(self, query: str = "", **kwargs) -> ToolResult:
        query = (query or "").strip()
        if not query:
            return ToolResult("Error: query is required", error=True)
        return self._run(f"Perform a web search for the query: {query}", query=query)

    def _format_response(self, response, query: str = "", **kwargs) -> ToolResult:
        hits, _, texts, error = self._collect(response)
        if error:
            return ToolResult(f"Error: DeepSeek web search failed: {error}", error=True)

        lines: list[str] = []
        seen: set[str] = set()
        for item in hits:
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            title = str(item.get("title") or "").strip() or "(untitled)"
            snippet = str(item.get("snippet") or "").strip()
            if len(snippet) > self.max_snippet_chars:
                snippet = snippet[: self.max_snippet_chars] + "..."
            lines.append(f"{len(seen)}. {title}\n   {url}")
            if snippet:
                lines.append(f"   {snippet}")
            if len(seen) >= self.max_results:
                break

        summary = self._summary(texts)
        if not lines and not summary:
            return ToolResult("Error: DeepSeek web search returned no content", error=True)

        parts = [
            f"Search results for: {query}\n\n" + "\n".join(lines)
            if lines
            else "No search result links were returned by DeepSeek."
        ]
        if summary:
            parts.append("Summary:\n" + summary)
        return ToolResult(_wrap_untrusted("\n\n".join(parts)))


class DeepSeekReadUrlTool(_ServerToolBase):
    """DeepSeek 服务端网页读取（客户端包装）。"""

    name = "deepseek_read_url"
    server_type = "deepseek_read_url"
    system_prompt = (
        "You are an assistant that reads web pages. When asked to read a URL, "
        "you must use the deepseek_read_url tool instead of answering from prior "
        "knowledge, then summarize the page content concisely."
    )
    description = (
        "Read a specific URL with DeepSeek's server-side fetcher and return its "
        "title plus a short summary of the page content."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http(s) URL to read.",
            },
        },
        "required": ["url"],
    }
    concurrency_safe = True

    def execute(self, url: str = "", **kwargs) -> ToolResult:
        url = (url or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ToolResult("Error: url must be an absolute http(s) URL", error=True)
        return self._run(
            f"Use the deepseek_read_url tool to read this URL and summarize the page: {url}",
            url=url,
        )

    def _format_response(self, response, url: str = "", **kwargs) -> ToolResult:
        _, fetched, texts, error = self._collect(response)
        if error:
            return ToolResult(f"Error: DeepSeek read URL failed: {error}", error=True)

        page = fetched[0] if fetched else {}
        page_url = str(page.get("url") or url)
        title = ""
        content = page.get("content")
        if isinstance(content, dict):
            title = str(content.get("title") or "").strip()

        summary = self._summary(texts)
        if not summary and not fetched:
            return ToolResult("Error: DeepSeek could not read the URL", error=True)

        parts = [f"URL: {page_url}"]
        if title:
            parts.append(f"Title: {title}")
        if summary:
            parts.append("Summary:\n" + summary)
        else:
            parts.append("(No page text was returned; only metadata is available.)")
        return ToolResult(_wrap_untrusted("\n".join(parts)))
