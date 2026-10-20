"""SDK 接线：``WSCallbacks`` 全量回调 + 事件分发。

关键事实：
- ``QQWebSocket`` **自带专用守护线程 + 事件循环**处理网络/心跳/重连，入站回调经
  ``run_coroutine_threadsafe`` 派发到我们传入的 ``main_loop``。所以传输线程归 SDK，
  我们只提供**宿主 loop**。
- ``WSCallbacks`` 有 **11 个必填回调**，缺一个都构造不出来。
- 入站去重（``msg_id`` + 300s 窗口）与重连退避 SDK 已内置，不用自己写。

**API 域名是进程级资源**（设计 §4.1/§4.2）：SDK 的 ``API_BASE`` 在 import 时从
环境变量 ``QQ_API_BASE`` 求值、且被 ``api_client`` 按值导入，一个进程只能有一个
域名。所以本模块在**导入 SDK 之前** setdefault 该环境变量（默认沙箱，未过审
机器人只能沙箱收发；要跑正式环境，在启动 cili 前设置
``QQ_API_BASE=https://api.sgroup.qq.com``）。运行期不再切换域名。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from core.config import DATA_DIR
from interfaces.qq.runtime import QQRuntime

logger = logging.getLogger(__name__)

# 沙箱 / 正式 API 域名（未过审机器人只能沙箱收发）。
# ⚠️ 待实测确认；若官方沙箱域名不同，改这里或用环境变量 QQ_API_BASE 覆盖。
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
PROD_API_BASE = "https://api.sgroup.qq.com"

# 必须在导入 SDK **之前**落定环境变量：SDK 的 constants.py 在 import 时读它。
# setdefault 尊重用户显式设置的值（正式环境部署的常规做法）。
os.environ.setdefault("QQ_API_BASE", SANDBOX_API_BASE)

# 延迟 import：未启用 / 未安装时不影响 cili 启动
SDK_AVAILABLE = False
_IMPORT_ERROR: str = ""
try:  # pragma: no cover - 取决于环境是否装了 SDK
    from qqbot_agent_sdk import (  # type: ignore
        EventParser,
        QQApiClient,
        QQWebSocket,
        WSCallbacks,
    )
    from qqbot_agent_sdk.media_loader import MediaUploader  # type: ignore
    from qqbot_agent_sdk.session_store import WSSessionStore  # type: ignore

    SDK_AVAILABLE = True
except ImportError as e:  # pragma: no cover
    _IMPORT_ERROR = str(e)
    QQApiClient = QQWebSocket = WSCallbacks = EventParser = None  # type: ignore
    MediaUploader = WSSessionStore = None  # type: ignore


def sdk_available() -> bool:
    return SDK_AVAILABLE


def sdk_import_error() -> str:
    return _IMPORT_ERROR


def effective_api_base() -> str:
    """当前 SDK 实际使用的 API 域名（来自 import 时的环境变量）。"""
    return os.environ.get("QQ_API_BASE") or SANDBOX_API_BASE


class QQGateway:
    """QQ WebSocket 网关的生命周期与事件分发。"""

    def __init__(
        self,
        runtime: QQRuntime,
        on_message: Callable[[Any], Awaitable[None]],
        *,
        app_id: str,
    ) -> None:
        self._rt = runtime
        self._on_message = on_message
        self._app_id = app_id
        self._parser = EventParser() if SDK_AVAILABLE else None
        self._session_store = None
        self._heartbeat_interval = 0.0

    # ── 生命周期（都在宿主 loop 上跑）────────────────────────────────

    async def start(self) -> bool:
        """建连接、拿网关地址、启动 SDK 的 WS 线程。返回是否成功。"""
        if not SDK_AVAILABLE:
            logger.error(f"[qq/gateway] SDK 不可用，跳过启动：{_IMPORT_ERROR}")
            return False

        import httpx

        rt = self._rt
        # httpx 客户端与宿主 loop 绑定：分片上传的 COS PUT 与附件下载共用
        rt.http = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
        rt.api = QQApiClient(app_id=rt.cfg.app_id, client_secret=rt.cfg.secret)
        rt.api.setup(rt.http)
        rt.uploader = MediaUploader(api_client=rt.api, http_client=rt.http, log_tag="cili-qq")

        # ws 会话（resume 游标）按账号分目录（设计 §4.5）：SDK 的
        # WSSessionStore 构造时整读、保存时整写，多账号共用同一目录会
        # last-write-wins 互相覆盖。
        base_dir = Path(DATA_DIR) / "tools" / "qq" / rt.account_id
        base_dir.mkdir(parents=True, exist_ok=True)
        self._session_store = WSSessionStore(base_dir=str(base_dir))

        callbacks = self._make_callbacks(rt)
        rt.ws = QQWebSocket(callbacks=callbacks, log_tag="cili-qq")

        await rt.api.ensure_token()
        gateway_url = await rt.api.get_gateway_url()
        rt.ws.start(gateway_url, rt.loop)
        logger.info(f"[qq/gateway] 账号 {rt.account_id} 网关已启动（SDK 自带传输线程）")
        return True

    async def stop(self) -> None:
        rt = self._rt
        if rt.ws is not None:
            try:
                await rt.ws.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[qq/gateway] 关闭 WS 失败：{e}")
            rt.ws = None
        if rt.http is not None:
            try:
                await rt.http.aclose()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[qq/gateway] 关闭 HTTP 客户端失败：{e}")
            rt.http = None

    # ── 回调接线 ────────────────────────────────────────────────────

    def _make_callbacks(self, rt: QQRuntime) -> Any:
        """构造 ``WSCallbacks``（11 个必填 + 若干可选）。"""
        store = self._session_store
        app_id = self._app_id

        async def on_message_event(event_type: str, data: dict) -> None:
            try:
                event = self._parser.parse(event_type, data) if self._parser else None
                if event is None:
                    return
                await self._on_message(event)
            except Exception:
                logger.exception("[qq/gateway] 入站事件处理失败")

        def get_session():
            s = store.get(app_id)
            return (s.session_id or None, s.seq)

        def set_session(session_id, seq) -> None:
            store.save(app_id, session_id or "", seq)

        def set_heartbeat_interval(interval: float) -> None:
            self._heartbeat_interval = float(interval or 0.0)

        def fail_pending(reason: str) -> None:
            # 我们不做按钮交互，没有待决 future；仅记录（签名要求必填）
            logger.debug(f"[qq/gateway] fail_pending: {reason}")

        return WSCallbacks(
            on_message_event=on_message_event,
            on_connected=lambda: logger.info("[qq/gateway] 已连接"),
            on_disconnected=lambda: logger.warning("[qq/gateway] 连接断开"),
            on_fatal_error=lambda code, msg: logger.error(
                f"[qq/gateway] 致命错误 {code}: {msg}"
            ),
            get_token=rt.api.ensure_token_sync,
            get_session=get_session,
            set_session=set_session,
            set_heartbeat_interval=set_heartbeat_interval,
            clear_token=rt.api.clear_token,
            fail_pending=fail_pending,
            get_gateway_url=rt.api.get_gateway_url_sync,
            # 可选：不做按钮交互，留 None
            on_interaction_event=None,
        )
