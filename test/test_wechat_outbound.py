"""微信出站链路单测：文本清洗、发送预算与 defer、typing、sink 的缓冲与切分。

sink 的回调跑在 runner 线程上，真正发送是提交到宿主 loop 的——测试里自己建一个
loop 并手动推一下，走的就是生产那条 ``run_coroutine_threadsafe`` 路径。
"""

from __future__ import annotations

import asyncio

import pytest

# ─── 文本清洗 ─────────────────────────────────────────────────────────


class TestSanitize:
    def test_strips_scaffolding(self):
        from interfaces.wechat.sanitize import sanitize_wechat_text
        s = "hello\n<system-reminder>internal</system-reminder>\nworld"
        out = sanitize_wechat_text(s)
        assert "internal" not in out
        assert "hello" in out and "world" in out

    def test_angle_brackets_become_fullwidth_outside_code(self):
        """微信客户端把裸 ``<`` 当标记，可能吞掉后面整段内容。"""
        from interfaces.wechat.sanitize import clean_markdown_for_wechat
        out = clean_markdown_for_wechat("a <b> c")
        assert out == "a ＜b＞ c"

    def test_code_regions_are_preserved_byte_for_byte(self):
        from interfaces.wechat.sanitize import clean_markdown_for_wechat
        fenced = "text\n```python\nif a < b: pass\n```\nmore <x>"
        out = clean_markdown_for_wechat(fenced)
        assert "if a < b: pass" in out            # 代码区原样
        assert "more ＜x＞" in out                  # 区外转换

        inline = "看 `a < b` 这个表达式 <ok>"
        out2 = clean_markdown_for_wechat(inline)
        assert "`a < b`" in out2
        assert "＜ok＞" in out2

    def test_removes_strikethrough_and_images(self):
        from interfaces.wechat.sanitize import clean_markdown_for_wechat
        out = clean_markdown_for_wechat("~~删掉~~ ![图](https://x/y.png) 保留")
        assert "~~" not in out
        assert "![图]" not in out
        assert "保留" in out

    def test_strips_deep_headings_only(self):
        from interfaces.wechat.sanitize import clean_markdown_for_wechat
        out = clean_markdown_for_wechat("# 一级\n##### 五级\n###### 六级")
        assert "# 一级" in out
        assert "五级" in out and "#####" not in out
        assert "六级" in out and "######" not in out

    def test_empty(self):
        from interfaces.wechat.sanitize import clean_markdown_for_wechat
        assert clean_markdown_for_wechat("") == ""


# ─── 假件 ─────────────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self, *, quota_error: bool = False, config: dict | None = None):
        self.sent: list[dict] = []
        self.config_calls: list[tuple] = []
        self.typing_calls: list[tuple] = []
        self._quota_error = quota_error
        self._config = config or {}

    async def send_message(self, msg: dict) -> dict:
        if self._quota_error:
            from interfaces.wechat.protocol import WechatQuotaError
            raise WechatQuotaError("sendmessage", ret=-2)
        self.sent.append(msg)
        return {"ret": 0}

    async def get_config(self, user_id: str, context_token: str = "") -> dict:
        self.config_calls.append((user_id, context_token))
        return self._config

    async def send_typing(self, user_id: str, ticket: str, status: int) -> dict:
        self.typing_calls.append((user_id, ticket, status))
        return {"ret": 0}


def _tokens(*, budget: int = 8, max_age: float = 60.0):
    from interfaces.wechat.account import ContextTokenStore
    store = ContextTokenStore(max_age_seconds=max_age, budget=budget)
    store.update("u1", "ctx-1")
    return store


def _sender(*, budget: int = 8, max_age: float = 60.0, quota_error: bool = False,
            config: dict | None = None, defer_max: int = 3):
    from interfaces.wechat.config import WechatConfig
    from interfaces.wechat.outbound import OutboundSender
    cfg = WechatConfig(defer_queue_max=defer_max)
    client = _FakeClient(quota_error=quota_error, config=config)
    tokens = _tokens(budget=budget, max_age=max_age)
    return OutboundSender(cfg, client, tokens), client, tokens


# ─── 发送预算与 defer ─────────────────────────────────────────────────


class TestOutboundSender:
    def test_sends_when_budget_available(self):
        sender, client, _ = _sender()
        ok = asyncio.run(sender.send_text("u1", "你好"))
        assert ok is True
        assert client.sent[0]["item_list"][0]["text_item"]["text"] == "你好"
        assert client.sent[0]["message_type"] == 2       # BOT
        assert client.sent[0]["message_state"] == 2      # FINISH
        assert client.sent[0]["context_token"] == "ctx-1"

    def test_budget_decrements_after_send(self):
        sender, _, _ = _sender(budget=3)
        asyncio.run(sender.send_text("u1", "a"))
        assert sender.budget_left("u1") == 2

    def test_defers_when_budget_exhausted_without_calling_api(self):
        """本地预算兜底的意义：超预算时**不打 API**，省掉一次白发的请求。"""
        sender, client, _ = _sender(budget=1)
        assert asyncio.run(sender.send_text("u1", "第一条")) is True
        assert asyncio.run(sender.send_text("u1", "第二条")) is False
        assert len(client.sent) == 1                     # 第二条没发出去
        assert sender.deferred_count("u1") == 1

    def test_reserve_reduces_effective_budget(self):
        sender, _, _ = _sender(budget=1)
        assert asyncio.run(sender.send_text("u1", "留余量", reserve=1)) is False

    def test_defers_on_server_quota_error(self):
        sender, client, _ = _sender(quota_error=True)
        assert asyncio.run(sender.send_text("u1", "被限流")) is False
        assert client.sent == []
        assert sender.deferred_count("u1") == 1

    def test_no_token_means_no_send(self):
        sender, client, tokens = _sender()
        tokens.drop("u1")
        assert asyncio.run(sender.send_text("u1", "无上下文")) is False
        assert client.sent == []

    def test_empty_text_is_noop(self):
        sender, client, _ = _sender()
        assert asyncio.run(sender.send_text("u1", "")) is True
        assert client.sent == []

    def test_replay_reuses_client_id(self):
        """重放必须复用 client_id——服务端据此把重复投递识别为同一条消息。"""
        sender, client, _ = _sender(quota_error=True)
        asyncio.run(sender.send_text("u1", "被推迟的"))
        deferred_id = next(iter(sender._deferred["u1"]))

        client._quota_error = False
        asyncio.run(sender.replay_deferred("u1"))

        assert len(client.sent) == 1
        assert client.sent[0]["client_id"] == deferred_id
        assert sender.deferred_count("u1") == 0

    def test_replay_stops_when_budget_gone(self):
        sender, client, _ = _sender(budget=1, quota_error=True)
        asyncio.run(sender.send_text("u1", "一"))
        asyncio.run(sender.send_text("u1", "二"))
        client._quota_error = False

        asyncio.run(sender.replay_deferred("u1"))

        assert len(client.sent) == 1                     # 预算只剩 1 条
        assert sender.deferred_count("u1") == 1

    def test_defer_queue_drops_oldest(self):
        sender, _, _ = _sender(quota_error=True, defer_max=2)
        for text in ("一", "二", "三"):
            asyncio.run(sender.send_text("u1", text))
        assert sender.deferred_count("u1") == 2
        texts = [p.text for p in sender._deferred["u1"].values()]
        assert texts == ["二", "三"]                      # 最旧的一条被丢掉

    def test_refreshes_stale_token(self):
        sender, client, tokens = _sender(config={"context_token": "ctx-2"})
        tokens._entries["u1"].refreshed_at -= 120        # 装作很老了
        asyncio.run(sender.send_text("u1", "hi"))
        assert client.config_calls == [("u1", "ctx-1")]
        assert client.sent[0]["context_token"] == "ctx-2"
        assert tokens.token("u1") == "ctx-2"

    def test_does_not_refresh_fresh_token(self):
        sender, client, _ = _sender()
        asyncio.run(sender.send_text("u1", "hi"))
        assert client.config_calls == []                 # 新 token 不用刷

    def test_refresh_failure_falls_back_to_old_token(self):
        sender, client, tokens = _sender()
        tokens._entries["u1"].refreshed_at -= 120

        async def boom(user_id, context_token=""):
            raise RuntimeError("网络炸了")

        client.get_config = boom
        assert asyncio.run(sender.send_text("u1", "hi")) is True
        assert client.sent[0]["context_token"] == "ctx-1"


# ─── typing ───────────────────────────────────────────────────────────


class TestTypingManager:
    def test_disabled_is_noop(self):
        from interfaces.wechat.config import WechatConfig
        from interfaces.wechat.typing import TypingManager
        client = _FakeClient(config={"typing_ticket": "t1"})
        mgr = TypingManager(WechatConfig(typing_enabled=False), client)

        async def scenario():
            await mgr.start("u1", "ctx-1")
            await mgr.stop("u1")

        asyncio.run(scenario())
        assert client.typing_calls == []

    def test_start_sends_typing_then_stop_cancels(self):
        from interfaces.wechat.config import WechatConfig
        from interfaces.wechat.typing import TypingManager
        client = _FakeClient(config={"typing_ticket": "t1"})
        mgr = TypingManager(WechatConfig(), client)

        async def scenario():
            await mgr.start("u1", "ctx-1")
            assert client.typing_calls == [("u1", "t1", 1)]
            await mgr.stop("u1")

        asyncio.run(scenario())
        assert client.typing_calls[-1] == ("u1", "t1", 2)     # 取消

    def test_ticket_is_cached(self):
        from interfaces.wechat.config import WechatConfig
        from interfaces.wechat.typing import TypingManager
        client = _FakeClient(config={"typing_ticket": "t1"})
        mgr = TypingManager(WechatConfig(), client)

        async def scenario():
            assert await mgr.ticket("u1", "ctx-1") == "t1"
            assert await mgr.ticket("u1", "ctx-1") == "t1"

        asyncio.run(scenario())
        assert len(client.config_calls) == 1                  # 第二次命中缓存

    def test_ticket_failure_returns_empty(self):
        from interfaces.wechat.config import WechatConfig
        from interfaces.wechat.typing import TypingManager
        client = _FakeClient()

        async def boom(user_id, context_token=""):
            raise RuntimeError("网络炸了")

        client.get_config = boom
        mgr = TypingManager(WechatConfig(), client)
        assert asyncio.run(mgr.ticket("u1", "ctx-1")) == ""


# ─── sink ─────────────────────────────────────────────────────────────


class _FakeOutbound:
    def __init__(self, *, budget: int = 8):
        self.sent: list[str] = []
        self.budget = budget

    def budget_left(self, user_id: str, *, reserve: int = 0) -> int:
        return max(0, self.budget - reserve)

    def deferred_count(self, user_id: str) -> int:
        return 0

    async def send_text(self, user_id: str, text: str, *, client_id: str = "",
                        reserve: int = 0) -> bool:
        if self.budget <= 0:
            return False
        self.budget -= 1
        self.sent.append(text)
        return True


class _FakeTyping:
    def __init__(self):
        self.stops = 0

    async def start(self, *a, **kw) -> None:
        pass

    async def stop(self, *a, **kw) -> None:
        self.stops += 1


def _sink(*, budget: int = 8, chunk_limit: int = 1800):
    from interfaces.wechat.account import WechatAccount
    from interfaces.wechat.config import WechatConfig
    from interfaces.wechat.runtime import WechatRuntime
    from interfaces.wechat.sink import WechatSink
    from interfaces.wechat.state import WechatState

    loop = asyncio.new_event_loop()
    outbound = _FakeOutbound(budget=budget)
    typing = _FakeTyping()
    rt = WechatRuntime(
        cfg=WechatConfig(text_chunk_limit=chunk_limit),
        state=WechatState(),
        account=WechatAccount(),
        tokens=_tokens(),
        loop=loop,
    )
    rt.outbound = outbound
    rt.typing = typing
    return WechatSink(rt, "u1"), outbound, typing, loop


def _flush_loop(loop) -> None:
    """把提交到宿主 loop 的发送任务跑完（测试里 loop 没在跑，手动推）。"""
    loop.run_until_complete(asyncio.sleep(0.05))
    loop.close()


class TestWechatSink:
    def test_buffers_until_turn_complete(self):
        """回合内不发正文——iLink 不能更新已发消息，边生成边发只会刷屏。"""
        sink, outbound, _, loop = _sink()
        sink.on_text("第一段")
        sink.on_text("第二段")
        assert outbound.sent == []

        sink.on_turn_complete()
        _flush_loop(loop)
        assert outbound.sent == ["第一段第二段"]

    def test_stops_typing_at_turn_end(self):
        sink, _, typing, loop = _sink()
        sink.on_text("hi")
        sink.on_turn_complete()
        _flush_loop(loop)
        assert typing.stops == 1

    def test_chunks_long_text(self):
        sink, outbound, _, loop = _sink(chunk_limit=100)
        sink.on_text("x" * 250)
        sink.on_turn_complete()
        _flush_loop(loop)
        assert len(outbound.sent) == 3
        assert "".join(outbound.sent) == "x" * 250

    def test_tool_lines_dedupe_consecutive(self):
        sink, outbound, _, loop = _sink()
        sink.on_tool_call("read", {}, "t1")
        sink.on_tool_call("read", {}, "t2")      # 连续同名 → 只记一次
        sink.on_tool_call("grep", {}, "t3")
        sink.on_turn_complete()
        _flush_loop(loop)
        text = outbound.sent[0]
        assert text.count("正在调用 read 工具") == 1
        assert text.count("正在调用 grep 工具") == 1

    def test_ask_user_folded_into_text(self):
        sink, outbound, _, loop = _sink()
        sink.on_tool_call("ask_user", {"questions": [{
            "question": "选哪个？",
            "options": [{"label": "甲", "description": "第一个"}, {"label": "乙"}],
        }]}, "t1")
        sink.on_turn_complete()
        _flush_loop(loop)
        text = outbound.sent[0]
        assert "选哪个？" in text
        assert "1. 甲 — 第一个" in text
        assert "2. 乙" in text

    def test_retry_clear_sentinel_resets_buffer(self):
        from core.base_session_runner import RETRY_CLEAR_SENTINEL
        sink, outbound, _, loop = _sink()
        sink.on_text("要被丢弃的内容")
        sink.on_text(RETRY_CLEAR_SENTINEL)
        sink.on_text("重试后的内容")
        sink.on_turn_complete()
        _flush_loop(loop)
        assert outbound.sent == ["重试后的内容"]

    def test_sanitizes_at_flush_not_per_chunk(self):
        """成对标签可能被切在两次回调之间，逐块清洗会漏掉。"""
        sink, outbound, _, loop = _sink()
        sink.on_text("回答：<system-")
        sink.on_text("reminder>内部</system-reminder>完")
        sink.on_turn_complete()
        _flush_loop(loop)
        assert "内部" not in outbound.sent[0]
        assert "回答：" in outbound.sent[0]
        assert "完" in outbound.sent[0]

    def test_truncates_when_over_budget(self):
        """不静默丢内容：截断要显式标注。"""
        sink, outbound, _, loop = _sink(budget=3, chunk_limit=50)
        sink.on_text("x" * 500)                  # 会切成 10 片，但预算只够 2 条
        sink.on_turn_complete()
        _flush_loop(loop)
        assert len(outbound.sent) == 2
        assert outbound.sent[-1].endswith("…（内容过长，已截断）")

    def test_fans_out_to_base(self):
        """微信驱动的回合必须在 Web 的全局事件流里可见。"""
        from core.output_sink import OutputSink
        from interfaces.wechat.account import WechatAccount
        from interfaces.wechat.config import WechatConfig
        from interfaces.wechat.runtime import WechatRuntime
        from interfaces.wechat.sink import WechatSink
        from interfaces.wechat.state import WechatState

        seen: list = []
        loop = asyncio.new_event_loop()
        rt = WechatRuntime(
            cfg=WechatConfig(), state=WechatState(), account=WechatAccount(),
            tokens=_tokens(), loop=loop,
        )
        rt.outbound = _FakeOutbound()
        rt.typing = _FakeTyping()
        sink = WechatSink(rt, "u1", base=OutputSink(
            on_text=seen.append,
            on_turn_complete=lambda: seen.append("<complete>"),
        ))

        sink.on_text("正文")
        sink.on_turn_complete()

        assert "正文" in seen
        assert "<complete>" in seen
        _flush_loop(loop)
