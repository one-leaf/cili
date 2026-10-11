"""流式消息状态机：前缀不可变 + 前缀发散处理 + 节流。

语义照搬官方 TS 插件的 ``outbound/streaming-controller.ts``（其 docstring 原文：
「核心约束：QQ 流式 API 替换模式下，**已下发文本的前缀不可变更**」）。

本模块只管**「什么时候推、推什么文本」**；真正的网络下发交给 ``target``
（``sink.py`` 里的流会话管理器），因此可以用假 target 单测。

两个必须处理的坑
----------------
1. **前缀发散**：新文本不再以已下发内容为前缀时——
   - 更短 → 视为**新回复**：收尾当前流，另开一条
   - 更长但发散 → **只追加发散出来的尾巴**（不能回退已下发内容）
   比较前先**归一化空白**（模型重发时空白常有差异）。

2. **runner 的 413 去图重试**会发 ``RETRY_CLEAR_SENTINEL`` 要求清空已流式输出的
   文本（``core/session_runner_runtime/runner.py``）。QQ 侧**无法清空**，只能走
   上面「新回复」那条路——否则旧文本会永远留在消息开头。

节流参数对齐官方插件的 ``DeliverDebouncer``：窗口 1500ms、最长等待 8000ms。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Protocol

logger = logging.getLogger(__name__)


class StreamTarget(Protocol):
    """流式下发的接收方（由 ``sink.py`` 实现，负责网络与流会话生命周期）。"""

    def push(self, full_text: str, *, final: bool) -> None:
        """推一片累积全文。``final=True`` 表示这一片后收尾。"""
        ...

    def restart(self) -> None:
        """收尾当前流；下一片会开一条新流（用于前缀发散/清空重试）。"""
        ...


def _normalize_ws(s: str) -> str:
    return " ".join(s.split())


def _is_prefix_extension(accepted: str, incoming: str) -> bool:
    """``incoming`` 是否以 ``accepted`` 为前缀（空白归一化后比较）。"""
    if incoming.startswith(accepted):
        return True
    return _normalize_ws(incoming).startswith(_normalize_ws(accepted))


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class StreamingController:
    """节流地把累积文本推给 ``target``，并处理前缀发散。"""

    def __init__(
        self,
        target: StreamTarget,
        *,
        flush_interval: float = 1.5,
        max_wait: float = 8.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._target = target
        self._flush_interval = max(0.0, flush_interval)
        self._max_wait = max(0.0, max_wait)
        self._clock = clock

        self._buffer = ""            # 当前应显示的全文
        self._accepted = ""          # 已请求下发的全文（单一真相）
        self._first_pending_at: float | None = None
        self._last_flush_at = 0.0
        self._chunks = 0             # 已请求的下发次数
        self._finished = False

    # ── 只读状态 ────────────────────────────────────────────────────

    @property
    def chunks_sent(self) -> int:
        return self._chunks

    @property
    def accepted_text(self) -> str:
        return self._accepted

    @property
    def has_sent_chunks(self) -> bool:
        return self._chunks > 0

    # ── 入口 ────────────────────────────────────────────────────────

    def push(self, full_text: str) -> None:
        """内容更新（``full_text`` 是当前应显示的**全文**）。按节流决定是否下发。"""
        if self._finished:
            return
        self._buffer = full_text
        if self._first_pending_at is None:
            self._first_pending_at = self._clock()
        if self._should_flush():
            self._flush(final=False)

    def finish(self) -> None:
        """收尾：把剩余内容推完并标记结束。"""
        if self._finished:
            return
        self._finished = True
        self._flush(final=True)

    def reset(self) -> None:
        """外部要求清空（如 ``RETRY_CLEAR_SENTINEL``）：收尾当前流并从头开始。"""
        if self._finished:
            return
        self._target.restart()
        self._buffer = ""
        self._accepted = ""
        self._chunks = 0
        self._first_pending_at = None

    # ── 内部 ────────────────────────────────────────────────────────

    def _should_flush(self) -> bool:
        if self._chunks == 0:
            # 首片立即下发：先建立起流式消息，用户才看得到「已经在答了」。
            # （不能靠 _last_flush_at 的初值凑——那只是恰好等于真实时钟的 0。）
            return True
        now = self._clock()
        if self._first_pending_at is not None and now - self._first_pending_at >= self._max_wait:
            return True
        return now - self._last_flush_at >= self._flush_interval

    def _flush(self, *, final: bool) -> None:
        text = self._buffer
        if not final and text == self._accepted:
            return  # 内容没变，不必重复下发

        if self._accepted and not _is_prefix_extension(self._accepted, text):
            if len(text) < len(self._accepted):
                # 长度回退 → 新回复：收尾当前流，另开一条
                logger.info(
                    f"[qq/stream] 前缀发散且更短（{len(self._accepted)}→{len(text)}），"
                    f"作为新回复另开一条流"
                )
                self._target.restart()
                self._accepted = ""
            else:
                # 更长但前缀发散 → 只追加发散出来的尾巴，不回退已下发内容
                common = _common_prefix_len(self._accepted, text)
                extra = text[common:]
                merged = self._accepted + extra
                logger.warning(
                    f"[qq/stream] 前缀发散（{len(self._accepted)}→{len(text)}，"
                    f"公共前缀 {common}），改为追加尾巴 {len(extra)} 字符"
                )
                text = merged

        self._target.push(text, final=final)
        self._accepted = text
        self._chunks += 1
        self._last_flush_at = self._clock()
        self._first_pending_at = None
