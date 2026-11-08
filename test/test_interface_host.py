"""HostBase 生命周期回归测试。

重点是启动超时/停止竞态：宿主启动慢于等待上限时，`stop()` 会打断还在
跑的 `run_until_complete(_async_start())`，抛出的 RuntimeError 不应被当作
"启动失败"记录——那是停止请求，不是失败。
"""

from __future__ import annotations

import asyncio
import logging

import interfaces.common.host as host_mod
from interfaces.common.host import HostBase


class _Cfg:
    id = "test-host"


class _FastHost(HostBase):
    async def _async_start(self) -> None:
        self.ready = True

    async def _async_stop(self) -> None:
        pass


class _SlowHost(HostBase):
    """`_async_start` 挂起，模拟 QQ 网关 token/网关地址请求超时。"""

    def __init__(self, cfg, delay: float) -> None:
        super().__init__(cfg)
        self.delay = delay

    async def _async_start(self) -> None:
        await asyncio.sleep(self.delay)
        self.ready = True

    async def _async_stop(self) -> None:
        pass


class _FailHost(HostBase):
    async def _async_start(self) -> None:
        raise RuntimeError("boom")

    async def _async_stop(self) -> None:
        pass


def test_fast_start_ok(monkeypatch):
    monkeypatch.setattr(host_mod, "_START_TIMEOUT", 5.0)
    host = _FastHost(_Cfg())
    assert host.start() is True
    assert host.is_alive()
    host.stop()
    assert not host.is_alive()


def test_slow_start_times_out_then_stop_is_quiet(monkeypatch, caplog):
    """启动超过等待上限 → start() 返回 False；随后 stop() 打断启动，不应记 ERROR。"""
    monkeypatch.setattr(host_mod, "_START_TIMEOUT", 0.2)
    host = _SlowHost(_Cfg(), delay=2.0)
    with caplog.at_level(logging.ERROR, logger=host_mod.logger.name):
        assert host.start() is False
        host.stop()
    assert not host._thread.is_alive()
    assert not any("启动失败" in r.message for r in caplog.records)


def test_real_start_failure_logs_error(monkeypatch, caplog):
    monkeypatch.setattr(host_mod, "_START_TIMEOUT", 2.0)
    host = _FailHost(_Cfg())
    with caplog.at_level(logging.ERROR, logger=host_mod.logger.name):
        assert host.start() is False
    assert any("启动失败" in r.message for r in caplog.records)
