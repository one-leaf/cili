"""流式/长任务专用线程池：单例、规格，以及 SSE 轮询确实走它（防回退）。

回归背景：``/api/events`` 的阻塞 ``queue.get`` 原先走事件循环的**默认** executor，
长驻的 SSE 流会把默认池占满，导致请求期短任务（``/api/workspaces/{uuid}/sessions``）
永久排队——症状是主页加载卡死。长任务必须走专用池。
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from core.stream_executor import (
    MAX_STREAM_WORKERS,
    get_stream_executor,
    shutdown_stream_executor,
)


class TestStreamExecutor:
    def test_singleton(self):
        assert get_stream_executor() is get_stream_executor()

    def test_sizing_and_thread_naming(self):
        ex = get_stream_executor()
        assert ex._max_workers == MAX_STREAM_WORKERS
        assert ex._thread_name_prefix == "cili-stream"

    def test_shutdown_resets_singleton(self):
        first = get_stream_executor()
        shutdown_stream_executor()
        assert get_stream_executor() is not first


class _RecordingExecutor(ThreadPoolExecutor):
    """记录 submit 次数的真实线程池：证明调用点确实把活儿交给了它。"""

    def __init__(self):
        super().__init__(max_workers=2, thread_name_prefix="test-stream")
        self.submissions = 0

    def submit(self, fn, /, *args, **kwargs):
        self.submissions += 1
        return super().submit(fn, *args, **kwargs)


class TestEventsPollingUsesStreamExecutor:
    def test_polling_submits_to_stream_executor(self, monkeypatch):
        import web.routes_chat as routes_chat
        from core.event_bus import get_event_bus

        rec = _RecordingExecutor()
        monkeypatch.setattr(routes_chat, "get_stream_executor", lambda: rec)
        try:
            async def run():
                resp = await routes_chat.stream_global_events(session_id="sess-sep")
                agen = resp.body_iterator
                get_event_bus().publish({
                    "type": "text", "workspace_uuid": "ws-x",
                    "session_id": "sess-sep", "content": "hi",
                })
                chunk = await agen.__anext__()
                await agen.aclose()
                return chunk if isinstance(chunk, str) else chunk.decode()

            assert "hi" in asyncio.run(run())
            assert rec.submissions >= 1
        finally:
            rec.shutdown(wait=False)


class TestConfigTestEndpointDoesNotBlockLoop:
    """回归：/api/config/test（设置里的「测试连接」）内部是同步阻塞的 LLM 请求
    （重试总上限 300s），必须走专用池——否则冻住整个事件循环，所有页面一起卡死。"""

    def test_blocking_probe_runs_off_event_loop(self, monkeypatch):
        import core.llm as core_llm
        import web.routes_config as routes_config

        rec = _RecordingExecutor()
        seen: dict = {}

        class _FakeClient:
            def test_connection(self):
                seen["thread"] = threading.current_thread().name
                return True, "ok"

            def close(self):
                pass

        monkeypatch.setattr(routes_config, "get_stream_executor", lambda: rec)
        monkeypatch.setattr(
            routes_config, "_resolve_test_provider",
            lambda req: ("openai", "k", "https://example.invalid", {}),
        )
        monkeypatch.setattr(core_llm, "create_llm_client", lambda cfg: _FakeClient())
        try:
            req = SimpleNamespace(model_name="m", api_key="k", base_url="",
                                  interface_type=None, auth_type=None, headers=None)
            out = asyncio.run(routes_config.test_config(req))
            assert out["success"] is True
            assert rec.submissions >= 1
            assert seen["thread"].startswith("test-stream")     # 不在事件循环线程上跑
            assert seen["thread"] != threading.current_thread().name
        finally:
            rec.shutdown(wait=False)
