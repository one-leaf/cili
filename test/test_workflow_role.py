"""workflow 监督者角色：模型继承、工具白名单；步骤执行器复用 worker/lite。"""

from core.config import (
    AgentConfig,
    Config,
    ModelSpec,
    ProviderConfig,
    SystemConfig,
)
from core.role_config import load_role


def make_config(agents):
    return Config(
        providers={
            "dgx": ProviderConfig(
                protocol="anthropic",
                api_key="no_need",
                base_url="http://127.0.0.1:1",
                models={
                    "m": ModelSpec(name="m", max_output_tokens=4096, max_context_tokens=100000),
                    "w": ModelSpec(name="w", max_output_tokens=4096, max_context_tokens=100000),
                    "t": ModelSpec(name="t", max_output_tokens=4096, max_context_tokens=100000),
                },
            )
        },
        agents=agents,
        system=SystemConfig(),
    )


def test_workflow_model_follows_master_by_default():
    config = make_config({
        "master": AgentConfig(model="dgx/m"),
        "worker": AgentConfig(model="dgx/w"),
    })
    assert config.role_refs()["workflow"] == "dgx/m"
    assert config.role_models["workflow"].name == "m"


def test_explicit_workflow_model_wins():
    config = make_config({
        "master": AgentConfig(model="dgx/m"),
        "workflow": AgentConfig(model="dgx/t"),
    })
    assert config.role_refs()["workflow"] == "dgx/t"


def test_workflow_role_is_business_agnostic():
    config = make_config({"master": AgentConfig(model="dgx/m")})
    role = load_role("workflow", config)
    assert role.mode == "interactive"
    assert role.approval is False
    for forbidden in ("write", "edit", "bash", "pwsh", "python", "agent",
                      "ask_user", "workflow", "workflow_agent"):
        assert forbidden not in role.tools
    for allowed in ("read", "grep", "glob", "todo", "clock"):
        assert allowed in role.tools
    assert role.user_layers == []


def test_step_executors_are_worker_and_lite():
    config = make_config({"master": AgentConfig(model="dgx/m")})
    assert load_role("worker", config).mode == "autonomous"
    assert load_role("lite", config).mode == "autonomous"
    assert config.role_refs()["worker"] == "dgx/m"
    assert config.role_refs()["lite"] == "dgx/m"
