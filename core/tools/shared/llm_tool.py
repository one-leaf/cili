"""LLM tool - single-turn LLM call with file I/O and structured output support.

Available to both main agent and sub-agent as a shared tool.
Uses prompt as system prompt, input_file content as user message, output_file for result.
"""

from __future__ import annotations

import json
import os

from core.tools.shared.base import Tool, ToolResult


class LLMTool(Tool):
    name = "llm"
    description = (
        "**Single-turn LLM call for text processing: translation, summarization, extraction, analysis, etc.**\n\n"
        "## Parameters:\n"
        "- **prompt**: System prompt - instructions for how to process the content\n"
        "- **input_file**: Path to input file - content becomes the user message\n"
        "- **output_file**: Path to write result (optional, returns text if omitted)\n"
        "- **output_schema**: JSON Schema for structured output (optional)\n\n"
        "## Examples:\n"
        '```json\n'
        '// Translate file content\n'
        '{"prompt": "Translate to Chinese", "input_file": "article.txt", "output_file": "article_zh.txt"}\n\n'
        '// Structured extraction from file\n'
        '{"input_file": "logs.txt", "output_schema": {"type": "object", "properties": {"errors": {"type": "array"}}}}\n'
        '```'
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "System prompt - instructions for how to process the input content.",
            },
            "input_file": {
                "type": "string",
                "description": "Path to input file. Content will be used as user message.",
            },
            "output_file": {
                "type": "string",
                "description": "Path to write the result to. If not specified, result is returned as tool output text.",
            },
            "output_schema": {
                "type": "object",
                "description": (
                    "JSON Schema for structured output. "
                    "When provided, LLM returns structured JSON data matching this schema. "
                    "When omitted, returns plain text."
                ),
            },
        },
        "required": ["input_file"],
    }

    _DEFAULT_SYSTEM_PROMPT = (
        "You are a text processing assistant. "
        "Process the given content according to the instructions. "
        "Return only the processed content, without commentary or explanations. "
        "Preserve the original formatting unless instructed otherwise."
    )

    _BATCH_SYSTEM_PROMPT = (
        "You are running in a non-interactive batch environment. "
        "You cannot ask questions or wait for user confirmation. "
        "Execute the task autonomously to completion. "
        "Make reasonable decisions and proceed without waiting for approval."
    )

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session_manager=None, config=None):
        super().__init__(cwd, workspace_uuid, session_manager)
        self._config = config

    def execute(
        self,
        input_file: str,
        prompt: str | None = None,
        output_file: str | None = None,
        output_schema: dict | None = None,
    ) -> ToolResult:
        """Execute a single-turn LLM call."""
        # Use injected config, fall back to loading from disk
        config = self._config
        if config is None:
            from core.config import load_config
            config = load_config()
        if not config.llm_model:
            return ToolResult(
                "Error: llm is not available. LLM model is not configured.\n\n"
                "Please configure the LLM model in settings:\n"
                "1. Open the settings modal (gear icon)\n"
                "2. Switch to the 'LLM 模型' tab\n"
                "3. Configure a model for single-turn calls (translation, extraction, etc.)\n"
                "4. Save and try again",
                error=True
            )

        # Read input file
        path = self._resolve_path(input_file)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            return ToolResult(f"Error: input file not found: {input_file}", error=True)
        except Exception as e:
            return ToolResult(f"Error reading input file: {e}", error=True)

        # Build system prompt
        if prompt:
            system = f"{prompt}\n\n{self._BATCH_SYSTEM_PROMPT}"
        else:
            system = f"{self._DEFAULT_SYSTEM_PROMPT}\n\n{self._BATCH_SYSTEM_PROMPT}"

        try:
            from core.llm import create_llm_client, ToolCallBlock, TextBlock, Message
            client = create_llm_client(config.llm_model)

            # Always use chat() directly to access response.usage
            tool = None
            if output_schema:
                tool = [{
                    "name": "output_result",
                    "description": "You must call this tool to return structured results. Do not output any text.",
                    "input_schema": output_schema,
                }]

            response = client.chat(
                messages=[Message(role="user", content=content)],
                system=system,
                tools=tool,
            )

            if output_schema:
                # Extract structured result from tool_use
                for block in response.content:
                    if isinstance(block, ToolCallBlock) and block.name == "output_result":
                        result_text = block.arguments  # raw JSON string
                        break
                else:
                    text = response.get_text()
                    return ToolResult(f"Error: LLM did not return structured result: {text[:200]}", error=True)
            else:
                result_text = response.get_text()

            # Forward usage to session metadata (UsageData object)
            usage_data = response.usage
            if self.session_manager and usage_data:
                self.session_manager.update_usage(
                    input_tokens=usage_data.input_tokens,
                    output_tokens=usage_data.output_tokens,
                    api_calls=1,
                    cache_read_tokens=usage_data.cache_read_tokens,
                    cache_creation_tokens=usage_data.cache_write_tokens,
                )

        except Exception as e:
            return ToolResult(f"Error: LLM call failed: {e}", error=True)

        # Write to file or return text
        if output_file:
            path = self._resolve_path(output_file)
            try:
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(result_text)
                return ToolResult(f"Result written to {output_file}")
            except Exception as e:
                return ToolResult(f"Error writing output file: {e}", error=True)

        return ToolResult(result_text)
