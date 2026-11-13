"""上下文占用接口（/context）：口径、两条路径、缓存比与兜底。

沿用 web 路由测试约定：不用 TestClient（会与 asyncio.to_thread 死锁），
直接调用路由函数。
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.config import (
    AgentConfig,
    Config,
    ImageConfig,
    ModelSpec,
    ProviderConfig,
    SystemConfig,
)
from web import routes_chat
from web.routes_chat import _cache_hit_ratio


def _config(interface="anthropic", max_ctx=10000, extra_models=None):
    models = {"a": ModelSpec(name="Model A", max_context_tokens=max_ctx)}
    if extra_models:
        models.update(extra_models)
    return Config(
        providers={
            "p": ProviderConfig(
                interface_type=interface,
                api_key="test",
                base_url="http://localhost",
                models=models,
            )
        },
        agents={"master": AgentConfig(model="p/a")},
        system=SystemConfig(),
        images=ImageConfig(),
    )


class _FakeSM:
    """SessionStore 最小替身：messages / metadata / get_usage。"""

    def __init__(self, messages, metadata=None, name="测试会话", session_dir=None):
        self.messages = messages
        self.metadata = metadata or {
            "created_at": "2026-11-12 10:00:00",
            "updated_at": "2026-11-13 15:00:00",
        }
        self.name = name
        self.session_dir = Path(session_dir or ".")

    def get_usage(self):
        return {
            "input_tokens": 100,
            "output_tokens": 20,
            "api_calls": 2,
            "cache_read_tokens": 300,
            "cache_creation_tokens": 50,
        }


class _FakeRunner:
    """master runner 最小替身（只暴露 /context 使用的方法）。"""

    def __init__(self, sm, messages, config, running=False):
        self.session = sm
        self.model = config.resolve_model("p/a")
        self.model_ref = "p/a"
        self._messages = messages
        self._running = running
        self.usage = {
            "input_tokens": 10,
            "output_tokens": 5,
            "api_calls": 1,
            "cache_read_tokens": 90,
            "cache_creation_tokens": 0,
        }

    def build_projected_messages(self):
        return self._messages

    def _count_messages_tokens(self, messages):
        return 1234

    def get_usage(self):
        return dict(self.usage)

    def is_running(self):
        return self._running


def _patch(monkeypatch, config, sm=None, runner=None):
    monkeypatch.setattr(routes_chat, "load_config", lambda: config)
    monkeypatch.setattr(routes_chat, "_get_session", lambda ws, sid: sm)
    monkeypatch.setattr(
        routes_chat, "registry", SimpleNamespace(get=lambda key: runner)
    )


def _call(workspace="ws-a", session="s1"):
    return asyncio.run(
        routes_chat.get_session_context(workspace, session, Path("."))
    )


class TestFallbackPath:
    """无 runner：从存盘消息估算。"""

    @staticmethod
    def _messages():
        return [
            {"role": "user", "content": "请帮我查一下这个文件的内容"},
            {"role": "assistant", "content": [{"type": "text", "text": "好的"}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "工具输出"}
                ],
            },
            # 压缩失效消息不计入
            {"role": "user", "content": "失效的历史", "_meta": {"valid": False}},
            # 仅 UI 展示的卡片不计入
            {"role": "assistant", "content": "文件卡片", "_meta": {"file_card": True}},
        ]

    def test_report_fields(self, monkeypatch):
        cfg = _config()
        sm = _FakeSM(self._messages())
        _patch(monkeypatch, cfg, sm=sm, runner=None)

        data = _call()

        assert data["estimated"] is True
        assert data["running"] is False
        assert data["model"]["ref"] == "p/a"
        assert data["model"]["name"] == "Model A"
        assert data["model"]["interface_type"] == "anthropic"
        assert data["max_context_tokens"] == 10000
        assert data["compact_threshold_tokens"] == 8000
        assert data["context_tokens"] > 0
        assert data["ratio"] == round(data["context_tokens"] / 10000, 4)
        assert data["remaining_tokens"] == max(0, 10000 - data["context_tokens"])
        assert data["body_size"] > 0
        # 有效消息 3 条（失效 + 文件卡片被过滤）；纯文本 user 仅 1 条
        assert data["session"]["message_count"] == 3
        assert data["session"]["user_turn_count"] == 1
        assert data["session"]["created_at"] == "2026-11-12 10:00:00"
        # Anthropic 语义：300 / (100 + 300 + 50)
        assert data["usage"]["cache_hit_ratio"] == round(300 / 450, 4)

    def test_model_override_changes_window(self, monkeypatch):
        cfg = _config(extra_models={"b": ModelSpec(name="Model B", max_context_tokens=200000)})
        sm = _FakeSM(
            self._messages(),
            metadata={
                "model_ref": "p/b",
                "created_at": "",
                "updated_at": "",
            },
        )
        _patch(monkeypatch, cfg, sm=sm, runner=None)

        data = _call()

        assert data["model"]["ref"] == "p/b"
        assert data["max_context_tokens"] == 200000
        assert data["compact_threshold_tokens"] == 160000

    def test_no_model_config_still_200(self, monkeypatch):
        cfg = Config()
        sm = _FakeSM(self._messages())
        _patch(monkeypatch, cfg, sm=sm, runner=None)

        data = _call()

        assert data["max_context_tokens"] == 0
        assert data["ratio"] == 0.0
        assert data["model"]["ref"] == ""

    def test_missing_session_404(self, monkeypatch):
        _patch(monkeypatch, _config(), sm=None, runner=None)

        with pytest.raises(HTTPException) as exc_info:
            _call()
        assert exc_info.value.status_code == 404


class TestRunnerPath:
    """有 runner：复用请求投影，且不读盘。"""

    def test_runner_fields_and_no_disk_read(self, monkeypatch):
        cfg = _config()
        sm = _FakeSM([{"role": "user", "content": "hi"}])
        runner = _FakeRunner(sm, [{"role": "user", "content": "hi"}], cfg, running=True)

        monkeypatch.setattr(routes_chat, "load_config", lambda: cfg)
        monkeypatch.setattr(
            routes_chat,
            "_get_session",
            lambda ws, sid: (_ for _ in ()).throw(AssertionError("不应读盘")),
        )
        monkeypatch.setattr(
            routes_chat, "registry", SimpleNamespace(get=lambda key: runner)
        )

        data = _call()

        assert data["estimated"] is False
        assert data["running"] is True
        assert data["context_tokens"] == 1234
        assert data["ratio"] == round(1234 / 10000, 4)
        assert data["max_context_tokens"] == 10000
        assert data["usage"]["api_calls"] == 1
        assert data["session"]["message_count"] == 1
        assert data["session"]["user_turn_count"] == 1


class TestCacheHitRatio:
    def test_anthropic_semantics(self):
        usage = {"input_tokens": 100, "cache_read_tokens": 300, "cache_creation_tokens": 100}
        assert _cache_hit_ratio(usage, "anthropic") == 0.6

    def test_openai_semantics(self):
        # OpenAI: input_tokens 即 prompt_tokens（缓存已含其中）
        usage = {"input_tokens": 1000, "cache_read_tokens": 250}
        assert _cache_hit_ratio(usage, "openai") == 0.25

    def test_empty_usage(self):
        assert _cache_hit_ratio({}, "anthropic") == 0.0
        assert _cache_hit_ratio({"input_tokens": 0}, "openai") == 0.0
