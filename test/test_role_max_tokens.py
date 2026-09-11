"""Tests for 角色级 max_tokens 上限。

验证：
- 各角色 JSON 的 max_tokens 默认值
- Agent 构造时应用 min(角色配置, 模型上限)
- 未设置 max_tokens 的角色回退继承模型值
"""

from unittest.mock import MagicMock, patch

import pytest

from core.agent import Agent
from core.agent_config import load_agent_role
from core.config import Config, ModelConfig, SystemConfig


ROLE_DEFAULTS = {
    "master": 16384,
    "worker": 16384,
    "lite": 8192,
}


class TestRoleConfigDefaults:
    """各角色 JSON 的 max_tokens 默认值。"""

    @pytest.mark.parametrize("role", ["master", "worker", "lite"])
    def test_role_default(self, role):
        role_cfg = load_agent_role(role)
        assert role_cfg.max_tokens == ROLE_DEFAULTS[role]


def _make_real_config(model_max_tokens: int) -> Config:
    return Config(
        model=ModelConfig(
            name="test-model",
            interface_type="anthropic",
            max_tokens=model_max_tokens,
            max_context_tokens=256000,
        ),
        system=SystemConfig(),
    )


class TestAgentAppliesCap:
    """Agent 构造时把角色级 max_tokens 应用到 client。"""

    def _make_agent(self, role: str, model_max_tokens: int):
        config = _make_real_config(model_max_tokens)
        with patch("core.agent.create_llm_client") as mock_client:
            mock_client.return_value = MagicMock()
            agent = Agent(config, role=role, task="t" if role != "master" else "")
            return agent, mock_client.return_value

    @pytest.mark.parametrize("role", ["master", "worker"])
    def test_caps_to_role_value_when_model_larger(self, role):
        """模型上限大于角色值 → 取角色值。"""
        agent, client = self._make_agent(role, model_max_tokens=36000)
        assert client.max_tokens == ROLE_DEFAULTS[role]

    def test_lite_caps_to_8192(self):
        """lite 角色上限 8192，即使模型支持更大。"""
        agent, client = self._make_agent("lite", model_max_tokens=36000)
        assert client.max_tokens == 8192

    def test_caps_to_model_when_model_smaller(self):
        """模型上限小于角色值 → 取模型值（不超模型能力）。"""
        agent, client = self._make_agent("master", model_max_tokens=4096)
        assert client.max_tokens == 4096

    def test_role_max_tokens_stored(self):
        """角色 max_tokens 保存在 role_cfg 中。"""
        agent, _ = self._make_agent("master", model_max_tokens=36000)
        assert agent.role_cfg.max_tokens == 16384
