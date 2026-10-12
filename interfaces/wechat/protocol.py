"""iLink 协议的纯函数层：请求头、消息编解码、错误分类、媒体加解密。

**为什么单独一层**：iLink 是**未公开**协议，字段名与错误码随时可能变。把所有
"字节与结构"的知识集中在这里（无 IO、无状态、可直接单测），将来协议变动只需改
这一个文件；``client.py`` 只负责把这里造出的结构发出去。

协议对标官方 npm 插件 ``@tencent-weixin/openclaw-weixin`` v2.4.6。
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ─── 协议常量 ─────────────────────────────────────────────────────────

DEFAULT_CHANNEL_VERSION = "2.4.6"
ILINK_APP_ID = "bot"
BOT_AGENT = "cili (python)"

# item_list 的元素类型
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

# message_type：1 = 用户发来，2 = bot 发出
MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2

# message_state：2 = 完整消息（发送时用）
MESSAGE_STATE_FINISH = 2

# getuploadurl 的 media_type
UPLOAD_MEDIA_IMAGE = 1
UPLOAD_MEDIA_VIDEO = 2
UPLOAD_MEDIA_FILE = 3
UPLOAD_MEDIA_VOICE = 4

# 业务错误码（ret 与 errcode 任一命中即算）
ERRCODE_CONTEXT_RESTRICTED = -2   # context 受限 / 配额用尽 → defer，不重试
ERRCODE_INVALID_ARGUMENT = -3     # 参数非法
ERRCODE_STALE_TOKEN = -14         # bot token 失效 → 需重新扫码

# 群聊成员的 id 以该后缀结尾（本次范围仅私聊，见到即忽略）
GROUP_ID_SUFFIX = "@chatroom"

_MEDIA_ITEM_KINDS = {
    ITEM_IMAGE: "image",
    ITEM_VOICE: "voice",
    ITEM_FILE: "file",
    ITEM_VIDEO: "video",
}
_MEDIA_ITEM_KEYS = {
    ITEM_IMAGE: "image_item",
    ITEM_VOICE: "voice_item",
    ITEM_FILE: "file_item",
    ITEM_VIDEO: "video_item",
}

_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")


# ─── 错误分类 ─────────────────────────────────────────────────────────


class WechatApiError(RuntimeError):
    """iLink 返回了非 0 的 ret/errcode。"""

    def __init__(
        self,
        endpoint: str,
        *,
        ret: int = 0,
        errcode: int = 0,
        errmsg: str = "",
        retryable: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.ret = ret
        self.errcode = errcode
        self.errmsg = errmsg
        self.retryable = retryable
        code = errcode or ret
        super().__init__(
            f"iLink {endpoint} 失败 (code={code}, ret={ret}, errcode={errcode}): "
            f"{errmsg or '无错误信息'}"
        )


class WechatQuotaError(WechatApiError):
    """当前 context_token 发不出去了（配额用尽/受限）——**不可重试**，走 defer。"""


class WechatAuthError(WechatApiError):
    """bot token 已失效，需要重新扫码登录。"""


def as_int(value: Any) -> int:
    """把 ret/errcode 宽松转成 int（服务端可能给字符串或 bool）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


def raise_for_api_error(endpoint: str, data: dict[str, Any]) -> None:
    """``ret``/``errcode`` 任一非 0 就抛对应异常；都为 0 则什么都不做。"""
    ret = as_int(data.get("ret"))
    errcode = as_int(data.get("errcode"))
    if ret == 0 and errcode == 0:
        return
    errmsg = str(data.get("errmsg", "") or "")
    if ERRCODE_CONTEXT_RESTRICTED in (ret, errcode):
        raise WechatQuotaError(
            endpoint, ret=ret, errcode=errcode,
            errmsg=errmsg or "context 受限或配额用尽",
        )
    if ERRCODE_STALE_TOKEN in (ret, errcode):
        raise WechatAuthError(
            endpoint, ret=ret, errcode=errcode,
            errmsg=errmsg or "bot token 已失效，请重新扫码",
        )
    raise WechatApiError(endpoint, ret=ret, errcode=errcode, errmsg=errmsg)


# ─── 请求头与自报家门 ─────────────────────────────────────────────────


def app_client_version(version: str) -> int:
    """语义版本 → uint32（``0x00MMNNPP``），官方插件的 ``buildClientVersion``。

    ``"2.4.6"`` → ``0x020406`` = 132102。非数字段按 0 处理（协议要的是不报错）。
    """
    parts = str(version or "").split(".")

    def _part(idx: int) -> int:
        try:
            return int(parts[idx])
        except (IndexError, ValueError):
            return 0

    return ((_part(0) & 0xFF) << 16) | ((_part(1) & 0xFF) << 8) | (_part(2) & 0xFF)


def random_wechat_uin() -> str:
    """``X-WECHAT-UIN``：随机 uint32 → 十进制字符串 → base64。

    官方实现**每次请求都重新生成**，这里照做（不是会话级常量）。
    """
    return base64.b64encode(str(int.from_bytes(os.urandom(4), "big")).encode()).decode()


def base_info(
    version: str = DEFAULT_CHANNEL_VERSION,
    agent: str = BOT_AGENT,
) -> dict[str, str]:
    """POST 请求体里要带的自报家门。"""
    return {"channel_version": version, "bot_agent": agent}


def make_headers(token: str = "", *, version: str = DEFAULT_CHANNEL_VERSION) -> dict[str, str]:
    """构造一次请求的全部 header（``X-WECHAT-UIN`` 每次重新生成）。

    登录用的两个接口（取二维码 / 查扫码状态）不带 ``Authorization``，传空 token 即可。
    """
    headers = {
        "X-WECHAT-UIN": random_wechat_uin(),
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(app_client_version(version)),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ─── 出站消息 ─────────────────────────────────────────────────────────


def build_message(
    *,
    to_user_id: str,
    item_list: list[dict[str, Any]],
    context_token: str,
    client_id: str,
) -> dict[str, Any]:
    """构造一条出站消息（``sendmessage`` 请求体里的 ``msg``）。

    ``client_id`` 由调用方生成并**在重试/重放时复用**——服务端据此幂等去重，所以同一条
    消息重发不能换 id（见 ``outbound.py``）。
    """
    msg: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": client_id,
        "message_type": MESSAGE_TYPE_BOT,
        "message_state": MESSAGE_STATE_FINISH,
        "item_list": item_list,
    }
    if context_token:
        msg["context_token"] = context_token
    return msg


def build_text_message(
    *,
    to_user_id: str,
    text: str,
    context_token: str,
    client_id: str,
) -> dict[str, Any]:
    """构造一条出站文本消息。"""
    return build_message(
        to_user_id=to_user_id,
        item_list=[{"type": ITEM_TEXT, "text_item": {"text": text}}],
        context_token=context_token,
        client_id=client_id,
    )


# ─── 入站消息 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MediaRef:
    """一条待下载的入站媒体（定位信息 + 密钥）。下载与解密见 ``media.py``。"""

    kind: str                    # "image" | "voice" | "file" | "video"
    full_url: str = ""
    encrypt_query_param: str = ""
    aes_key: str = ""            # 原始串（hex 或 base64），解析见 parse_aes_key
    file_name: str = ""
    voice_text: str = ""         # 微信自带的语音转写（可能为空）


@dataclass(frozen=True)
class InboundMessage:
    """一条解析后的入站消息。"""

    message_id: str
    from_user_id: str
    context_token: str = ""
    text: str = ""
    media: tuple[MediaRef, ...] = ()
    from_bot: bool = False
    is_group: bool = False


def message_id_of(msg: dict[str, Any]) -> str:
    """取消息 id；服务端没给就用 ``from_user_id + create_time_ms`` 兜底（去重用）。"""
    mid = str(msg.get("message_id", "") or msg.get("seq", "") or "")
    if mid:
        return mid
    return f"{msg.get('from_user_id', '')}_{msg.get('create_time_ms', '')}"


def _text_of(item: dict[str, Any]) -> str:
    """取文本项内容；引用消息展开成 ``[引用: ...]`` 前缀。"""
    text = str((item.get("text_item") or {}).get("text", "") or "")
    if not text:
        return ""
    ref = item.get("ref_msg") or {}
    ref_item = ref.get("message_item") or {}
    if not ref_item:
        return text
    if as_int(ref_item.get("type")) in _MEDIA_ITEM_KINDS:
        # 引用的是媒体 → 引用信息没有文本价值，只留正文
        return text
    parts: list[str] = []
    title = str(ref.get("title", "") or "")
    if title:
        parts.append(title)
    ref_text = str((ref_item.get("text_item") or {}).get("text", "") or "")
    if ref_text:
        parts.append(ref_text)
    if not parts:
        return text
    return f"[引用: {' | '.join(parts)}]\n{text}"


def _media_ref(item: dict[str, Any], kind: str) -> MediaRef:
    """从媒体项里抽定位信息与密钥。

    图片的 aeskey 在 item 顶层（hex），其余在 ``media.aes_key``（base64）。
    """
    typed = item.get(_MEDIA_ITEM_KEYS.get(as_int(item.get("type")), "")) or {}
    media = typed.get("media") or {}
    return MediaRef(
        kind=kind,
        full_url=str(media.get("full_url", "") or ""),
        encrypt_query_param=str(media.get("encrypt_query_param", "") or ""),
        aes_key=str(typed.get("aeskey", "") or media.get("aes_key", "") or ""),
        file_name=str(typed.get("file_name", "") or ""),
        voice_text=str(typed.get("text", "") or ""),
    )


def parse_inbound(msg: dict[str, Any]) -> InboundMessage:
    """把 ``getupdates`` 返回的一条 ``msgs[]`` 元素解析成 ``InboundMessage``。"""
    from_user_id = str(msg.get("from_user_id", "") or "")
    texts: list[str] = []
    media: list[MediaRef] = []
    for item in msg.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        item_type = as_int(item.get("type"))
        if item_type == ITEM_TEXT:
            text = _text_of(item)
            if text:
                texts.append(text)
            continue
        kind = _MEDIA_ITEM_KINDS.get(item_type)
        if kind:
            media.append(_media_ref(item, kind))
    return InboundMessage(
        message_id=message_id_of(msg),
        from_user_id=from_user_id,
        context_token=str(msg.get("context_token", "") or ""),
        text="\n".join(texts),
        media=tuple(media),
        from_bot=as_int(msg.get("message_type")) == MESSAGE_TYPE_BOT,
        is_group=from_user_id.endswith(GROUP_ID_SUFFIX),
    )


# ─── 媒体加解密（AES-128-ECB + PKCS7） ────────────────────────────────


def pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    """PKCS7 填充（长度正好是整块时也要补一整块）。"""
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


def pkcs7_padded_size(size: int, block: int = 16) -> int:
    """填充后的字节数（``getuploadurl`` 要报这个值）。"""
    return size + (block - (size % block))


def pkcs7_unpad(data: bytes, block: int = 16) -> bytes:
    """去 PKCS7 填充。填充非法时**原样返回**。

    服务端偶有不合规实现，为它抛异常会让整条消息丢掉——图片损坏好过没有。
    """
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > block or pad > len(data):
        return data
    if data[-pad:] != bytes([pad]) * pad:
        return data
    return data[:-pad]


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB + PKCS7（iLink 媒体上传用的就是这套）。"""
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(pkcs7_pad(data)) + encryptor.finalize()


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密并去 PKCS7。"""
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return pkcs7_unpad(decryptor.update(data) + decryptor.finalize())


class EcbStreamDecryptor:
    """流式 AES-128-ECB 解密，给 ``core/media_fetch.download_to_uploads`` 的
    ``transform(chunk, is_last)`` 钩子用——边下载边解密，不落密文中间文件。

    ``cryptography`` 的 ``update()`` 自己会缓存不满一块的尾巴，所以这里只需在最后
    一块上 ``finalize()`` 并去填充。
    """

    def __init__(self, key: bytes) -> None:
        self._decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        self._finished = False

    def __call__(self, chunk: bytes, is_last: bool) -> bytes:
        if self._finished:
            return b""
        if is_last:
            self._finished = True
            return pkcs7_unpad(self._decryptor.update(chunk) + self._decryptor.finalize())
        return self._decryptor.update(chunk)


def parse_aes_key(raw: str) -> bytes | None:
    """把消息里的 aeskey 解析成 16 字节原始 key；无法解析返回 None。

    见过两种形态：
    - 图片项顶层 ``aeskey``：32 字符 hex
    - ``media.aes_key``：base64——解出来可能是 raw16，也可能是 32 字符 hex 的 ASCII
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if _HEX32_RE.match(text):
        return bytes.fromhex(text)
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(decoded) == 16:
        return decoded
    try:
        as_text = decoded.decode("ascii")
    except UnicodeDecodeError:
        return None
    if _HEX32_RE.match(as_text):
        return bytes.fromhex(as_text)
    return None
