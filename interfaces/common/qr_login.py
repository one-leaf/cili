"""扫码登录的公共常量（QQ / 微信扫码会话同值）。"""

from __future__ import annotations

SESSION_TTL_S = 600.0      # 扫码会话有效期
POLL_INTERVAL_MS = 2000    # 建议前端轮询间隔
