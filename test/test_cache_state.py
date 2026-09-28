"""CacheState 单元测试。

验证缓存状态追踪：压缩事件记录、LLM 响应更新、缓存失效检测、
累计命中率计算、reset 行为。
"""

import time
import pytest

from core.cache_state import CacheState


class TestCacheStateBasic:
    """CacheState 基础行为：初始化、to_dict、hit_rate。"""

    def test_initial_state(self):
        """初始状态：所有计数为零，命中率 0。"""
        cs = CacheState()
        assert cs.total_cache_read == 0
        assert cs.total_cache_write == 0
        assert cs.total_input == 0
        assert cs.cache_break_count == 0
        assert cs.llm_call_count == 0
        assert cs.hit_rate == 0.0

    def test_to_dict_format(self):
        """to_dict 返回标准字段格式。"""
        cs = CacheState()
        d = cs.to_dict()
        assert "total_cache_read" in d
        assert "hit_rate" in d
        assert d["hit_rate"] == "0.0%"
        assert d["cache_breaks"] == 0

    def test_hit_rate_calculation(self):
        """命中率 = cache_read / (cache_read + input_tokens)。"""
        cs = CacheState()
        cs.on_llm_response(cache_read=8000, cache_write=2000, input_tokens=2000)
        # hit_rate = 8000 / (8000 + 2000) = 80%
        assert cs.hit_rate == 0.8
        assert cs.to_dict()["hit_rate"] == "80.0%"

    def test_hit_rate_zero_total(self):
        """无调用时命中率 0，不报错。"""
        cs = CacheState()
        assert cs.hit_rate == 0.0


class TestCacheStateLLMResponse:
    """LLM 响应后的缓存状态更新。"""

    def test_cumulative_stats(self):
        """多次 LLM 响应后累计统计正确。"""
        cs = CacheState()
        cs.on_llm_response(cache_read=5000, cache_write=1000, input_tokens=500)
        cs.on_llm_response(cache_read=6000, cache_write=0, input_tokens=400)
        cs.on_llm_response(cache_read=7000, cache_write=0, input_tokens=300)
        assert cs.total_cache_read == 18000
        assert cs.total_cache_write == 1000
        assert cs.total_input == 1200
        assert cs.llm_call_count == 3

    def test_first_call_no_break_detection(self):
        """首次调用不触发缓存失效检测（无基线比较）。"""
        cs = CacheState()
        cs.on_llm_response(cache_read=5000, cache_write=1000, input_tokens=500)
        assert cs.cache_break_count == 0


class TestCacheBreakDetection:
    """缓存失效检测与诊断。"""

    def test_stable_cache_no_break(self):
        """缓存稳定增长不触发失效。"""
        cs = CacheState()
        cs.on_llm_response(cache_read=5000, cache_write=1000, input_tokens=500)
        cs.on_llm_response(cache_read=5100, cache_write=0, input_tokens=400)
        assert cs.cache_break_count == 0

    def test_break_detected(self):
        """cache_read 大幅下降触发失效检测。"""
        cs = CacheState()
        cs.on_llm_response(cache_read=10000, cache_write=2000, input_tokens=1000)
        # cache_read 从 10000 降到 5000（下降 50%，>5% 且 >2000）
        cs.on_llm_response(cache_read=5000, cache_write=2000, input_tokens=5000)
        assert cs.cache_break_count == 1

    def test_no_break_for_small_drop(self):
        """小幅下降（<5% 或 <2000 tokens）不触发失效。"""
        cs = CacheState()
        cs.on_llm_response(cache_read=10000, cache_write=1000, input_tokens=1000)
        # 下降 100 tokens（<2000 绝对阈值）
        cs.on_llm_response(cache_read=9900, cache_write=0, input_tokens=1100)
        assert cs.cache_break_count == 0

    def test_diagnose_l2(self):
        """L2 full compact 后缓存失效诊断。"""
        cs = CacheState()
        cs.on_compression(2)
        cs.on_llm_response(cache_read=10000, cache_write=1000, input_tokens=1000)
        cs.on_llm_response(cache_read=5000, cache_write=2000, input_tokens=5000)
        assert cs.cache_break_count == 1

    def test_diagnose_l1(self):
        """L1 microcompact 后缓存失效诊断。"""
        cs = CacheState()
        cs.on_compression(1)
        cs.on_llm_response(cache_read=10000, cache_write=1000, input_tokens=1000)
        cs.on_llm_response(cache_read=5000, cache_write=2000, input_tokens=5000)
        assert cs.cache_break_count == 1

    def test_diagnose_l3(self):
        """L3 emergency 后缓存失效诊断。"""
        cs = CacheState()
        cs.on_compression(3)
        cs.on_llm_response(cache_read=10000, cache_write=1000, input_tokens=1000)
        cs.on_llm_response(cache_read=5000, cache_write=2000, input_tokens=5000)
        assert cs.cache_break_count == 1


class TestCacheStateReset:
    """重置行为。"""

    def test_reset_after_compact(self):
        """reset_after_compact 清除压缩标记和缓存基线，保留累计统计。"""
        cs = CacheState()
        cs.on_compression(1)
        cs.on_compression(2)
        cs.on_llm_response(cache_read=5000, cache_write=1000, input_tokens=500)
        assert cs.l1_triggered is True
        assert cs.l2_triggered is True

        cs.reset_after_compact()
        assert cs.l1_triggered is False
        assert cs.l2_triggered is False
        # 累计统计保留
        assert cs.total_cache_read == 5000
        assert cs.llm_call_count == 1

    def test_full_reset(self):
        """reset() 清除所有状态。"""
        cs = CacheState()
        cs.on_compression(1)
        cs.on_llm_response(cache_read=5000, cache_write=1000, input_tokens=500)
        cs.on_llm_response(cache_read=5000, cache_write=0, input_tokens=500)

        cs.reset()
        assert cs.total_cache_read == 0
        assert cs.total_cache_write == 0
        assert cs.total_input == 0
        assert cs.cache_break_count == 0
        assert cs.llm_call_count == 0
        assert cs.l1_triggered is False

    def test_reset_allows_fresh_detection(self):
        """reset 后新调用不触发失效检测（基线清零）。"""
        cs = CacheState()
        cs.on_llm_response(cache_read=10000, cache_write=1000, input_tokens=1000)
        cs.reset()
        # reset 后首次调用不比较
        cs.on_llm_response(cache_read=3000, cache_write=1000, input_tokens=500)
        assert cs.cache_break_count == 0


class TestCompressionTracking:
    """压缩事件追踪。"""

    def test_l1_tracking(self):
        """L1 microcompact 触发标记。"""
        cs = CacheState()
        cs.on_compression(1)
        assert cs.l1_triggered is True
        assert cs.l2_triggered is False

    def test_l2_tracking(self):
        """L2 full compact 触发标记。"""
        cs = CacheState()
        cs.on_compression(2)
        assert cs.l1_triggered is False
        assert cs.l2_triggered is True

    def test_l3_tracking(self):
        """L3 emergency 触发标记。"""
        cs = CacheState()
        cs.on_compression(3)
        assert cs.l3_triggered is True

    def test_multiple_layers(self):
        """多层压缩同时触发。"""
        cs = CacheState()
        cs.on_compression(1)
        cs.on_compression(3)
        assert cs.l1_triggered is True
        assert cs.l3_triggered is True
