"""Shared test fixtures."""

import os
import sys
import shutil
import secrets
import uuid
import tempfile
from pathlib import Path
import pytest

# 添加项目根目录到路径
project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_dir)

from core.config import Config, ModelConfig, SystemConfig
from core.root_agent import RootAgent
from core.tools import create_tools, get_tool_by_name
import core.cron


# ── DGX 本地 LLM 端点（同时支持 Anthropic 和 OpenAI 协议）──
DGX_BASE_URL = "http://192.168.3.3:8080"
DGX_API_KEY = "no_need_for_local"
DGX_MODEL = "dgx"


def make_dgx_config(interface_type: str = "anthropic") -> Config:
    """构建 DGX 端点配置，支持 anthropic / openai 两种协议。"""
    return Config(
        model=ModelConfig(
            name=DGX_MODEL,
            interface_type=interface_type,
            api_key=DGX_API_KEY,
            base_url=DGX_BASE_URL,
            max_tokens=4096,
            max_context_tokens=256000,
            multimodal=True,
            temperature=0.2,
        ),
        system=SystemConfig(),
    )


@pytest.fixture(scope="session")
def test_workspace():
    """创建临时测试工作目录"""
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    test_dir = os.path.join(project_dir, "workspace", ".test_tmp")
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir, ignore_errors=True)
    os.makedirs(test_dir, exist_ok=True)
    yield test_dir
    # 清理
    shutil.rmtree(test_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def config():
    """加载测试配置（使用 DGX 本地端点，Anthropic 协议）。"""
    return make_dgx_config("anthropic")


@pytest.fixture
def tools(test_workspace):
    """创建工具实例"""
    # 使用测试专用 UUID，避免创建 "default" 目录
    test_uuid = "test-workspace"
    yield create_tools(cwd=test_workspace, workspace_uuid=test_uuid)
    # 清理测试产生的 memory 目录
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    test_memory_dir = os.path.join(project_dir, "data", "agents", test_uuid)
    if os.path.exists(test_memory_dir):
        shutil.rmtree(test_memory_dir, ignore_errors=True)


@pytest.fixture
def agent(config, test_workspace):
    """创建 RootAgent 实例"""
    # 使用唯一的 workspace_uuid 避免测试冲突
    test_uuid = secrets.token_hex(4)
    agent_instance = RootAgent(config, cwd=test_workspace, workspace_uuid=test_uuid)
    yield agent_instance
    # 清理测试生成的 workspace 目录
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    test_workspace_dir = os.path.join(project_dir, "data", "agents", test_uuid)
    if os.path.exists(test_workspace_dir):
        shutil.rmtree(test_workspace_dir, ignore_errors=True)


@pytest.fixture
def temp_cron_state():
    """临时替换 cron 状态目录，避免测试读取真实状态文件"""
    original_state_dir = core.cron.CRON_STATE_DIR
    with tempfile.TemporaryDirectory() as temp_dir:
        core.cron.CRON_STATE_DIR = Path(temp_dir) / "state"
        yield temp_dir
        core.cron.CRON_STATE_DIR = original_state_dir
