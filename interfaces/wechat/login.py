"""扫码登录：状态机 + 内存会话表（供 Web 调用）。

**为什么是请求驱动而不是后台任务**：登录就是三个 HTTP 调用（取码 → 轮询状态 → 拿
凭证），前端每 2s 调一次 ``poll`` 即可。这样登录不需要宿主线程，用户关掉设置页后也
不会留下后台任务在跑。

**多账号**（设计 §5.2）：登录会话带 ``target_account_id``——用户在哪个账号行上点
「扫码登录」，凭证就写到哪个账号。``_sessions`` 按 session_id 分键，多账号同时扫码
天然支持。``confirmed`` 时按 ``ilink_bot_id`` 查重：同一微信号不能同时绑在两个账号
行上。

**协议状态机**（``get_qrcode_status`` 返回的 ``status``）：

==============================  ============================================
status                          处理
==============================  ============================================
``wait`` / 未知 / 可重试网络错误   继续等
``scaned_but_redirect``         换到响应里的 ``redirect_host`` 继续轮询
``need_verifycode``             前端收集验证码后带 ``verify_code`` 重试
``verify_code_blocked``        刷新二维码（上限 3 次）
``expired``                     刷新二维码（上限 3 次）
``confirmed``                   保存凭证，结束
``binded_redirect``             该微信号已绑过 bot：本地有凭证就算成功
==============================  ============================================

回给前端的 ``status`` 是另一套取值：``pending`` / ``succeeded`` / ``failed`` /
``expired`` / ``cancelled``，别与协议里的 status 混了。
"""

from __future__ import annotations

import base64
import io
import logging
import secrets
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from interfaces.wechat.account import (
    WechatAccount,
    list_account_ids,
    load_account,
    save_account,
)
from interfaces.wechat.client import WechatClient
from interfaces.wechat.config import WechatConfig
from interfaces.wechat.protocol import WechatApiError

logger = logging.getLogger(__name__)

MAX_QR_REFRESH_COUNT = 3        # 二维码刷新上限（与官方插件一致）
SESSION_TTL_S = 600.0           # 扫码会话有效期
POLL_INTERVAL_MS = 2000         # 建议前端轮询间隔


def qr_png_data_url(content: str) -> str:
    """把二维码内容渲染成 PNG data URL；``qrcode`` 不可用时返回空串。

    ``qrcode`` 与 Pillow 都在 requirements 里（main.py 启动时自动装），但这里仍然
    容错——扫码登录不该因为一个可选库缺失就整个不可用，调用方降级展示原始内容串。
    """
    if not content:
        return ""
    try:
        import qrcode
    except ImportError:
        logger.warning("[wechat/login] 未安装 qrcode，二维码降级为原始内容串")
        return ""
    try:
        image = qrcode.make(content)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 — 渲染失败不该让登录不可用
        logger.exception("[wechat/login] 生成二维码 PNG 失败")
        return ""


@dataclass
class LoginSession:
    """一次扫码登录的内存状态。"""

    session_id: str
    cfg: WechatConfig
    client: WechatClient
    qrcode_id: str
    qr_content: str
    qr_png: str
    base_url: str
    expires_at: float
    target_account_id: str = ""   # 凭证写入哪个账号（空 = 由确认结果决定）
    refresh_count: int = 0
    verification_failed: bool = False


class WechatLoginStore:
    """扫码会话表（内存，``SESSION_TTL_S`` 过期自动清理）。"""

    def __init__(
        self,
        *,
        client_factory: Callable[[WechatConfig], WechatClient] | None = None,
    ) -> None:
        # 工厂只为测试注入 MockTransport；生产就是普通构造
        self._client_factory = client_factory or (lambda cfg: WechatClient(cfg))
        self._sessions: dict[str, LoginSession] = {}

    # ── 对外三个动作 ────────────────────────────────────────────────

    async def start(self, target_account_id: str = "", *, force: bool = False) -> dict[str, Any]:
        """取二维码、开一个会话，凭证写入 ``target_account_id`` 账号。

        ``force=False`` 且目标账号已有凭证时**不取二维码**，直接报已登录；Web 的
        「扫码登录」按钮走 ``force=True``——用户点它就是要扫码，而且若服务端那侧
        凭证已经失效，短路只会让人卡在「显示已登录、实际用不了」的状态。
        """
        await self._cleanup_expired()

        cfg = WechatConfig()
        if target_account_id and not force:
            account = load_account(target_account_id)
            if account.has_token():
                return {
                    "session_id": "",
                    "status": "succeeded",
                    "message": "该账号已登录。",
                    "account_id": target_account_id,
                    "ilink_user_id": account.ilink_user_id,
                }

        client = self._client_factory(cfg)
        try:
            qrcode_id, content = await client.fetch_qr_code()
        except Exception as e:
            with suppress(Exception):
                await client.aclose()
            raise WechatApiError("get_bot_qrcode", errmsg=f"取二维码失败：{e}") from e

        session_id = secrets.token_urlsafe(18)
        session = LoginSession(
            session_id=session_id,
            cfg=cfg,
            client=client,
            qrcode_id=qrcode_id,
            qr_content=content,
            qr_png=qr_png_data_url(content),
            base_url=cfg.base_url,
            expires_at=time.time() + SESSION_TTL_S,
            target_account_id=target_account_id,
        )
        self._sessions[session_id] = session
        return self._payload(session, status="pending", message="请用微信扫码登录。")

    async def poll(self, session_id: str, *, verify_code: str = "") -> dict[str, Any]:
        """查一次扫码状态（前端每 2s 调一次）。"""
        await self._cleanup_expired()
        session = self._sessions.get(session_id)
        if session is None:
            return {
                "session_id": session_id,
                "status": "expired",
                "message": "扫码会话已过期，请重新获取二维码。",
            }

        try:
            data = await session.client.poll_qr_status(
                session.qrcode_id, verify_code=verify_code, base_url=session.base_url,
            )
        except WechatApiError as e:
            if e.retryable:
                # 网络抖动 → 继续等（用户还举着手机呢）
                return self._payload(session, status="pending", message="等待微信响应…")
            await self._close(session_id)
            return {"session_id": session_id, "status": "failed", "message": f"扫码登录失败：{e}"}

        status = str(data.get("status", "") or "")
        return await self._handle_status(session_id, session, status, data, verify_code)

    async def cancel(self, session_id: str) -> dict[str, Any]:
        await self._close(session_id)
        return {"session_id": session_id, "status": "cancelled", "message": "已取消扫码登录。"}

    async def aclose(self) -> None:
        """关掉全部会话（进程退出时用）。"""
        for session_id in list(self._sessions):
            await self._close(session_id)

    def session_count(self) -> int:
        return len(self._sessions)

    # ── 状态机 ──────────────────────────────────────────────────────

    async def _handle_status(
        self,
        session_id: str,
        session: LoginSession,
        status: str,
        data: dict[str, Any],
        verify_code: str,
    ) -> dict[str, Any]:
        if status == "confirmed":
            return await self._on_confirmed(session_id, session, data)

        if status == "scaned_but_redirect":
            host = str(data.get("redirect_host", "") or "").strip()
            if host:
                session.base_url = (
                    host if host.startswith(("http://", "https://")) else f"https://{host}"
                )
            return self._payload(session, status="pending", message="已扫码，正在切换接入点…")

        if status == "need_verifycode":
            session.verification_failed = bool(verify_code)
            return self._payload(
                session,
                status="pending",
                message=(
                    "验证码不正确，请输入微信上显示的新数字。"
                    if verify_code
                    else "请输入微信上显示的数字以继续。"
                ),
                challenge="verify_code",
            )

        if status in ("verify_code_blocked", "expired"):
            if not await self._refresh_qr(session):
                await self._close(session_id)
                return {
                    "session_id": session_id,
                    "status": "failed",
                    "message": "二维码刷新次数过多，请稍后重试。",
                }
            return self._payload(
                session,
                status="pending",
                message=(
                    "验证被拒绝，已换新二维码，请重新扫码。"
                    if status == "verify_code_blocked"
                    else "二维码已过期，已自动刷新。"
                ),
            )

        if status == "binded_redirect":
            return await self._on_binded_redirect(session_id)

        # wait / 未知状态 → 继续等
        return self._payload(session, status="pending", message="等待扫码…")

    def _find_account_id_by_bot_id(self, bot_id: str) -> str:
        """按 ilink_bot_id 找已绑定的账号 id（找不到返回 ""）。"""
        if not bot_id:
            return ""
        for account_id in list_account_ids():
            if load_account(account_id).ilink_bot_id == bot_id:
                return account_id
        return ""

    async def _on_confirmed(
        self,
        session_id: str,
        session: LoginSession,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        token = str(data.get("bot_token", "") or "")
        if not token:
            await self._close(session_id)
            return {
                "session_id": session_id,
                "status": "failed",
                "message": "微信确认了扫码但没有返回凭证，请重试。",
            }

        # 同一微信号不能同时绑在两个账号行上：按 ilink_bot_id 查重
        bot_id = str(data.get("ilink_bot_id", "") or "")
        conflict_id = self._find_account_id_by_bot_id(bot_id) if bot_id else ""
        if conflict_id and conflict_id != session.target_account_id:
            await self._close(session_id)
            return {
                "session_id": session_id,
                "status": "failed",
                "message": "该微信号已绑定其它账号，请先在那个账号上登出再扫码。",
            }

        account_id = session.target_account_id or conflict_id or secrets.token_hex(4)
        account = WechatAccount(
            bot_token=token,
            base_url=str(data.get("baseurl", "") or session.base_url or session.cfg.base_url),
            ilink_user_id=str(data.get("ilink_user_id", "") or ""),
            ilink_bot_id=bot_id,
            # 新登录 → 长轮询游标从头开始（只影响本账号）
            get_updates_buf="",
        )
        save_account(account_id, account)
        await self._close(session_id)
        logger.info(
            f"[wechat/login] 账号 {account_id} 登录成功 "
            f"ilink_user_id={account.ilink_user_id or '(未知)'}"
        )
        return {
            "session_id": session_id,
            "status": "succeeded",
            "message": "微信已连接。",
            "account_id": account_id,
            "ilink_user_id": account.ilink_user_id,
        }

    async def _on_binded_redirect(self, session_id: str) -> dict[str, Any]:
        """该微信号已绑过 bot。

        **不区分是否 force**：本地握着可用凭证就算成功。cili 要的是「能用」而不是
        「拿到一批新凭证」。优先看目标账号自己的凭证；没有目标账号时，任一账号有
        凭证也算。
        """
        session = self._sessions.get(session_id)
        target_account_id = session.target_account_id if session is not None else ""
        await self._close(session_id)

        if target_account_id:
            account = load_account(target_account_id)
            if account.has_token():
                return {
                    "session_id": session_id,
                    "status": "succeeded",
                    "message": "该微信号已绑定本实例，无需重复登录。",
                    "account_id": target_account_id,
                    "ilink_user_id": account.ilink_user_id,
                }
            return {
                "session_id": session_id,
                "status": "failed",
                "message": "微信说该号已绑定，但本账号没有凭证——请先在设置页登出后再扫码。",
            }

        for account_id in list_account_ids():
            account = load_account(account_id)
            if account.has_token():
                return {
                    "session_id": session_id,
                    "status": "succeeded",
                    "message": "该微信号已绑定本实例，无需重复登录。",
                    "account_id": account_id,
                    "ilink_user_id": account.ilink_user_id,
                }
        return {
            "session_id": session_id,
            "status": "failed",
            "message": "微信说该号已绑定，但本地没有凭证——请先在设置页登出后再试。",
        }

    # ── 内部 ────────────────────────────────────────────────────────

    async def _refresh_qr(self, session: LoginSession) -> bool:
        session.refresh_count += 1
        if session.refresh_count > MAX_QR_REFRESH_COUNT:
            logger.warning("[wechat/login] 二维码刷新次数已达上限")
            return False
        try:
            qrcode_id, content = await session.client.fetch_qr_code()
        except Exception as e:  # noqa: BLE001 — 刷新失败按「不成功」处理，由调用方收尾
            logger.warning(f"[wechat/login] 刷新二维码失败：{e}")
            return False
        session.qrcode_id = qrcode_id
        session.qr_content = content
        session.qr_png = qr_png_data_url(content)
        # 换机房是扫码那一刻的状态，刷新后要回到默认接入点
        session.base_url = session.cfg.base_url
        session.verification_failed = False
        return True

    def _payload(
        self,
        session: LoginSession,
        *,
        status: str,
        message: str,
        challenge: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": session.session_id,
            "status": status,
            "message": message,
            "qrcode_id": session.qrcode_id,
            "qr_content": session.qr_content,
            "qr_png_data_url": session.qr_png,
            "expires_at_ms": int(session.expires_at * 1000),
            "interval_ms": POLL_INTERVAL_MS,
            "account_id": session.target_account_id,
        }
        if challenge:
            payload["challenge"] = challenge
            payload["verification_failed"] = session.verification_failed
        return payload

    async def _close(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            with suppress(Exception):
                await session.client.aclose()

    async def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [sid for sid, s in self._sessions.items() if now >= s.expires_at]
        for session_id in expired:
            logger.info(f"[wechat/login] 扫码会话 {session_id} 已过期，清理")
            await self._close(session_id)
