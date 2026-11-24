"""测试共享辅助：DGX 端点配置与 JSON 文件助手（不依赖 pytest fixture）。

conftest 只保留 fixture 与隔离逻辑；跨文件复制的构造器放这里。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.config import (
    AgentConfig,
    Config,
    ModelSpec,
    ProviderConfig,
    SystemConfig,
)

# ── DGX 本地 LLM 端点（同时支持 Anthropic 和 OpenAI 协议）──
DGX_BASE_URL = "http://192.168.3.3:8080"
DGX_API_KEY = "no_need_for_local"
DGX_MODEL = "dgx"


def make_dgx_config(protocol: str = "anthropic") -> Config:
    """构建 DGX 端点配置，支持 anthropic / openai 两种协议。"""
    return Config(
        providers={
            "dgx": ProviderConfig(
                protocol=protocol,
                api_key=DGX_API_KEY,
                base_url=DGX_BASE_URL,
                models={"dgx": ModelSpec(name=DGX_MODEL, max_output_tokens=4096, max_context_tokens=256000)},
            )
        },
        agents={"master": AgentConfig(model="dgx/dgx")},
        system=SystemConfig(),
    )


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, data: Any) -> Path:
    p = Path(path)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p
