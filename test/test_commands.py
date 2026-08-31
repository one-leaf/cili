"""命令行命令测试"""

from core.llm import LLMClient, AnthropicAdapter, OpenAIAdapter, create_llm_client
from core.config import ModelConfig


class TestCommands:
    """命令行命令测试"""

    def test_help_command_exists(self, agent):
        """测试 /help 命令处理存在"""
        # /help 在 web_api.py 中处理，验证 agent 可以正常创建
        assert agent is not None
        assert hasattr(agent, 'run')

    def test_session_manager_available(self, agent):
        """测试会话管理器可用"""
        session_mgr = agent.session_manager
        # 验证所有会话管理方法存在
        assert hasattr(session_mgr, 'add_message')
        assert hasattr(session_mgr, 'get_messages')
        assert hasattr(session_mgr, 'get_valid_messages')
        assert hasattr(session_mgr, 'save')
        assert hasattr(session_mgr, 'load')
        assert hasattr(session_mgr, 'clear')
        assert hasattr(session_mgr, 'delete')
        assert hasattr(session_mgr, 'rename')
        assert hasattr(session_mgr, 'update_usage')
        assert hasattr(session_mgr, 'get_usage')
        assert hasattr(session_mgr, 'get_message_count')

    def test_llm_client_is_pure_api(self, agent):
        """测试 LLMClient 是纯 API 客户端"""
        client = agent.client
        # 应该是 LLMClient 实例
        assert isinstance(client, LLMClient)
        # API 方法应该存在
        assert hasattr(client, 'chat')
        assert hasattr(client, 'chat_stream')
        assert hasattr(client, 'test_connection')
        assert hasattr(client, 'close')
        # 会话管理方法不应该存在（已移到 SessionManager）
        assert not hasattr(client, 'list_sessions')
        assert not hasattr(client, 'new_session')
        assert not hasattr(client, 'load_session')
        assert not hasattr(client, 'delete_session')

    def test_factory_anthropic(self):
        """测试工厂函数创建 AnthropicAdapter"""
        config = ModelConfig(
            name="test-model",
            interface_type="anthropic",
            api_key="sk-test",
            base_url="https://api.test.com",
        )
        client = create_llm_client(config)
        assert isinstance(client.adapter, AnthropicAdapter)
        assert client.config.interface_type == "anthropic"
        assert client.adapter.api_url == "https://api.test.com/v1/messages"
        client.close()

    def test_factory_openai(self):
        """测试工厂函数创建 OpenAIAdapter"""
        config = ModelConfig(
            name="gpt-4",
            interface_type="openai",
            api_key="sk-test",
            base_url="https://api.openai.com",
        )
        client = create_llm_client(config)
        assert isinstance(client.adapter, OpenAIAdapter)
        assert client.config.interface_type == "openai"
        assert client.adapter.api_url == "https://api.openai.com/v1/chat/completions"
        client.close()

    def test_factory_unknown_raises_error(self):
        """测试未知 interface_type 抛出错误"""
        config = ModelConfig(
            name="unknown-model",
            interface_type="unknown",
            api_key="sk-test",
            base_url="https://api.test.com",
        )
        try:
            client = create_llm_client(config)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Unsupported interface_type" in str(e)
