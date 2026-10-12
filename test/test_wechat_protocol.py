"""微信 iLink 协议层（纯函数）单测。

这一层不碰网络——字段名、错误码、加解密都是协议知识，用纯函数测最省事也最稳。
``client.py`` 的端点测试用 ``httpx.MockTransport``，见本文件末尾。
"""

from __future__ import annotations

import base64

# ─── 版本编码与请求头 ─────────────────────────────────────────────────


class TestClientVersion:
    def test_semver_to_uint32(self):
        from interfaces.wechat.protocol import app_client_version
        # (2<<16)|(4<<8)|6
        assert app_client_version("2.4.6") == 0x020406 == 132102

    def test_masks_each_component_to_one_byte(self):
        from interfaces.wechat.protocol import app_client_version
        assert app_client_version("1.0.0") == 0x010000
        assert app_client_version("256.0.0") == 0  # 256 & 0xFF == 0

    def test_tolerates_junk(self):
        from interfaces.wechat.protocol import app_client_version
        assert app_client_version("") == 0
        assert app_client_version("abc") == 0
        assert app_client_version("2") == 0x020000
        assert app_client_version("2.4") == 0x020400
        assert app_client_version("2.x.6") == 0x020006


class TestHeaders:
    def test_required_headers(self):
        from interfaces.wechat.protocol import make_headers
        h = make_headers("tok")
        assert h["AuthorizationType"] == "ilink_bot_token"
        assert h["iLink-App-Id"] == "bot"
        assert h["iLink-App-ClientVersion"] == "132102"
        assert h["Content-Type"] == "application/json"
        assert h["Authorization"] == "Bearer tok"
        assert "X-WECHAT-UIN" in h

    def test_uin_is_regenerated_every_request(self):
        from interfaces.wechat.protocol import make_headers
        uins = {make_headers("tok")["X-WECHAT-UIN"] for _ in range(50)}
        assert len(uins) > 1

    def test_uin_is_base64_of_decimal(self):
        from interfaces.wechat.protocol import make_headers
        uin = make_headers("tok")["X-WECHAT-UIN"]
        decoded = base64.b64decode(uin).decode()
        assert decoded.isdigit()
        assert 0 <= int(decoded) <= 0xFFFFFFFF

    def test_no_authorization_without_token(self):
        """登录用的两个接口不带 Authorization。"""
        from interfaces.wechat.protocol import make_headers
        assert "Authorization" not in make_headers("")

    def test_base_info_shape(self):
        from interfaces.wechat.protocol import base_info
        info = base_info()
        assert info["channel_version"] == "2.4.6"
        assert "cili" in info["bot_agent"]


# ─── 错误分类 ─────────────────────────────────────────────────────────


class TestErrorClassification:
    def test_zero_means_ok(self):
        from interfaces.wechat.protocol import raise_for_api_error
        raise_for_api_error("getupdates", {"ret": 0, "errcode": 0})
        raise_for_api_error("getupdates", {})

    def test_quota_error(self):
        from interfaces.wechat.protocol import WechatQuotaError, raise_for_api_error
        for payload in ({"ret": -2}, {"errcode": -2}, {"ret": 0, "errcode": -2}):
            try:
                raise_for_api_error("sendmessage", payload)
            except WechatQuotaError as e:
                assert e.ret == payload.get("ret", 0)
            else:
                raise AssertionError(f"应当抛配额异常：{payload}")

    def test_auth_error(self):
        from interfaces.wechat.protocol import WechatAuthError, raise_for_api_error
        try:
            raise_for_api_error("getupdates", {"ret": -14})
        except WechatAuthError:
            pass
        else:
            raise AssertionError("应当抛登录失效异常")

    def test_generic_error_carries_codes(self):
        from interfaces.wechat.protocol import WechatApiError, raise_for_api_error
        try:
            raise_for_api_error("sendmessage", {"ret": -3, "errmsg": "bad arg"})
        except WechatApiError as e:
            assert e.ret == -3
            assert e.errmsg == "bad arg"
            assert "bad arg" in str(e)
        else:
            raise AssertionError("应当抛通用异常")

    def test_string_and_bool_codes_are_coerced(self):
        from interfaces.wechat.protocol import WechatQuotaError, raise_for_api_error
        try:
            raise_for_api_error("sendmessage", {"errcode": "-2"})
        except WechatQuotaError:
            pass
        else:
            raise AssertionError("字符串 -2 也应识别为配额错误")


# ─── 出站消息 ─────────────────────────────────────────────────────────


class TestBuildTextMessage:
    def test_shape(self):
        from interfaces.wechat.protocol import build_text_message
        msg = build_text_message(
            to_user_id="u1", text="hi", context_token="ctx", client_id="cid",
        )
        assert msg["from_user_id"] == ""
        assert msg["to_user_id"] == "u1"
        assert msg["client_id"] == "cid"
        assert msg["message_type"] == 2          # BOT
        assert msg["message_state"] == 2         # FINISH
        assert msg["context_token"] == "ctx"
        assert msg["item_list"] == [{"type": 1, "text_item": {"text": "hi"}}]


# ─── 入站消息解析 ─────────────────────────────────────────────────────


def _text_item(text: str) -> dict:
    return {"type": 1, "text_item": {"text": text}}


class TestParseInbound:
    def test_plain_text(self):
        from interfaces.wechat.protocol import parse_inbound
        msg = parse_inbound({
            "message_id": "m1",
            "from_user_id": "u1",
            "context_token": "ctx",
            "message_type": 1,
            "item_list": [_text_item("你好")],
        })
        assert msg.message_id == "m1"
        assert msg.from_user_id == "u1"
        assert msg.context_token == "ctx"
        assert msg.text == "你好"
        assert msg.media == ()
        assert msg.from_bot is False
        assert msg.is_group is False

    def test_message_id_fallback(self):
        from interfaces.wechat.protocol import parse_inbound
        msg = parse_inbound({"from_user_id": "u1", "create_time_ms": 123, "item_list": []})
        assert msg.message_id == "u1_123"

    def test_bot_and_group_flags(self):
        from interfaces.wechat.protocol import parse_inbound
        assert parse_inbound({"message_type": 2, "item_list": []}).from_bot is True
        assert parse_inbound(
            {"from_user_id": "12345@chatroom", "item_list": []}
        ).is_group is True

    def test_multiple_text_items_joined(self):
        from interfaces.wechat.protocol import parse_inbound
        msg = parse_inbound({"item_list": [_text_item("a"), _text_item("b")]})
        assert msg.text == "a\nb"

    def test_quoted_text_expanded(self):
        from interfaces.wechat.protocol import parse_inbound
        item = _text_item("好的")
        item["ref_msg"] = {"title": "某人", "message_item": _text_item("原文")}
        msg = parse_inbound({"item_list": [item]})
        assert msg.text == "[引用: 某人 | 原文]\n好的"

    def test_quoted_media_keeps_plain_text(self):
        """引用的是媒体 → 引用信息没有文本价值，只留正文。"""
        from interfaces.wechat.protocol import parse_inbound
        item = _text_item("这张图不错")
        item["ref_msg"] = {"message_item": {"type": 2, "image_item": {}}}
        msg = parse_inbound({"item_list": [item]})
        assert msg.text == "这张图不错"

    def test_image_uses_item_level_hex_aeskey(self):
        from interfaces.wechat.protocol import parse_inbound
        item = {
            "type": 2,
            "image_item": {
                "aeskey": "00112233445566778899aabbccddeeff",
                "media": {
                    "full_url": "https://cdn/x",
                    "encrypt_query_param": "q=1",
                    "aes_key": "should-be-ignored",
                },
            },
        }
        ref = parse_inbound({"item_list": [item]}).media[0]
        assert ref.kind == "image"
        assert ref.aes_key == "00112233445566778899aabbccddeeff"
        assert ref.full_url == "https://cdn/x"
        assert ref.encrypt_query_param == "q=1"

    def test_file_uses_media_base64_aeskey(self):
        from interfaces.wechat.protocol import parse_inbound
        item = {
            "type": 4,
            "file_item": {
                "file_name": "报告.pdf",
                "len": "1234",
                "media": {"encrypt_query_param": "q=2", "aes_key": "QUJD"},
            },
        }
        ref = parse_inbound({"item_list": [item]}).media[0]
        assert ref.kind == "file"
        assert ref.file_name == "报告.pdf"
        assert ref.aes_key == "QUJD"
        assert ref.encrypt_query_param == "q=2"

    def test_voice_carries_wechat_transcript(self):
        from interfaces.wechat.protocol import parse_inbound
        item = {"type": 3, "voice_item": {"text": "帮我看看这个", "media": {}}}
        ref = parse_inbound({"item_list": [item]}).media[0]
        assert ref.kind == "voice"
        assert ref.voice_text == "帮我看看这个"

    def test_unknown_item_types_ignored(self):
        from interfaces.wechat.protocol import parse_inbound
        msg = parse_inbound({"item_list": [{"type": 99, "weird": {}}, "not-a-dict"]})
        assert msg.media == ()
        assert msg.text == ""


# ─── 加解密 ───────────────────────────────────────────────────────────


class TestAes:
    KEY = bytes(range(16))

    def test_pkcs7_sizes(self):
        from interfaces.wechat.protocol import pkcs7_pad, pkcs7_padded_size
        assert pkcs7_padded_size(0) == 16
        assert pkcs7_padded_size(1) == 16
        assert pkcs7_padded_size(15) == 16
        assert pkcs7_padded_size(16) == 32   # 整块也要补一整块
        assert pkcs7_padded_size(33) == 48
        assert len(pkcs7_pad(b"x" * 33)) == 48

    def test_roundtrip(self):
        from interfaces.wechat.protocol import aes_ecb_decrypt, aes_ecb_encrypt
        for size in (0, 1, 15, 16, 17, 1000):
            data = bytes(range(256)) * 4
            data = data[:size]
            ct = aes_ecb_encrypt(data, self.KEY)
            assert len(ct) % 16 == 0
            assert aes_ecb_decrypt(ct, self.KEY) == data

    def test_unpad_is_lenient_on_bad_padding(self):
        """填充非法时原样返回，不抛异常（丢数据比报错更糟）。"""
        from interfaces.wechat.protocol import pkcs7_unpad
        assert pkcs7_unpad(b"") == b""
        assert pkcs7_unpad(b"abc") == b"abc"                      # pad 值越界
        assert pkcs7_unpad(b"abcdefghijklmnop") == b"abcdefghijklmnop"
        # pad 值大于数据长度 → 非法
        assert pkcs7_unpad(b"abc" + bytes([9])) == b"abc" + bytes([9])
        # pad 值合法但尾字节凑不出 3 个 0x03 → 非法
        assert pkcs7_unpad(b"abc" + bytes([3])) == b"abc" + bytes([3])
        # 合法填充 → 剥掉
        assert pkcs7_unpad(b"abc" + bytes([3]) * 3) == b"abc"

    def test_stream_decryptor_matches_one_shot(self):
        from interfaces.wechat.protocol import EcbStreamDecryptor, aes_ecb_encrypt
        plain = b"iLink media payload " * 37          # 740 字节
        ct = aes_ecb_encrypt(plain, self.KEY)

        # 模拟 core/media_fetch 的 transform(chunk, is_last) 调用方式：最后一块标 True
        dec = EcbStreamDecryptor(self.KEY)
        out = b""
        step = 7                                    # 故意不是 16 的倍数
        pieces = [ct[i:i + step] for i in range(0, len(ct), step)]
        for i, piece in enumerate(pieces):
            out += dec(piece, i == len(pieces) - 1)
        assert out == plain

    def test_stream_decryptor_ignores_calls_after_finish(self):
        from interfaces.wechat.protocol import EcbStreamDecryptor, aes_ecb_encrypt
        ct = aes_ecb_encrypt(b"short", self.KEY)
        dec = EcbStreamDecryptor(self.KEY)
        assert dec(ct, True) == b"short"
        assert dec(ct, False) == b""

    def test_parse_aes_key_hex(self):
        from interfaces.wechat.protocol import parse_aes_key
        assert parse_aes_key("00112233445566778899AABBCCDDEEFF") == bytes.fromhex(
            "00112233445566778899aabbccddeeff"
        )

    def test_parse_aes_key_base64_raw16(self):
        from interfaces.wechat.protocol import parse_aes_key
        raw = bytes(range(16))
        assert parse_aes_key(base64.b64encode(raw).decode()) == raw

    def test_parse_aes_key_base64_of_hex(self):
        """``media.aes_key`` 可能是「32 字符 hex 串」再 base64 的形态。"""
        from interfaces.wechat.protocol import parse_aes_key
        hexstr = "a" * 32
        assert parse_aes_key(base64.b64encode(hexstr.encode()).decode()) == bytes.fromhex(hexstr)

    def test_parse_aes_key_rejects_garbage(self):
        from interfaces.wechat.protocol import parse_aes_key
        for bad in ("", "   ", "!!!", "not-a-key", base64.b64encode(b"too-short").decode()):
            assert parse_aes_key(bad) is None, bad


# ─── HTTP 客户端（httpx.MockTransport，走真实编解码路径） ──────────────


def _client(handler, *, token: str = "tok", **cfg_kw):
    """造一个挂了 MockTransport 的 WechatClient——不碰真实网络。

    MockTransport 没有真实连接，所以注入的 AsyncClient 不关也无妨。
    """
    import httpx

    from interfaces.wechat.client import WechatClient
    from interfaces.wechat.config import WechatConfig

    return WechatClient(
        WechatConfig(**cfg_kw),
        token=token,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class TestClientEndpoints:
    def test_get_updates_sends_cursor_headers_and_base_info(self):
        import asyncio
        import json

        import httpx

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["version"] = request.headers.get("iLink-App-ClientVersion")
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"ret": 0, "msgs": [], "get_updates_buf": "cur2"})

        data = asyncio.run(_client(handler).get_updates("cur1"))

        assert data["get_updates_buf"] == "cur2"
        assert seen["url"].endswith("/ilink/bot/getupdates")
        assert seen["auth"] == "Bearer tok"
        assert seen["version"] == "132102"
        assert seen["body"]["get_updates_buf"] == "cur1"
        assert seen["body"]["base_info"]["channel_version"] == "2.4.6"

    def test_send_message_wraps_msg(self):
        import asyncio
        import json

        import httpx

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"ret": 0})

        asyncio.run(_client(handler).send_message({"client_id": "c1", "item_list": []}))

        assert seen["body"]["msg"]["client_id"] == "c1"
        assert "base_info" in seen["body"]

    def test_qr_endpoints_have_no_auth_and_no_base_info(self):
        """登录两接口不带 Authorization，且官方实现不塞 base_info。"""
        import asyncio
        import json

        import httpx

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"ret": 0, "qrcode": "q1", "qrcode_img_content": "https://qr"})

        qrcode_id, content = asyncio.run(_client(handler).fetch_qr_code())

        assert (qrcode_id, content) == ("q1", "https://qr")
        assert seen["url"].endswith("/ilink/bot/get_bot_qrcode?bot_type=3")
        assert seen["auth"] is None
        assert seen["body"] == {"local_token_list": []}

    def test_poll_qr_status_honours_redirect_base_url(self):
        import asyncio

        import httpx

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"status": "wait"})

        data = asyncio.run(
            _client(handler).poll_qr_status("q1", base_url="https://other.example.com")
        )

        assert data["status"] == "wait"
        assert seen["url"].startswith("https://other.example.com/ilink/bot/get_qrcode_status")
        assert "qrcode=q1" in seen["url"]

    def test_retries_once_on_server_error(self, monkeypatch):
        import asyncio

        import httpx

        import interfaces.wechat.client as client_mod

        monkeypatch.setattr(client_mod, "RETRY_DELAY_S", 0)
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json={"ret": 0, "msgs": []})

        data = asyncio.run(_client(handler).get_updates(""))

        assert data["ret"] == 0
        assert len(calls) == 2

    def test_does_not_retry_client_error(self, monkeypatch):
        import asyncio

        import httpx

        import interfaces.wechat.client as client_mod
        from interfaces.wechat.protocol import WechatApiError

        monkeypatch.setattr(client_mod, "RETRY_DELAY_S", 0)
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(400, text="bad request")

        try:
            asyncio.run(_client(handler).get_updates(""))
        except WechatApiError as e:
            assert "400" in e.errmsg
        else:
            raise AssertionError("400 应当直接抛错")
        assert len(calls) == 1

    def test_quota_error_is_not_retried(self, monkeypatch):
        """配额错误重试没有意义——必须原样冒泡给上层去 defer。"""
        import asyncio

        import httpx

        import interfaces.wechat.client as client_mod
        from interfaces.wechat.protocol import WechatQuotaError

        monkeypatch.setattr(client_mod, "RETRY_DELAY_S", 0)
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json={"ret": -2, "errmsg": "context restricted"})

        try:
            asyncio.run(_client(handler).send_message({"client_id": "c1"}))
        except WechatQuotaError:
            pass
        else:
            raise AssertionError("应当抛 WechatQuotaError")
        assert len(calls) == 1

    def test_gives_up_after_max_attempts(self, monkeypatch):
        import asyncio

        import httpx

        import interfaces.wechat.client as client_mod
        from interfaces.wechat.protocol import WechatApiError

        monkeypatch.setattr(client_mod, "RETRY_DELAY_S", 0)
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(503, text="unavailable")

        try:
            asyncio.run(_client(handler).get_updates(""))
        except WechatApiError as e:
            assert e.retryable is True
        else:
            raise AssertionError("应当抛可重试标记的异常")
        assert len(calls) == client_mod.MAX_ATTEMPTS

    def test_notify_rejects_unknown_action(self):
        import asyncio

        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("不该发出请求")

        try:
            asyncio.run(_client(handler).notify("bogus"))
        except ValueError:
            pass
        else:
            raise AssertionError("应当抛 ValueError")
