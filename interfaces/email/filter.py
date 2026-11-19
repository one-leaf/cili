"""入站过滤：垃圾邮件、自动消息、退信、认证失败与白名单（设计 §三）。

邮箱没有 QQ/微信那样的平台闸门——地址一旦被爬虫收录，任何人都能发信进来。
每封进站信若都跑一个 LLM 回合，就是无上限的 token 消耗、对垃圾回信导致的
地址被拉黑、自动回复环路与提示词注入。所以这里的默认姿态是**拒绝**：

    白名单之外的一切入站内容，不进回合、不回信、不建状态。

**:func:`check_message` 是纯函数**（只读头部与大小），便于用样本单测；额度与
去重是进程内的有状态闸门（安全阀，不是计费系统，重启清零）。
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict, deque
from email.message import Message
from email.utils import parseaddr

# 被拦截原因 → 中文展示名（设置页拦截统计用）
REASON_LABELS = {
    "too_large": "邮件过大",
    "dsn": "退信/回执",
    "auto": "自动消息",
    "bulk": "批量/邮件列表",
    "bounce": "退信",
    "spam": "垃圾标记",
    "auth": "认证失败",
    "not_whitelisted": "非白名单",
    "rate_limited": "超出额度",
    "duplicate": "重复",
    "fetch_failed": "下载失败",
}

_BULK_PRECEDENCE = {"bulk", "list", "junk"}
_BOUNCE_LOCAL_PARTS = {"mailer-daemon", "postmaster"}

_AUTH_RESULT_RE = re.compile(
    r"\b(dmarc|spf|dkim)\s*=\s*(pass|fail|softfail|neutral|none|temperror|permerror|quarantine|reject)",
    re.IGNORECASE,
)


def sender_address(msg: Message) -> str:
    """From 头部里的纯地址（小写）；解析失败返回空串。"""
    _, addr = parseaddr(str(msg.get("From") or ""))
    return addr.strip().lower()


def is_allowed(sender: str, allowed_senders: list[str] | tuple[str, ...]) -> bool:
    """白名单匹配（大小写不敏感）。空白名单 = 全部拒绝。"""
    addr = (sender or "").strip().lower()
    if not addr:
        return False
    return addr in {str(a).strip().lower() for a in allowed_senders or ()}


def _parse_auth_results(msg: Message) -> dict[str, str]:
    """从 Authentication-Results 里取各方法的结果（后者覆盖前者）。"""
    out: dict[str, str] = {}
    for header in msg.get_all("Authentication-Results") or []:
        for method, result in _AUTH_RESULT_RE.findall(str(header)):
            out[method.lower()] = result.lower()
    return out


def _is_auto_submitted(msg: Message) -> bool:
    """RFC 3834：Auto-Submitted 缺失或为 ``no`` 才视为人工来信。"""
    value = str(msg.get("Auto-Submitted") or "").strip().lower()
    return bool(value) and value != "no"


def _is_bulk(msg: Message) -> bool:
    precedence = str(msg.get("Precedence") or "").strip().lower()
    return precedence in _BULK_PRECEDENCE or bool(msg.get("List-Id"))


def _is_bounce(msg: Message) -> bool:
    """DSN/MDN 的结构特征：multipart/report、空信封发件人、MAILER-DAEMON。"""
    if str(msg.get("Content-Type") or "").lower().startswith("multipart/report"):
        return True
    return_path = str(msg.get("Return-Path") or "").strip()
    if return_path == "<>":
        return True
    local = sender_address(msg).split("@", 1)[0]
    return local in _BOUNCE_LOCAL_PARTS


def _is_spam_marked(msg: Message) -> bool:
    if str(msg.get("X-Spam-Flag") or "").strip().lower() == "yes":
        return True
    return str(msg.get("X-Spam-Status") or "").strip().lower().startswith("yes")


def _fails_auth(msg: Message) -> bool:
    """认证结果闸：dmarc=fail，或 spf 与 dkim 同时失败。

    头部**不存在**时不判失败——小型自建邮件服务器不写 Authentication-Results，
    不能因此全拒；存在但各方法为 none/neutral 也不判失败。
    """
    results = _parse_auth_results(msg)
    if not results:
        return False
    if results.get("dmarc") in ("fail", "quarantine", "reject"):
        return True
    spf_fail = results.get("spf") in ("fail", "softfail")
    dkim_fail = results.get("dkim") in ("fail", "permerror")
    return spf_fail and dkim_fail


def check_message(
    msg: Message,
    *,
    size: int,
    allowed_senders: list[str] | tuple[str, ...],
    max_inbound_bytes: int,
) -> str | None:
    """十道闸的纯头部判断。返回拦截原因，放行返回 None。

    顺序刻意把「便宜的判断」放前面：结构/头部检查不需要下载正文。
    额度与去重是有状态闸门，由调用方通过 :class:`RateLimiter` / :class:`SeenIds`
    另行把关（它们在头闸之后）。
    """
    if size > max_inbound_bytes:
        return "too_large"
    if _is_bounce(msg):
        return "dsn" if str(msg.get("Content-Type") or "").lower().startswith(
            "multipart/report") else "bounce"
    if _is_auto_submitted(msg):
        return "auto"
    if _is_bulk(msg):
        return "bulk"
    if _is_spam_marked(msg):
        return "spam"
    if _fails_auth(msg):
        return "auth"
    if not is_allowed(sender_address(msg), allowed_senders):
        return "not_whitelisted"
    return None


class RateLimiter:
    """每发件人 / 每账号的回合额度（§3.6）。

    时间戳环形表只活在内存里：白名单账号被冒用或失控自动发信时的止血带，
    不是计费系统。``clock`` 可注入便于测试。
    """

    HOUR = 3600.0
    DAY = 86400.0

    def __init__(
        self,
        *,
        per_sender_per_hour: int,
        per_account_per_day: int,
        clock=time.monotonic,
    ) -> None:
        self._per_sender = max(1, int(per_sender_per_hour))
        self._per_account = max(1, int(per_account_per_day))
        self._clock = clock
        self._senders: dict[str, deque] = {}
        self._account: deque = deque()

    def allow(self, sender: str) -> bool:
        """是否允许这个发件人触发本回合（同时消耗账号额度）。"""
        now = self._clock()
        while self._account and now - self._account[0] > self.DAY:
            self._account.popleft()
        if len(self._account) >= self._per_account:
            return False

        bucket = self._senders.setdefault(sender, deque())
        while bucket and now - bucket[0] > self.HOUR:
            bucket.popleft()
        if len(bucket) >= self._per_sender:
            return False

        bucket.append(now)
        self._account.append(now)
        return True


class SeenIds:
    """Message-ID 去重（LRU，容量有界）。"""

    def __init__(self, capacity: int = 1000) -> None:
        self._capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()

    def seen_or_add(self, message_id: str) -> bool:
        """已见过返回 True；否则登记并返回 False。空 id 不去重。"""
        key = (message_id or "").strip()
        if not key:
            return False
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        self._seen[key] = None
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return False
