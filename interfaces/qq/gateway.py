"""SDK 接线：``WSCallbacks`` 全量回调 + 事件分发。

对齐官方插件的 ``gateway/``（qqbot-gateway.ts + event-handlers.ts + lifecycle.ts）。

关键事实（§4.11 #5/#6/#7/#8）：
- ``QQWebSocket`` **自带专用守护线程 + 事件循环**处理网络/心跳/重连，入站回调经
  ``run_coroutine_threadsafe`` 派发到我们传入的 ``main_loop``。所以传输线程归 SDK，
  我们只提供**宿主 loop**。
- ``WSCallbacks`` 有 **11 个必填回调**，缺一个都构造不出来。
- 入站去重（``msg_id`` + 300s 窗口）与重连退避 SDK 已内置，不用自己写。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from core.config import DATA_DIR
from interfaces.qq.runtime import QQRuntime

logger = logging.getLogger(__name__)

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


# 沙箱 API 域名（未过审机器人只能沙箱收发）。
# ⚠️ 待实测确认；若官方沙箱域名不同，改这里或用环境变量 QQ_API_BASE 覆盖。
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
PROD_API_BASE = "https://api.sgroup.qq.com"


def apply_api_base(use_sandbox: bool) -> str:
    """把 SDK 的 ``API_BASE`` 切到沙箱 / 正式，返回生效值。

    做两件事，缺一不可：
    1. **设环境变量** ``QQ_API_BASE`` —— 这是 SDK 还没导入时的唯一途径（SDK 在
       import 时读它）。所以这一步**不依赖 SDK 是否可用**。
    2. **给已导入的模块副本打补丁** —— ``api_client`` 是
       ``from .constants import API_BASE`` 的**按值导入**，只改 ``constants`` 没用，
       ``api_client`` 里那份副本也得改。这样运行时切换才生效（不必重启）。
    """
    base = SANDBOX_API_BASE if use_sandbox else PROD_API_BASE
    os.environ["QQ_API_BASE"] = base

    if SDK_AVAILABLE:
        try:
            from qqbot_agent_sdk import api_client as _ac
            from qqbot_agent_sdk import constants as _const

            _const.API_BASE = base
            _ac.API_BASE = base
        except Exception as e:  # noqa: BLE001 — 切不动就沿用启动时确定的值
            logger.warning(f"[qq/gateway] 切换 API_BASE 失败（将沿用当前值）：{e}")
    return base


def effective_api_base() -> str:
    """当前 SDK 实际使用的 API 域名。"""
    if not SDK_AVAILABLE:
        return ""
    try:
        from qqbot_agent_sdk import api_client as _ac

        return str(getattr(_ac, "API_BASE", ""))
    except Exception:  # noqa: BLE001
        return ""


async def test_connection(app_id: str, secret: str, use_sandbox: bool) -> tuple[bool, str]:
    """测试凭据能否拿到 token 与网关地址。返回 (是否成功, 说明文案)。

    不触碰正在运行的接口——用独立的临时客户端，测完即关。
    """
    if not SDK_AVAILABLE:
        return False, f"未安装 qqbot-agent-sdk（{_IMPORT_ERROR}）"
    if not app_id or not secret:
        return False, "app_id / secret 未填写"

    base = apply_api_base(use_sandbox)

    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        api = QQApiClient(app_id=app_id, client_secret=secret)
        api.setup(client)
        try:
            await api.ensure_token()
            gateway_url = await api.get_gateway_url()
        except Exception as e:  # noqa: BLE001 — 凭据/网络/权限错误统一回报
            return False, f"连接失败：{str(e)[:200]}"
        finally:
            try:
                api.clear_token()
            except Exception:  # noqa: BLE001
                pass

    scope = "沙箱" if use_sandbox else "正式"
    return True, f"连接正常（{scope}环境 {base}，网关 {gateway_url[:48]}…）"


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

        base_dir = Path(DATA_DIR) / "tools" / "qq"
        base_dir.mkdir(parents=True, exist_ok=True)
        self._session_store = WSSessionStore(base_dir=str(base_dir))

        callbacks = self._make_callbacks(rt)
        rt.ws = QQWebSocket(callbacks=callbacks, log_tag="cili-qq")

        await rt.api.ensure_token()
        gateway_url = await rt.api.get_gateway_url()
        rt.ws.start(gateway_url, rt.loop)
        logger.info("[qq/gateway] QQ 网关已启动（SDK 自带传输线程）")
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
