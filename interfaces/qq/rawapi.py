"""SDK 缺口的适配层：``stream_messages``（流式消息）裸调用。

``qqbot-agent-sdk`` 与 ``qq-botpy`` **都没有封装流式消息**（全仓检索无命中），
而它是 QQ 单聊体验的关键——被动回复每条用户消息只有 4 次配额，长回答用普通
消息根本发不完；整条流只占 1 次配额。

好在 SDK 有裸出口：``QQApiClient.request(Route, body)``。本模块就是那约 50 行
薄封装。``Route`` 未从包 ``__init__`` 导出，需从 ``api_client`` 直接 import。

接口（官方文档 /develop/api-v2/.../stream_messages）：
- ``input_mode=replace``：``content_raw`` 为当前**全量正文**，且**须以上游已下发
  前缀开头**（否则报 40007）——所以已下发内容不可撤回，只能追加
- ``input_state``：``1``=生成中，``10``=生成结束
- ``index``：分片序号，从 0 递增
- ``stream_msg_id``：首条由服务端生成并返回，后续分片携带
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

STREAM_PATH_C2C = "/v2/users/{user_id}/stream_messages"

INPUT_STATE_GENERATING = 1
INPUT_STATE_END = 10

# 官方错误码
ERR_PREFIX_IMMUTABLE = "40007"   # 流式消息前缀不可修改
ERR_SERVER = "50001"
ERR_RATE_LIMITED = "50002"


class StreamError(RuntimeError):
    """流式消息调用失败。"""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class PrefixNotExtendableError(StreamError):
    """40007：新内容不是已下发内容的前缀扩展。

    调用方应据此收尾当前流并另开一条，而不是重试同样的内容。
    """


def _classify(exc: Exception) -> StreamError:
    """把 SDK 抛的 RuntimeError 归类（SDK 只给文本，没有结构化错误码）。"""
    msg = str(exc)
    if ERR_PREFIX_IMMUTABLE in msg:
        return PrefixNotExtendableError(msg, code=ERR_PREFIX_IMMUTABLE)
    for code in (ERR_SERVER, ERR_RATE_LIMITED):
        if code in msg:
            return StreamError(msg, code=code)
    return StreamError(msg)


class StreamSession:
    """一条 QQ 流式消息（一个 ``stream_msg_id``）的会话。"""

    def __init__(
        self,
        api_client: Any,
        openid: str,
        msg_id: str,
        *,
        content_type: str = "markdown",
        msg_seq: int,
    ) -> None:
        self._api = api_client
        self._openid = openid
        self._msg_id = msg_id
        self._content_type = content_type
        self._msg_seq = msg_seq
        self._stream_msg_id: str | None = None
        self._index = 0
        self.last_sent_text = ""
        self.remain_msg_len: int | None = None

    @property
    def stream_msg_id(self) -> str | None:
        return self._stream_msg_id

    @property
    def is_open(self) -> bool:
        return self._stream_msg_id is not None

    async def send(self, text: str, *, final: bool = False) -> None:
        """推一片。``text`` 必须是**累积全文**，且以已下发内容为前缀。"""
        from qqbot_agent_sdk.api_client import Route  # 未从包 __init__ 导出

        payload: dict[str, Any] = {
            "input_mode": "replace",
            "input_state": INPUT_STATE_END if final else INPUT_STATE_GENERATING,
            "index": self._index,
            "content_type": self._content_type,
            "content_raw": text,
            "msg_id": self._msg_id,
            "msg_seq": self._msg_seq,
        }
        if self._stream_msg_id:
            payload["stream_msg_id"] = self._stream_msg_id

        route = Route("POST", STREAM_PATH_C2C, user_id=self._openid)
        try:
            data = await self._api.request(route, payload)
        except Exception as e:  # noqa: BLE001 — 归类后重抛，调用方按类型分流
            raise _classify(e) from e

        if not self._stream_msg_id:
            # 首片返回 stream_msg_id，后续分片必须携带
            self._stream_msg_id = data.get("id") or None
            if not self._stream_msg_id:
                raise StreamError(f"流式首片未返回 stream_msg_id: {data}")

        self.last_sent_text = text
        self._index += 1
        remain = data.get("remain_msg_len")
        if isinstance(remain, int):
            self.remain_msg_len = remain
