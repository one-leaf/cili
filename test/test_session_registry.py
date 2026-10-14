"""Tests for core/session_registry.py — evict/claim TOCTOU（审查 P1-31）。

回归：evict_idle 在「已 claim 快照」与「pop」之间存在窗口，另一线程的
claim() 可插队成功，随后 runner 仍被淘汰清理，调用方拿着失效 runner 执行。
修复后：快照、选择与 pop 全程持 _claims_lock，pop 前再显式复核目标 key。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from core.session_registry import HostedSession, SessionRegistry


def _make_registry(keys=("ws:a", "ws:b")):
    """构造 max_runners=1、两个闲置 mock runner 的注册表（超上限触发 evict）。"""
    reg = SessionRegistry(max_runners=1)
    runners = {}
    for i, key in enumerate(keys):
        runner = SimpleNamespace(is_running=lambda: False, cleanup=MagicMock())
        reg._hosted[key] = HostedSession(key=key, runner=runner)
        reg._access[key] = 100.0 + i  # keys[0] 最旧 → evict 目标
        runners[key] = runner
    return reg, runners


class TestEvictClaimRace:
    def test_evict_skips_runner_claimed_during_selection(self):
        """claim 插入在选择与 pop 之间的窗口时，evict 不得淘汰该 runner。"""
        reg, runners = _make_registry()
        target = "ws:a"  # 最旧 = evict 目标

        class ClaimDuringSelection(dict):
            """evict 用 _access.get() 选最旧 key：借这次调用模拟竞态窗口内
            另一线程完成 claim（直接改 _claims，绕过锁以在单线程内复现）。"""

            def get(self, k, default=0):
                reg._claims.add(target)
                return super().get(k, default)

        reg._access = ClaimDuringSelection(reg._access)

        reg.evict_idle()

        assert target in reg._hosted, "已 claim 的 runner 被淘汰（TOCTOU 回归）"
        runners[target].cleanup.assert_not_called()

    def test_evict_removes_idle_runner_and_cleans_up(self):
        reg, runners = _make_registry()
        reg.evict_idle()
        assert "ws:a" not in reg._hosted
        assert "ws:b" in reg._hosted
        runners["ws:a"].cleanup.assert_called_once()

    def test_claim_then_evict_keeps_runner(self):
        """先 claim 再 evict：快照阶段即跳过（原有行为的回归保护）。"""
        reg, runners = _make_registry()
        assert reg.claim("ws:a") is True
        reg.evict_idle()
        assert "ws:a" in reg._hosted
        runners["ws:a"].cleanup.assert_not_called()
