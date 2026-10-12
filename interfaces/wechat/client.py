"""iLink HTTP 客户端：把 ``protocol`` 造出的结构发出去。

**职责边界**：这里只做传输与重试（HTTP 层）。协议知识全在 ``protocol.py``，
业务决策（配额、defer 队列、长轮询退避）在 ``outbound.py`` / ``poller.py``。

**为什么不用系统代理**：iLink 是境内服务（``ilinkai.weixin.qq.com``）。cili 用户的
环境变量里常有指向境外 LLM 端点的代理，走它反而连不上——所以 ``trust_env=False``
直连。要换端点改 ``cfg.base_url`` 即可。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from interfaces.wechat.config import WechatConfig
from interfaces.wechat.protocol import (
    WechatApiError,
    base_info,
    make_headers,
    raise_for_api_error,
)

logger = logging.getLogger(__name__)

DEFAULT_API_TIMEOUT_S = 15.0
DEFAULT_CONFIG_TIMEOUT_S = 10.0
# getupdates 是长轮询：服务端最多 hold 35s，客户端超时要更宽
DEFAULT_POLL_TIMEOUT_S = 45.0
QR_POLL_TIMEOUT_S = 60.0

# 可重试的 HTTP 状态（业务错误码另算，见 protocol.raise_for_api_error）
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429})
RETRY_DELAY_S = 2.0
MAX_ATTEMPTS = 2          # 首次 + 1 次重试


class WechatClient:
    """iLink 的 HTTP 客户端（一个宿主 loop 一个实例）。"""

    def __init__(
        self,
        cfg: WechatConfig,
        *,
        token: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._cfg = cfg
        self._token = token
        # 注入的 client 归调用方所有（测试用 MockTransport），自己建的要自己关
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_API_TIMEOUT_S, connect=30.0),
            follow_redirects=True,
            trust_env=False,
        )

    # ── 生命周期 ────────────────────────────────────────────────────

    @property
    def token(self) -> str:
        return self._token

    def set_token(self, token: str) -> None:
        self._token = token

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    # ── 传输核心 ────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
        base_url: str = "",
        timeout: float | None = None,
        include_base_info: bool = True,
    ) -> dict[str, Any]:
        """发一次请求并解析 JSON。

        - HTTP 层可重试的（408/425/429、5xx、超时、传输错误）→ 重试 ``MAX_ATTEMPTS`` 次
        - 业务层（``ret``/``errcode`` 非 0）→ 交给 ``raise_for_api_error`` 抛类型化异常，
          **不重试**（配额/登录失效重试也没用，得由上层决定怎么办）
        """
        url = f"{(base_url or self._cfg.base_url).rstrip('/')}/{endpoint}"
        headers = make_headers(self._token if auth else "", version=self._cfg.client_version)

        payload: dict[str, Any] | None = None
        if method == "POST":
            payload = dict(body or {})
            if include_base_info and "base_info" not in payload:
                payload["base_info"] = base_info(self._cfg.client_version)

        last_error: WechatApiError | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    json=payload,
                    headers=headers,
                    timeout=timeout or DEFAULT_API_TIMEOUT_S,
                )
                if not _is_retryable_status(response.status_code):
                    response.raise_for_status()      # 4xx（非可重试）→ 直接抛
                    return _parse(endpoint, response)
                last_error = WechatApiError(
                    endpoint, errmsg=f"HTTP {response.status_code}", retryable=True,
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = WechatApiError(
                    endpoint, errmsg=f"网络错误：{type(e).__name__}", retryable=True,
                )
            except httpx.HTTPStatusError as e:
                raise WechatApiError(
                    endpoint,
                    errmsg=f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                ) from e

            if attempt + 1 < MAX_ATTEMPTS:
                logger.warning(
                    f"[wechat/client] {endpoint} 第 {attempt + 1} 次失败"
                    f"（{last_error}），{RETRY_DELAY_S}s 后重试"
                )
                await asyncio.sleep(RETRY_DELAY_S)

        assert last_error is not None
        raise last_error

    # ── 扫码登录 ────────────────────────────────────────────────────

    async def fetch_qr_code(self) -> tuple[str, str]:
        """取登录二维码，返回 ``(qrcode_id, qr_content)``。

        请求体里的 ``local_token_list`` 恒为空：协议允许把本地旧 token 交给服务端，
        让它认出「这个号已经绑过」并回 ``binded_redirect`` 而不是发新码。但 cili 只在
        「确实没有可用凭证」时才来取码——已经有凭证的情况在 ``login.py`` 就被短路了。
        空列表反而是诚实的：我们确实没有想复用的凭证。
        """
        data = await self._request(
            "POST", "ilink/bot/get_bot_qrcode",
            params={"bot_type": 3},
            body={"local_token_list": []},
            auth=False,
            include_base_info=False,
        )

        qrcode_id = str(data.get("qrcode", "") or "")
        if not qrcode_id:
            raise WechatApiError("get_bot_qrcode", errmsg="响应里没有 qrcode")
        content = str(data.get("qrcode_img_content", "") or "") or qrcode_id
        return qrcode_id, content

    async def poll_qr_status(
        self,
        qrcode_id: str,
        *,
        verify_code: str = "",
        base_url: str = "",
    ) -> dict[str, Any]:
        """查扫码状态。``base_url`` 用于 ``scaned_but_redirect`` 之后的换机房轮询。"""
        params: dict[str, Any] = {"qrcode": qrcode_id}
        if verify_code:
            params["verify_code"] = verify_code
        return await self._request(
            "GET", "ilink/bot/get_qrcode_status",
            params=params,
            auth=False,
            base_url=base_url,
            timeout=QR_POLL_TIMEOUT_S,
        )

    # ── 收发消息 ────────────────────────────────────────────────────

    async def get_updates(self, cursor: str) -> dict[str, Any]:
        """长轮询收消息。``cursor`` 是上一轮返回的 ``get_updates_buf``（首轮空串）。"""
        return await self._request(
            "POST", "ilink/bot/getupdates",
            body={"get_updates_buf": cursor},
            timeout=DEFAULT_POLL_TIMEOUT_S,
        )

    async def send_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        """发一条消息（``msg`` 由 ``protocol.build_message`` 等构造）。"""
        return await self._request("POST", "ilink/bot/sendmessage", body={"msg": msg})

    # ── 媒体上传（见 media.py） ─────────────────────────────────────

    async def get_upload_url(self, body: dict[str, Any]) -> dict[str, Any]:
        """取 CDN 预签名上传地址（``upload_full_url`` 或 ``upload_param``）。"""
        return await self._request("POST", "ilink/bot/getuploadurl", body=body)

    def stream(self, method: str, url: str, **kwargs: Any):
        """透传底层 httpx 的流式请求。

        媒体下载（``core/media_fetch.download_to_uploads``）需要边下边解密，所以它要
        的是「能 ``stream`` 的对象」。与其把 httpx 实例暴露出去，只开这一个口。
        """
        return self._client.stream(method, url, **kwargs)

    async def upload_encrypted(self, url: str, data: bytes) -> str:
        """把密文 POST 到 CDN，返回响应头 ``x-encrypted-param``（即下载参数）。

        这个请求不打 iLink 主域，所以不走 ``_request``（没有 base_info、没有重试语义）。
        """
        response = await self._client.post(
            url,
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        response.raise_for_status()
        param = str(response.headers.get("x-encrypted-param", "") or "")
        if not param:
            raise WechatApiError("cdn_upload", errmsg="CDN 响应缺少 x-encrypted-param")
        return param

    # ── 会话辅助 ────────────────────────────────────────────────────

    async def get_config(self, user_id: str, context_token: str = "") -> dict[str, Any]:
        """取 typing 票据 / 刷新 context_token（同一个接口，看响应里回哪个字段）。"""
        return await self._request(
            "POST", "ilink/bot/getconfig",
            body={"ilink_user_id": user_id, "context_token": context_token or None},
            timeout=DEFAULT_CONFIG_TIMEOUT_S,
        )

    async def send_typing(self, user_id: str, typing_ticket: str, status: int) -> dict[str, Any]:
        """输入状态：``status`` 1 = 正在输入，2 = 取消。"""
        return await self._request(
            "POST", "ilink/bot/sendtyping",
            body={
                "ilink_user_id": user_id,
                "typing_ticket": typing_ticket,
                "status": status,
            },
            timeout=DEFAULT_CONFIG_TIMEOUT_S,
        )

    async def notify(self, action: str) -> dict[str, Any]:
        """上下线通知（best-effort，失败只记日志，不影响主流程）。"""
        if action not in ("start", "stop"):
            raise ValueError(f"未知的 notify 动作：{action}")
        return await self._request(
            "POST", f"ilink/bot/msg/notify{action}",
            body={},
            timeout=DEFAULT_CONFIG_TIMEOUT_S,
        )


# ─── 模块级辅助 ───────────────────────────────────────────────────────


def _is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_HTTP_STATUS or status_code >= 500


def _parse(endpoint: str, response: httpx.Response) -> dict[str, Any]:
    """解析响应体并做业务错误分类。"""
    try:
        data = response.json()
    except ValueError as e:
        raise WechatApiError(endpoint, errmsg="服务端返回的不是合法 JSON") from e
    if not isinstance(data, dict):
        raise WechatApiError(endpoint, errmsg="服务端返回的不是 JSON 对象")
    raise_for_api_error(endpoint, data)
    return data
