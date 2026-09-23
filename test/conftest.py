"""Shared test fixtures."""

import os
import socket
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
from core.agent import Agent
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


@pytest.fixture(scope="session", autouse=True)
def _isolate_workspaces_index():
    """隔离 workspaces.json：测试期间重定向到临时索引，避免读写真实数据。

    get_workspace_data_dir() 等路径函数从临时索引解析，测试 workspace_uuid
    映射到 test_workspace 目录（{test_workspace}/.cili/）。

    不依赖 test_workspace fixture（部分测试模块覆盖为 function-scope，
    会造成 ScopeMismatch），改为内部计算相同的测试目录路径。
    """
    import core.config as config_mod
    original = config_mod.WORKSPACES_JSON
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    test_dir = os.path.join(project_dir, "workspace", ".test_tmp")
    tmp_index = Path(test_dir).parent / ".test_workspaces.json"
    if tmp_index.exists():
        tmp_index.unlink()
    config_mod.WORKSPACES_JSON = tmp_index
    # 注册固定测试工作区（tools/temp 工具 fixture 使用），
    # 未注册 uuid 会回落真实默认工作区（PROJECT_ROOT/workspace/.cili），需避免
    for test_uuid in ("test-workspace", "test-uuid"):
        config_mod.upsert_workspace_entry({
            "uuid": test_uuid,
            "workspace_name": f"Test {test_uuid}",
            "directory": test_dir,
        })
    yield
    config_mod.WORKSPACES_JSON = original
    if tmp_index.exists():
        tmp_index.unlink()


@pytest.fixture(scope="session")
def config():
    """加载测试配置（使用 DGX 本地端点，Anthropic 协议）。"""
    return make_dgx_config("anthropic")


@pytest.fixture(scope="session")
def dgx_available():
    """探测 DGX LLM 服务器可达性，不可达则 skip（解耦 CI，A45 §2.1）。

    纯逻辑测试不依赖真实 LLM；只有显式依赖本 fixture 的集成测试才声明
    "需要真实服务器"。服务器不在线时跳过而非失败，保证无内网环境也能
    稳定跑完套件。
    """
    host = DGX_BASE_URL.split("://", 1)[-1]
    if ":" in host:
        host, _, port = host.rpartition(":")
        port = int(port)
    else:
        port = 80
    try:
        with socket.create_connection((host, port), timeout=1.5):
            pass
    except OSError:
        pytest.skip(f"DGX LLM 服务器不可达: {DGX_BASE_URL}")
    return True


@pytest.fixture
def tools(test_workspace):
    """创建工具实例"""
    # 使用测试专用 UUID，避免创建 "default" 目录
    test_uuid = "test-workspace"
    yield create_tools(cwd=test_workspace, workspace_uuid=test_uuid)
    # 清理测试产生的 .cili 数据目录
    test_data_dir = os.path.join(test_workspace, ".cili")
    if os.path.exists(test_data_dir):
        shutil.rmtree(test_data_dir, ignore_errors=True)


@pytest.fixture
def agent(config, test_workspace):
    """创建 Master Agent 实例"""
    import core.config as config_mod
    # 使用唯一的 workspace_uuid 避免测试冲突，并注册到临时索引
    test_uuid = secrets.token_hex(4)
    config_mod.upsert_workspace_entry({
        "uuid": test_uuid,
        "workspace_name": "Test Agent Workspace",
        "directory": test_workspace,
    })
    agent_instance = Agent(config, role="master", cwd=test_workspace, workspace_uuid=test_uuid)
    yield agent_instance
    # 清理测试生成的 .cili 数据目录
    test_data_dir = os.path.join(test_workspace, ".cili")
    if os.path.exists(test_data_dir):
        shutil.rmtree(test_data_dir, ignore_errors=True)


@pytest.fixture
def temp_cron_state(monkeypatch):
    """临时替换 cron 状态目录，避免测试读取真实状态文件"""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        # Monkeypatch the functions that return cron directories
        monkeypatch.setattr(core.cron, "get_cron_state_dir", lambda workspace_uuid="": temp_path / "state")
        monkeypatch.setattr(core.cron, "get_user_tasks_file", lambda workspace_uuid="": temp_path / "user_tasks.json")
        # Also patch in cron_tool module which imports these
        import core.tools.cron_tool as cron_tool
        monkeypatch.setattr(cron_tool, "get_user_tasks_file", lambda workspace_uuid="": temp_path / "user_tasks.json")
        yield temp_dir
