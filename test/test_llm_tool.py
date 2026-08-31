"""Tests for core/tools/shared/llm_tool.py."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from core.tools.shared.llm_tool import LLMTool
from core.tools.shared.base import ToolResult
from core.llm import LLMResponse, TextBlock, ToolCallBlock, UsageData


@pytest.fixture
def llm_tool(test_workspace):
    return LLMTool(cwd=test_workspace, workspace_uuid="test-workspace")


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.llm_model = MagicMock()
    config.llm_model.name = "test-model"
    return config


class TestLLMToolExecute:
    """LLMTool.execute() with mocked LLM client."""

    def test_no_llm_model_configured(self, llm_tool):
        """Returns error when llm_model is not configured."""
        config = MagicMock()
        config.llm_model = None
        llm_tool._config = config

        result = llm_tool.execute(input_file="test.txt")
        assert result.error is True
        assert "not configured" in result.output

    def test_input_file_not_found(self, llm_tool, mock_config, test_workspace):
        """Returns error when input file doesn't exist."""
        llm_tool._config = mock_config
        result = llm_tool.execute(input_file="nonexistent.txt")
        assert result.error is True
        assert "not found" in result.output

    def test_reads_input_file(self, llm_tool, mock_config, test_workspace):
        """Reads content from input file."""
        llm_tool._config = mock_config
        # Create test input file
        input_path = os.path.join(test_workspace, "input.txt")
        with open(input_path, "w") as f:
            f.write("test content")

        # Mock LLM client with real LLMResponse
        mock_client = MagicMock()
        mock_response = LLMResponse(
            content=[TextBlock(text="processed result")],
            usage=UsageData(input_tokens=10, output_tokens=5),
        )
        mock_client.chat.return_value = mock_response

        with patch("core.llm.create_llm_client", return_value=mock_client):
            result = llm_tool.execute(input_file="input.txt")

        assert result.error is False
        assert "processed result" in result.output

    def test_writes_output_file(self, llm_tool, mock_config, test_workspace):
        """Writes result to output file when specified."""
        llm_tool._config = mock_config
        # Create input file
        input_path = os.path.join(test_workspace, "input.txt")
        with open(input_path, "w") as f:
            f.write("test")

        # Mock LLM client with real LLMResponse
        mock_client = MagicMock()
        mock_response = LLMResponse(
            content=[TextBlock(text="output text")],
            usage=UsageData(),
        )
        mock_client.chat.return_value = mock_response

        output_path = os.path.join(test_workspace, "output.txt")
        with patch("core.llm.create_llm_client", return_value=mock_client):
            result = llm_tool.execute(input_file="input.txt", output_file="output.txt")

        assert result.error is False
        assert os.path.exists(output_path)
        with open(output_path) as f:
            assert f.read() == "output text"

    def test_output_schema_structured_result(self, llm_tool, mock_config, test_workspace):
        """Extracts structured result from tool_use block."""
        llm_tool._config = mock_config
        input_path = os.path.join(test_workspace, "input.txt")
        with open(input_path, "w") as f:
            f.write("test")

        # Mock LLM client returning ToolCallBlock (real type)
        mock_client = MagicMock()
        mock_response = LLMResponse(
            content=[
                TextBlock(text=""),
                ToolCallBlock(
                    id="t1",
                    name="output_result",
                    arguments='{"key": "value"}',
                ),
            ],
            usage=UsageData(),
        )
        mock_client.chat.return_value = mock_response

        schema = {"type": "object", "properties": {"key": {"type": "string"}}}
        with patch("core.llm.create_llm_client", return_value=mock_client):
            result = llm_tool.execute(input_file="input.txt", output_schema=schema)

        assert result.error is False
        assert '"key"' in result.output
        assert '"value"' in result.output

    def test_output_schema_no_tool_use_error(self, llm_tool, mock_config, test_workspace):
        """Returns error when LLM doesn't call output_result tool."""
        llm_tool._config = mock_config
        input_path = os.path.join(test_workspace, "input.txt")
        with open(input_path, "w") as f:
            f.write("test")

        mock_client = MagicMock()
        mock_response = LLMResponse(
            content=[TextBlock(text="I can't do that")],
            usage=UsageData(),
        )
        mock_client.chat.return_value = mock_response

        schema = {"type": "object"}
        with patch("core.llm.create_llm_client", return_value=mock_client):
            result = llm_tool.execute(input_file="input.txt", output_schema=schema)

        assert result.error is True
        assert "did not return structured result" in result.output

    def test_tracks_usage(self, llm_tool, mock_config, test_workspace):
        """Forwards usage to session manager."""
        llm_tool._config = mock_config
        session_manager = MagicMock()
        llm_tool.session_manager = session_manager

        input_path = os.path.join(test_workspace, "input.txt")
        with open(input_path, "w") as f:
            f.write("test")

        mock_client = MagicMock()
        mock_response = LLMResponse(
            content=[TextBlock(text="result")],
            usage=UsageData(
                input_tokens=100,
                output_tokens=50,
                cache_read_tokens=10,
                cache_write_tokens=5,
            ),
        )
        mock_client.chat.return_value = mock_response

        with patch("core.llm.create_llm_client", return_value=mock_client):
            llm_tool.execute(input_file="input.txt")

        session_manager.update_usage.assert_called_once_with(
            input_tokens=100,
            output_tokens=50,
            api_calls=1,
            cache_read_tokens=10,
            cache_creation_tokens=5,
        )

    def test_llm_call_failure(self, llm_tool, mock_config, test_workspace):
        """Returns error when LLM call fails."""
        llm_tool._config = mock_config
        input_path = os.path.join(test_workspace, "input.txt")
        with open(input_path, "w") as f:
            f.write("test")

        mock_client = MagicMock()
        mock_client.chat.side_effect = Exception("API error")

        with patch("core.llm.create_llm_client", return_value=mock_client):
            result = llm_tool.execute(input_file="input.txt")

        assert result.error is True
        assert "LLM call failed" in result.output

    def test_custom_prompt(self, llm_tool, mock_config, test_workspace):
        """Uses custom prompt as system prompt."""
        llm_tool._config = mock_config
        input_path = os.path.join(test_workspace, "input.txt")
        with open(input_path, "w") as f:
            f.write("test")

        mock_client = MagicMock()
        mock_response = LLMResponse(
            content=[TextBlock(text="result")],
            usage=UsageData(),
        )
        mock_client.chat.return_value = mock_response

        with patch("core.llm.create_llm_client", return_value=mock_client):
            llm_tool.execute(input_file="input.txt", prompt="Translate to Chinese")

        # Verify system prompt includes custom prompt
        call_args = mock_client.chat.call_args
        assert "Translate to Chinese" in call_args.kwargs["system"]

    def test_default_prompt_when_none(self, llm_tool, mock_config, test_workspace):
        """Uses default system prompt when none provided."""
        llm_tool._config = mock_config
        input_path = os.path.join(test_workspace, "input.txt")
        with open(input_path, "w") as f:
            f.write("test")

        mock_client = MagicMock()
        mock_response = LLMResponse(
            content=[TextBlock(text="result")],
            usage=UsageData(),
        )
        mock_client.chat.return_value = mock_response

        with patch("core.llm.create_llm_client", return_value=mock_client):
            llm_tool.execute(input_file="input.txt")

        call_args = mock_client.chat.call_args
        assert "text processing assistant" in call_args.kwargs["system"].lower()


class TestLLMToolParameters:
    """LLMTool parameter schema."""

    def test_required_input_file(self):
        assert "input_file" in LLMTool.parameters["required"]

    def test_optional_parameters(self):
        props = LLMTool.parameters["properties"]
        assert "prompt" in props
        assert "output_file" in props
        assert "output_schema" in props
