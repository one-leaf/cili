"""Prompt cache 状态追踪。

追踪每轮 LLM 调用的缓存命中/写入情况，检测缓存失效并诊断原因，
提供累计命中率统计。与三层压缩协同：压缩触发时记录事件，
下一轮 LLM 响应后检测缓存是否被打破。

设计文档：docs/design/prompt-cache-design.md（Phase 2）。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class CacheState:
    """追踪 prompt cache 状态，协调压缩与缓存的关系。

    生命周期：Agent 初始化时创建，reset/compact 时重置。
    """

    # 缓存失效检测阈值：相对下降 + 绝对下降同时满足才判定失效
    _RELATIVE_DROP_THRESHOLD = 0.95   # cache_read 下降超 5%
    _ABSOLUTE_DROP_THRESHOLD = 2000   # 且绝对下降超 2000 tokens
    # Anthropic 短 TTL（实际 5min，保守用 4min 判断过期）
    _TTL_MS = 4 * 60 * 1000

    def __init__(self) -> None:
        # 各层压缩触发标记（首次打破缓存，之后幂等稳定）
        self.l1_triggered: bool = False
        self.l2_triggered: bool = False
        self.l3_triggered: bool = False

        # 上次请求的缓存统计
        self._last_cache_read: int = 0
        self._last_request_time: float = 0.0

        # 累计统计
        self.total_cache_read: int = 0
        self.total_cache_write: int = 0
        self.total_input: int = 0
        self.cache_break_count: int = 0
        self.llm_call_count: int = 0

    def on_compression(self, layer: int) -> None:
        """记录压缩事件。layer: 1=L1 microcompact, 2=L2 full compact, 3=L3 emergency。"""
        if layer == 1:
            self.l1_triggered = True
        elif layer == 2:
            self.l2_triggered = True
        elif layer == 3:
            self.l3_triggered = True

    def on_llm_response(
        self,
        cache_read: int,
        cache_write: int,
        input_tokens: int,
    ) -> None:
        """LLM 响应后更新缓存状态。

        检测缓存失效：cache_read 相比上次显著下降且绝对值下降超过阈值。
        """
        self.llm_call_count += 1

        # 检测缓存失效（排除首次调用和 cache_read 为 0 的情况）
        if (self._last_cache_read > 0
                and cache_read < self._last_cache_read * self._RELATIVE_DROP_THRESHOLD
                and self._last_cache_read - cache_read > self._ABSOLUTE_DROP_THRESHOLD):
            self.cache_break_count += 1
            reason = self._diagnose_break()
            logger.info(
                f"[Cache] 缓存失效 #{self.cache_break_count}: {reason} "
                f"(上次 cache_read={self._last_cache_read}, 本次={cache_read})"
            )

        self._last_cache_read = cache_read
        self._last_request_time = time.time()
        self.total_cache_read += cache_read
        self.total_cache_write += cache_write
        self.total_input += input_tokens

    def _diagnose_break(self) -> str:
        """诊断缓存失效原因。优先级：L2 > L1 > L3 > TTL > 未知。"""
        if self.l2_triggered:
            return "L2 full compact 重建了消息序列"
        if self.l1_triggered:
            return "L1 microcompact 首次触发改变了旧 tool_result 内容"
        if self.l3_triggered:
            return "L3 emergency 标记了旧 tool_use 为无效"
        elapsed_ms = (time.time() - self._last_request_time) * 1000
        if elapsed_ms > self._TTL_MS:
            return f"TTL 过期（{elapsed_ms / 1000:.0f}s > {self._TTL_MS / 1000:.0f}s）"
        return "未知（可能是动态注入内容变化或外部因素）"

    def reset_after_compact(self) -> None:
        """L2 full compact 后重置压缩标记和缓存基线。"""
        self.l1_triggered = False
        self.l2_triggered = False
        self.l3_triggered = False
        self._last_cache_read = 0

    def reset(self) -> None:
        """完全重置（/clear 时调用）。"""
        self.l1_triggered = False
        self.l2_triggered = False
        self.l3_triggered = False
        self._last_cache_read = 0
        self._last_request_time = 0.0
        self.total_cache_read = 0
        self.total_cache_write = 0
        self.total_input = 0
        self.cache_break_count = 0
        self.llm_call_count = 0

    @property
    def hit_rate(self) -> float:
        """累计缓存命中率：cache_read / (cache_read + input_tokens)。

        input_tokens 是不走缓存的新计费 tokens。命中率越高，实际新计费越少。
        """
        total = self.total_cache_read + self.total_input
        return self.total_cache_read / total if total > 0 else 0.0

    def to_dict(self) -> dict:
        """返回统计摘要（可序列化，供日志或前端展示）。"""
        return {
            "total_cache_read": self.total_cache_read,
            "total_cache_write": self.total_cache_write,
            "total_input": self.total_input,
            "hit_rate": f"{self.hit_rate:.1%}",
            "cache_breaks": self.cache_break_count,
            "llm_calls": self.llm_call_count,
        }
