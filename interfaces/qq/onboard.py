"""QQ 机器人扫码绑定：生成二维码 → 手机 QQ 扫码 → 凭据自动落库。

复用 ``qqbot-agent-sdk.onboard`` 对 q.qq.com 裸 API（``create_bind_task`` /
``poll_bind_result``）的封装：本进程本地生成一把 AES-256 密钥随建任务请求
发出，服务端用它把机器人 AppSecret 加密后回传，只有持钥方（本进程）能解密
——AppSecret 明文不经过 QQ 服务端日志，也不回传浏览器，继续满足 QQ 接入端
「前端只回显掩码」的约定。

形态与微信 ``WechatLoginStore`` 一致（请求驱动，前端每 2s 调一次 ``poll``，
不占宿主线程）。拿到凭据后按账号 id 合入 ``setting.json`` 的 ``qq.accounts``
——已有条目只覆盖 ``app_id`` / ``secret``，其余字段（启用 / 工作空间 / 备注）
不动；没有则新建一条 ``enabled=False`` 的，由用户点「保存」走既有启停链路
生效（保存时前端表单对该账号 secret 留空 → 继承已保存值，不会丢）。

SDK 未导出细粒度的 onboard API（只有阻塞式 ``start_onboard``），这里用它的
内部函数自己编排，才能按微信的节奏逐次轮询。二维码指向 q.qq.com 官方绑定
页（URL 模板内置于 SDK），扫码者在页上选择要绑定的机器人——必须是扫码者
有管理权限的机器人，否则服务端不会下发凭据。
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from interfaces.qr import qr_png_data_url

logger = logging.getLogger(__name__)

SESSION_TTL_S = 600.0           # 扫码会话有效期
POLL_INTERVAL_MS = 2000         # 建议前端轮询间隔


@dataclass
class OnboardSession:
    """一次扫码绑定的内存状态。"""

    session_id: str
    account_id: str          # 凭据写入哪个账号
    task_id: str             # q.qq.com 绑定任务 id
    aes_key: str             # 本地生成的 AES 密钥（解密 bot_encrypt_secret 用）
    qr_content: str
    qr_png: str
    expires_at: float


def _sdk_onboard():
    """延迟导入 SDK 的 onboard 子模块；未安装时给出可读错误。"""
    try:
        from qqbot_agent_sdk import onboard as sdk_onboard
    except ImportError as e:
        raise RuntimeError(f"未安装 qqbot-agent-sdk（{e}）") from e
    return sdk_onboard


class QQOnboardStore:
    """扫码绑定会话表（内存，``SESSION_TTL_S`` 过期自动清理）。"""

    def __init__(self) -> None:
        self._sessions: dict[str, OnboardSession] = {}

    # ── 对外三个动作（与微信登录 store 同形） ────────────────────────

    async def start(self, account_id: str) -> dict[str, Any]:
        """建绑定任务、生成二维码。凭据之后写入 ``account_id`` 账号。"""
        self._cleanup_expired()
        sdk = _sdk_onboard()
        task = await sdk._create_bind_task()
        qr_url = sdk.build_connect_url(task.task_id)
        session = OnboardSession(
            session_id=secrets.token_hex(8),
            account_id=account_id,
            task_id=task.task_id,
            aes_key=task.aes_key,
            qr_content=qr_url,
            qr_png=qr_png_data_url(qr_url),
            expires_at=time.time() + SESSION_TTL_S,
        )
        self._sessions[session.session_id] = session
        return {
            "session_id": session.session_id,
            "status": "pending",
            "qr_content": session.qr_content,
            "qr_png_data_url": session.qr_png,
            "interval_ms": POLL_INTERVAL_MS,
            "message": "用手机 QQ 扫码，在打开的页面里选择要绑定的机器人",
        }

    async def poll(self, session_id: str) -> dict[str, Any]:
        """轮询绑定结果一次。完成时解密凭据并合入已保存配置。"""
        self._cleanup_expired()
        session = self._sessions.get(session_id)
        if session is None:
            return {"status": "expired", "message": "扫码会话已过期，请重新获取二维码"}
        sdk = _sdk_onboard()
        result = await sdk._poll_bind_result(session.task_id)
        if result.is_expired():
            self._sessions.pop(session_id, None)
            return {"status": "expired", "message": "二维码已过期，请重新获取"}
        if not result.is_completed():
            # pending / 未知状态：继续等
            return {"status": "pending", "message": "等待扫码…"}
        secret = sdk._decrypt_secret(result.bot_encrypt_secret, session.aes_key)
        self._merge_credentials(session.account_id, result.bot_appid, secret)
        self._sessions.pop(session_id, None)
        logger.info(
            "[qq/onboard] 账号 %s 扫码绑定成功（app_id=%s）",
            session.account_id, result.bot_appid,
        )
        return {
            "status": "succeeded",
            "account_id": session.account_id,
            "app_id": result.bot_appid,
            "user_openid": result.user_openid,
            "message": "已获取机器人凭据，点「保存」后生效",
        }

    async def cancel(self, session_id: str) -> dict[str, Any]:
        """取消扫码绑定（丢弃会话；绑定任务由服务端 TTL 兜底过期）。"""
        self._sessions.pop(session_id, None)
        return {"status": "cancelled"}

    # ── 内部 ────────────────────────────────────────────────────────

    def _cleanup_expired(self) -> None:
        now = time.time()
        for sid in [s for s, sess in self._sessions.items() if sess.expires_at < now]:
            self._sessions.pop(sid, None)

    @staticmethod
    def _merge_credentials(account_id: str, app_id: str, secret: str) -> None:
        """凭据合入 setting.json 的 qq.accounts（合并语义见模块 docstring）。"""
        from interfaces.qq.config import QQConfig, load_qq_accounts, save_qq_accounts

        accounts = load_qq_accounts()
        for acc in accounts:
            if acc.id == account_id:
                acc.app_id = app_id
                acc.secret = secret
                break
        else:
            accounts.append(QQConfig(id=account_id, app_id=app_id, secret=secret))
        save_qq_accounts(accounts)


_STORE: QQOnboardStore | None = None


def get_onboard_store() -> QQOnboardStore:
    """进程级单例（路由层入口）。"""
    global _STORE
    if _STORE is None:
        _STORE = QQOnboardStore()
    return _STORE
