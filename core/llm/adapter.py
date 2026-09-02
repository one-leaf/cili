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

from core.config import ModelConfig
from core.llm.types import (
    ContentBlock,
    Message,
    StreamChunk,
    UsageData,
)


class Adapter(ABC):
    """Abstract adapter for LLM providers.

    Each provider (Anthropic, OpenAI, etc.) implements this interface
    to handle its specific wire format and streaming protocol.
    """

    def __init__(self, config: ModelConfig):
        """Initialize adapter with model configuration.

        Args:
            config: Model configuration (name, api_key, base_url, etc.)
        """
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self._is_litellm_proxy: bool = False

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
