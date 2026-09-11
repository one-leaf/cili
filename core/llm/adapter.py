"""Adapter abstract base class for LLM providers.

Adapters handle provider-specific:
- Request serialization (Message → wire format)
- Response deserialization (wire format → ContentBlock)
- Streaming translation (SSE events → StreamChunk)

This separation allows the LLMClient to be provider-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable
from urllib.parse import urlparse

from core.config import ModelConfig
from core.llm.types import (
    ContentBlock,
    Message,
    StreamChunk,
    TextBlock,
    ToolCallBlock,
    UsageData,
)

# 官方域名无需做 LiteLLM 代理探测（也不该为它们发多余请求）
OFFICIAL_API_HOSTS = frozenset({"api.anthropic.com", "api.openai.com"})


def merge_consecutive_same_role(messages: list[Message]) -> list[Message]:
    """Merge consecutive messages with the same role into one.

    Required by OpenAI API and Bedrock (which reject consecutive same-role
    messages). Anthropic 1P API auto-merges, but we normalize anyway for
    consistency across all providers.

    Merging rules:
    - Both string content → concatenate with "\\n\\n"
    - Both list content → concatenate block lists
    - Mixed (str + list) → convert string to TextBlock, then merge lists
    """
    if not messages:
        return messages

    def _has_tool_calls(content: str | list[ContentBlock]) -> bool:
        """判断消息内容是否含 tool_call 块。"""
        return isinstance(content, list) and any(
            isinstance(b, ToolCallBlock) for b in content
        )

    merged: list[Message] = [messages[0]]

    for msg in messages[1:]:
        prev = merged[-1]
        if msg.role != prev.role:
            merged.append(msg)
            continue

        # 相邻 assistant 均含 tool_calls 时禁止合并：合并成同一回合会破坏
        # tool_use ↔ tool_result 的配对语义（L15）。其余情况照常合并。
        if (
            msg.role == "assistant"
            and _has_tool_calls(prev.content)
            and _has_tool_calls(msg.content)
        ):
            merged.append(msg)
            continue

        # Same role — merge content
        prev_content = prev.content
        cur_content = msg.content

        if isinstance(prev_content, str) and isinstance(cur_content, str):
            new_msg = Message(role=prev.role, content=prev_content + "\n\n" + cur_content)
        elif isinstance(prev_content, list) and isinstance(cur_content, list):
            new_msg = Message(role=prev.role, content=list(prev_content) + list(cur_content))
        elif isinstance(prev_content, str) and isinstance(cur_content, list):
            blocks = [TextBlock(text=prev_content)] + list(cur_content)
            new_msg = Message(role=prev.role, content=blocks)
        elif isinstance(prev_content, list) and isinstance(cur_content, str):
            blocks = list(prev_content) + [TextBlock(text=cur_content)]
            new_msg = Message(role=prev.role, content=blocks)
        else:
            # Fallback: keep separate (shouldn't happen)
            merged.append(msg)
            continue

        # 保留元数据（L15）：合并前取先出现的值，避免 provider/model/usage/
        # stop_reason 丢失（usage 两段都非空时按数值相加）。
        new_msg.provider = prev.provider or msg.provider
        new_msg.model = prev.model or msg.model
        new_msg.stop_reason = prev.stop_reason or msg.stop_reason
        new_msg.compacted = prev.compacted or msg.compacted
        new_msg.invalidated = prev.invalidated or msg.invalidated
        if prev.usage and msg.usage:
            pu, mu = prev.usage, msg.usage
            new_msg.usage = UsageData(
                input_tokens=pu.input_tokens + mu.input_tokens,
                output_tokens=pu.output_tokens + mu.output_tokens,
                cache_read_tokens=pu.cache_read_tokens + mu.cache_read_tokens,
                cache_write_tokens=pu.cache_write_tokens + mu.cache_write_tokens,
            )
        else:
            new_msg.usage = prev.usage or msg.usage
        merged[-1] = new_msg

    return merged


class Adapter(ABC):
    """Abstract adapter for LLM providers.

    Each provider (Anthropic, OpenAI, etc.) implements this interface
    to handle its specific wire format and streaming protocol.
    """

    def __init__(self, config: ModelConfig):
        """Initialize adapter with model configuration.

        Args:
            config: Model configuration (name, api_key, base_url, etc.)

        Raises:
            ValueError: 若 base_url 为空或不可解析
        """
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self._validate_base_url()
        self._is_litellm_proxy: bool = False

    def _validate_base_url(self) -> None:
        """校验 base_url 非空且可解析（含 scheme 与 host）。"""
        if not self.base_url:
            raise ValueError(
                f"{self.__class__.__name__}: base_url 不能为空，请在配置中设置模型地址"
            )
        parsed = urlparse(self.base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(
                f"{self.__class__.__name__}: 无效的 base_url: {self.base_url!r}"
            )

    def should_detect_litellm(self) -> bool:
        """官方域名（Anthropic/OpenAI 1P）不做 LiteLLM 探测，直接返回 False。"""
        return urlparse(self.base_url).hostname not in OFFICIAL_API_HOSTS

    @property
    @abstractmethod
    def api_path(self) -> str:
        """API endpoint path (e.g., '/v1/messages' for Anthropic)."""
        ...

    @property
    def api_url(self) -> str:
        """Full API endpoint URL."""
        return f"{self.base_url}{self.api_path}"

    @abstractmethod
    def build_headers(self) -> dict[str, str]:
        """Build HTTP headers for this provider.

        Returns:
            Dict of HTTP headers (Authorization, content-type, etc.)
        """
        ...

    @abstractmethod
    def serialize(
        self,
        messages: list[Message],
        system: str,
        tools: list[dict[str, Any]] | None,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        stream: bool = False,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Serialize messages and parameters to wire format.

        Args:
            messages: List of messages to send
            system: System prompt
            tools: Tool schemas (provider-specific format)
            model: Model name
            max_tokens: Maximum output tokens
            temperature: Sampling temperature
            stream: Whether to enable streaming
            session_id: Optional session ID for proxy routing

        Returns:
            Request body as dict (ready for JSON serialization)
        """
        ...

    @abstractmethod
    def parse_response(self, data: dict[str, Any]) -> tuple[list[ContentBlock], str, UsageData]:
        """Parse non-streaming response into content blocks.

        Args:
            data: Response body from API

        Returns:
            (content_blocks, stop_reason, usage)
        """
        ...

    @abstractmethod
    def translate_stream(self, events: Iterable[dict[str, Any]]) -> Iterable[StreamChunk]:
        """Translate provider SSE events into neutral StreamChunks.

        Args:
            events: Iterable of parsed SSE events from the stream

        Yields:
            StreamChunk objects for the BlockAssembler
        """
        ...

    def detect_litellm(self, transport: Any) -> None:
        """Detect if the endpoint is a LiteLLM proxy.

        Called during initialization to enable proxy-specific features.

        Args:
            transport: HttpTransport instance for making the detection request
        """
        import logging
        logger = logging.getLogger(__name__)

        try:
            # /openapi.json is a standard REST endpoint, must use GET
            url = f"{self.base_url}/openapi.json"
            logger.debug(f"[LiteLLM] Detecting proxy at {url}")

            resp = transport.client.get(url, timeout=10)
            logger.debug(f"[LiteLLM] Response status: {resp.status_code}")

            if resp.status_code == 200:
                data = resp.json()
                info = data.get("info", {})
                title = info.get("title", "")
                logger.debug(f"[LiteLLM] API title: {title}")

                if "litellm" in title.lower():
                    self._is_litellm_proxy = True
                    logger.info(f"[LiteLLM] Detected LiteLLM proxy: {self.base_url}")
                else:
                    logger.debug(f"[LiteLLM] Not a LiteLLM proxy: {self.base_url}")
        except Exception as e:
            logger.warning(f"[LiteLLM] Detection failed for {self.base_url}: {e}")
