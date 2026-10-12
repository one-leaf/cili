"""微信入站链单测：状态持久化、owner 绑定、指令、长轮询过滤、媒体工具、编号回映射。

回合本身（``_run_turn``）要真 workspace + runner，属集成范畴；这里只测到「消息被正确
筛选、解析、分流」为止——对齐 ``test_qq_inbound.py`` 的粒度。
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dirs(tmp_path, monkeypatch):
    """把微信接入端的两个持久化位置都指到临时目录。

    ``account.py`` 与 ``state.py`` 各自 ``from core.config import DATA_DIR``（模块级
    绑定），必须分别 patch。Poller 的游标落盘、owner 绑定都有写盘副作用——不隔离
    就会写进真实的 ``data/cili/tools/wechat/``（本文件曾因此漏写 account.json）。
    """
    import interfaces.wechat.account as account_mod
    import interfaces.wechat.state as state_mod
    monkeypatch.setattr(account_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(state_mod, "DATA_DIR", tmp_path)


# ─── 假件 ─────────────────────────────────────────────────────────────


def _runtime(*, cursor: str = "", cfg: dict | None = None):
    from interfaces.wechat.account import ContextTokenStore, WechatAccount
    from interfaces.wechat.config import WechatConfig
    from interfaces.wechat.runtime import WechatRuntime
    from interfaces.wechat.state import WechatState

    return WechatRuntime(
        cfg=WechatConfig(**(cfg or {})),
        state=WechatState(),
        account=WechatAccount(get_updates_buf=cursor),
        tokens=ContextTokenStore(max_age_seconds=60, budget=8),
        loop=asyncio.new_event_loop(),
    )


class _FakeClient:
    """按顺序吐 ``get_updates`` 响应；吐完再被调用就抛错（测试用它收尾）。"""

    def __init__(self, responses: list[dict] | None = None):
        self._responses = list(responses or [])
        self.notify_calls: list[str] = []

    async def get_updates(self, cursor: str) -> dict:
        if not self._responses:
            raise RuntimeError("没有更多响应")
        return self._responses.pop(0)

    async def notify(self, action: str) -> dict:
        self.notify_calls.append(action)
        return {"ret": 0}


class _FakeHandler:
    def __init__(self):
        self.handled: list = []

    async def handle(self, inbound) -> None:
        self.handled.append(inbound)


class _FakeOutbound:
    def __init__(self, *, budget: int = 8, deferred: int = 0):
        self._budget = budget
        self._deferred = deferred

    def budget_left(self, user_id: str, *, reserve: int = 0) -> int:
        return max(0, self._budget - reserve)

    def deferred_count(self, user_id: str) -> int:
        return self._deferred


def _text_msg(text: str, *, mid: str = "m1", sender: str = "u1",
              msg_type: int = 1, token: str = "") -> dict:
    msg = {
        "message_id": mid,
        "from_user_id": sender,
        "message_type": msg_type,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if token:
        msg["context_token"] = token
    return msg


# ─── 状态 ─────────────────────────────────────────────────────────────


class TestWechatState:
    def test_from_dict_tolerates_junk(self):
        from interfaces.wechat.state import WechatState
        assert WechatState.from_dict(None) == WechatState()
        assert WechatState.from_dict({"owner_user_id": 123}).owner_user_id == "123"

    def test_save_and_load(self, tmp_path, monkeypatch):
        import interfaces.wechat.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        state = st.WechatState(owner_user_id="u1", workspace_uuid="w1", session_id="s1")
        assert st.save_state(state)
        assert st.load_state() == state

    def test_load_missing_returns_empty(self, tmp_path, monkeypatch):
        import interfaces.wechat.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        assert st.load_state() == st.WechatState()


# ─── owner 绑定 ───────────────────────────────────────────────────────


class TestOwnerTracker:
    def _tracker(self, state, monkeypatch):
        import interfaces.wechat.access as acc
        monkeypatch.setattr(acc, "save_state", lambda s: True)
        from interfaces.wechat.access import OwnerTracker
        return OwnerTracker(state)

    def test_binds_first_sender(self, monkeypatch):
        from interfaces.wechat.state import WechatState
        state = WechatState()
        tr = self._tracker(state, monkeypatch)
        assert tr.track("u1") is True
        assert tr.owner_user_id == "u1"
        assert state.owner_user_id == "u1"

    def test_refreshes_when_sender_changes(self, monkeypatch):
        from interfaces.wechat.state import WechatState
        state = WechatState(owner_user_id="u1")
        tr = self._tracker(state, monkeypatch)
        assert tr.track("u2") is True
        assert tr.owner_user_id == "u2"

    def test_empty_sender_rejected(self, monkeypatch):
        from interfaces.wechat.state import WechatState
        tr = self._tracker(WechatState(), monkeypatch)
        assert tr.track("") is False


# ─── 指令 ─────────────────────────────────────────────────────────────


class TestCommands:
    def test_parse(self):
        from interfaces.wechat.commands import parse
        assert parse("/w 2") == ("w", "2")
        assert parse("/HELP") == ("help", "")
        assert parse("普通消息") is None
        assert parse("") is None

    def test_aliases_point_to_same_command(self):
        from interfaces.wechat.commands import build_commands
        table = build_commands()
        assert table["h"] is table["help"]
        assert table["w"] is table["workspace"]
        assert table["s"] is table["session"]

    def test_unique_commands_dedupes_aliases(self):
        from interfaces.wechat.commands import unique_commands
        names = [c.name for c in unique_commands()]
        assert names == ["h", "w", "s", "new", "status", "stop"]

    def test_unknown_command(self):
        rt = _runtime()
        from interfaces.wechat.commands import dispatch
        out = asyncio.run(dispatch(rt, rt.state, "/bogus"))
        assert "未知指令" in out

    def test_non_command_returns_none(self):
        rt = _runtime()
        from interfaces.wechat.commands import dispatch
        assert asyncio.run(dispatch(rt, rt.state, "你好")) is None

    def test_help_lists_commands(self):
        rt = _runtime()
        from interfaces.wechat.commands import dispatch
        out = asyncio.run(dispatch(rt, rt.state, "/h"))
        assert "可用指令" in out
        assert "/status" in out

    def test_status_reports_owner_and_budget(self):
        rt = _runtime()
        rt.state.owner_user_id = "u1"
        rt.outbound = _FakeOutbound(budget=5, deferred=2)
        from interfaces.wechat.commands import dispatch
        out = asyncio.run(dispatch(rt, rt.state, "/status"))
        assert "u1" in out
        assert "本轮剩余发送条数：5" in out
        assert "待重发（配额受限）：2 条" in out


# ─── 长轮询 ───────────────────────────────────────────────────────────


class TestPoller:
    def _poller(self, responses, *, cursor: str = ""):
        from interfaces.wechat.poller import Poller
        rt = _runtime(cursor=cursor)
        client = _FakeClient(responses)
        rt.client = client
        handler = _FakeHandler()
        return Poller(rt, handler), client, handler, rt

    def test_filters_bot_group_and_duplicates(self):
        """只放行「私聊 + 不是自己发的 + 没见过」的消息。"""
        poller, _, handler, _ = self._poller([{
            "get_updates_buf": "c2",
            "msgs": [
                _text_msg("hi", mid="m1"),
                _text_msg("bot 自己发的", mid="m2", msg_type=2),
                _text_msg("群聊", mid="m3", sender="123@chatroom"),
                _text_msg("重复", mid="m1"),
                {"message_id": "m4"},                       # 没有发送者
                "not-a-dict",
            ],
        }])
        asyncio.run(poller._poll_once())
        assert [m.text for m in handler.handled] == ["hi"]

    def test_dedupe_survives_across_polls(self):
        poller, _, handler, _ = self._poller([
            {"get_updates_buf": "c1", "msgs": [_text_msg("一", mid="m1")]},
            {"get_updates_buf": "c2", "msgs": [_text_msg("一again", mid="m1")]},
        ])
        asyncio.run(poller._poll_once())
        asyncio.run(poller._poll_once())
        assert [m.text for m in handler.handled] == ["一"]

    def test_persists_cursor(self, tmp_path, monkeypatch):
        """游标丢了重启会把积压消息重收一遍。"""
        import interfaces.wechat.account as account_mod
        monkeypatch.setattr(account_mod, "DATA_DIR", tmp_path)

        poller, _, _, rt = self._poller([{"get_updates_buf": "c9", "msgs": []}], cursor="c1")
        asyncio.run(poller._poll_once())

        assert rt.account.get_updates_buf == "c9"
        assert account_mod.load_account().get_updates_buf == "c9"

    def test_auth_error_stops_and_propagates(self):
        """-14 → 轮询停掉并向上抛，由服务层提示重新扫码。"""
        from interfaces.wechat.protocol import WechatAuthError
        poller, client, _, _ = self._poller([])

        async def boom(cursor):
            raise WechatAuthError("getupdates", ret=-14)

        client.get_updates = boom
        with pytest.raises(WechatAuthError):
            asyncio.run(poller.run())
        assert poller.running is False

    def test_notifies_lifecycle(self, monkeypatch):
        import interfaces.wechat.poller as poller_mod
        poller, client, _, _ = self._poller([{"get_updates_buf": "c1", "msgs": []}])

        async def stop_now(delay):
            await poller.stop()

        # 第一轮成功；第二轮没有响应 → 抛错 → 走退避分支（被我们用来收尾）
        monkeypatch.setattr(poller_mod.asyncio, "sleep", stop_now)
        asyncio.run(poller.run())

        assert client.notify_calls == ["start", "stop"]

    def test_backoff_grows_after_repeated_failures(self, monkeypatch):
        import interfaces.wechat.poller as poller_mod
        poller, client, _, _ = self._poller([])

        async def boom(cursor):
            raise RuntimeError("网络炸了")

        client.get_updates = boom
        sleeps: list[float] = []

        async def fake_sleep(delay):
            sleeps.append(delay)
            if len(sleeps) >= poller_mod.MAX_CONSECUTIVE_FAILURES:
                await poller.stop()

        monkeypatch.setattr(poller_mod.asyncio, "sleep", fake_sleep)
        asyncio.run(poller.run())

        assert sleeps[0] == poller_mod.RETRY_DELAY_S
        assert sleeps[-1] == poller_mod.BACKOFF_DELAY_S


# ─── 媒体工具 ─────────────────────────────────────────────────────────


class TestMediaHelpers:
    def test_infer_upload_type(self):
        from interfaces.wechat import media
        assert media.infer_upload_type("a.png")[:2] == (
            media.UPLOAD_MEDIA_IMAGE, media.ITEM_IMAGE,
        )
        assert media.infer_upload_type("a.PDF")[:2] == (
            media.UPLOAD_MEDIA_FILE, media.ITEM_FILE,
        )

    def test_validate_media_path_rejects_outside_and_missing(self, tmp_path):
        from interfaces.wechat.media import MediaError, validate_media_path
        inside = tmp_path / "a.txt"
        inside.write_text("x")
        assert validate_media_path(inside, tmp_path) == inside

        outside = tmp_path.parent / "b.txt"
        outside.write_text("x")
        with pytest.raises(MediaError):
            validate_media_path(outside, tmp_path)
        with pytest.raises(MediaError):
            validate_media_path(tmp_path / "missing.txt", tmp_path)

    def test_sniff_image_mime(self, tmp_path):
        from interfaces.wechat.media import _sniff_image_mime
        png = tmp_path / "a"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
        assert _sniff_image_mime(png) == "image/png"

        jpg = tmp_path / "b"
        jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 8)
        assert _sniff_image_mime(jpg) == "image/jpeg"

    def test_download_media_decrypts_and_sniffs(self, tmp_path, monkeypatch):
        """入站图片：下载 → AES-ECB 解密 → 落盘 → 按魔数判类型。"""
        import core.media_fetch as mf
        from interfaces.wechat.config import WechatConfig
        from interfaces.wechat.media import download_media
        from interfaces.wechat.protocol import MediaRef, aes_ecb_encrypt

        # SSRF 校验会解析域名；测试里固定成公网地址
        monkeypatch.setattr(mf.socket, "getaddrinfo", lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        ])

        key = bytes(range(16))
        png = b"\x89PNG\r\n\x1a\n" + b"p" * 40
        ciphertext = aes_ecb_encrypt(png, key)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=ciphertext))
        )

        item = asyncio.run(download_media(
            MediaRef(kind="image", full_url="https://cdn.example/a", aes_key=key.hex()),
            cfg=WechatConfig(),
            client=client,
            uploads_dir=tmp_path / "uploads",
            workspace_dir=tmp_path,
        ))

        assert item is not None
        assert Path(item.path).read_bytes() == png
        assert item.content_type == "image/png"
        assert item.kind == "image"
        # 扩展名要对齐嗅探出的真实类型（否则按扩展名判类型的工具会报错）
        assert Path(item.path).suffix == ".png"
        assert item.rel_path == str(Path("uploads") / item.filename)

    def test_download_media_requires_key_for_non_image(self, tmp_path, monkeypatch):
        """非图片必须能解密，没 key 就没法用（官方插件同样放弃）。"""
        import core.media_fetch as mf
        from interfaces.wechat.config import WechatConfig
        from interfaces.wechat.media import download_media
        from interfaces.wechat.protocol import MediaRef

        monkeypatch.setattr(mf.socket, "getaddrinfo", lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        ])
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x"))
        )
        item = asyncio.run(download_media(
            MediaRef(kind="file", full_url="https://cdn.example/a", file_name="a.pdf"),
            cfg=WechatConfig(),
            client=client,
            uploads_dir=tmp_path / "uploads",
            workspace_dir=tmp_path,
        ))
        assert item is None


# ─── 编号 → label 回映射 ──────────────────────────────────────────────


class TestResolveAnswer:
    """ask_user 的编号回映射，必须与 ``sink.render_questions`` 的编号一致。"""

    class _Session:
        def __init__(self, questions):
            self.messages = [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu1", "name": "ask_user",
                     "input": {"questions": questions}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu1",
                     "content": "Waiting for user input...",
                     "_meta": {"completed": False, "tool_name": "ask_user"}},
                ]},
            ]

    class _Runner:
        def __init__(self, questions):
            self.session = TestResolveAnswer._Session(questions)

    def _resolve(self, questions, content):
        from interfaces.wechat.inbound import _resolve_answer
        return _resolve_answer(self._Runner(questions), content)

    def test_single_question_maps_number_to_label(self):
        qs = [{"question": "选?", "options": [
            {"label": "允许", "description": ""}, {"label": "拒绝", "description": ""},
        ]}]
        assert self._resolve(qs, "1") == "允许"
        assert self._resolve(qs, "2") == "拒绝"

    def test_out_of_range_number_passes_through(self):
        qs = [{"question": "选?", "options": [{"label": "A", "description": ""}]}]
        assert self._resolve(qs, "9") == "9"

    def test_multi_question_does_not_guess(self):
        """两个问题时 "1" 是歧义的 → 原样透传，不猜。"""
        qs = [
            {"question": "甲?", "options": [{"label": "甲1", "description": ""}]},
            {"question": "乙?", "options": [{"label": "乙1", "description": ""}]},
        ]
        assert self._resolve(qs, "1") == "1"

    def test_non_numeric_passes_through(self):
        qs = [{"question": "选?", "options": [{"label": "A", "description": ""}]}]
        assert self._resolve(qs, "我自己输入") == "我自己输入"

    def test_no_pending_ask_user_passes_through(self):
        class _Empty:
            class session:  # noqa: N801
                messages = []
        from interfaces.wechat.inbound import _resolve_answer
        assert _resolve_answer(_Empty(), "1") == "1"
