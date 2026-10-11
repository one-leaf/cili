"""QQSink：把 runner 的输出回调渲染成 QQ 的**单条流式消息**。

单流方案（§5.2）
---------------
整个回合只开**一条**流式消息，内容是一个只增不减的 transcript：

```
🔧 正在调用 read 工具…
🔧 正在调用 grep 工具…
根据检索结果，这个问题在于……

🔧 正在调用 edit 工具…
已修复，改动如下……
```

工具只显示「正在调用 xx 工具…」——不带入参、不带结果。连续同名工具去重。
这样整个回合只占 **1 次被动回复配额**（配额账见 §5.2），而不是每条工具一个消息
（nanobot 的 QQ 通道就是这个坑，§4.8）。

与 ``default_sink`` 的关系
-------------------------
``layer_sink`` 的覆盖语义是**替换**而非叠加，所以这里像 ``web/sse.py`` 一样
**手动扇出到 base**——否则 QQ 驱动的回合在 Web 的 /api/events 全局流里就看不见了。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from core.base_session_runner import RETRY_CLEAR_SENTINEL
from core.output_sink import OutputSink
from interfaces.qq import rawapi
from interfaces.qq.runtime import QQRuntime
from interfaces.qq.sanitize import sanitize_qq_text
from interfaces.qq.stream import StreamingController

logger = logging.getLogger(__name__)


class _QQStreamTarget:
    """``StreamTarget`` 实现：把累积全文推成一条 QQ 流式消息。

    同步入口（runner 线程调用）→ ``run_coroutine_threadsafe`` 交到宿主 loop 串行发送。
    任何异常都只记日志——**绝不能让 sink 抛异常打断 runner**。
    """

    def __init__(self, runtime: QQRuntime, openid: str, msg_id: str) -> None:
        self._rt = runtime
        self._openid = openid
        self._msg_id = msg_id
        self._session: Any = None
        self._lock: asyncio.Lock | None = None
        self._closed = False
        self.sent_ok = 0          # 成功下发的片数（0 → 收尾时降级为静态消息）
        self.failed = False

    # ── StreamTarget 协议 ────────────────────────────────────────────

    def push(self, full_text: str, *, final: bool) -> None:
        self._submit(self._send(full_text, final))

    def restart(self) -> None:
        """收尾当前流；下一片另开一条（用于前缀发散 / 清空重试）。"""
        self._submit(self._complete())

    # ── 内部 ────────────────────────────────────────────────────────

    def _submit(self, coro) -> None:
        loop = self._rt.loop
        if loop is None or loop.is_closed() or self._closed:
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError as e:  # loop 已关闭等
            logger.warning(f"[qq/sink] 提交流式下发失败：{e}")

    async def _send(self, text: str, final: bool) -> None:
        # 惰性建锁：所有 _send 都在同一 loop 上跑，check/set 之间无 await，安全
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            try:
                if self._session is None:
                    self._session = rawapi.StreamSession(
                        self._rt.api,
                        self._openid,
                        self._msg_id,
                        content_type="markdown" if self._rt.cfg.msg_format == "markdown" else "text",
                        msg_seq=_next_msg_seq(self._rt),
                    )
                await self._session.send(text, final=final)
                self.sent_ok += 1
                if final:
                    self._session = None
            except rawapi.PrefixNotExtendableError:
                # 40007：新内容不是已下发前缀的扩展。收尾旧流、另开一条重推。
                logger.warning("[qq/sink] 前缀不可扩展（40007），收尾后另开一条流")
                await self._complete_locked()
                try:
                    self._session = rawapi.StreamSession(
                        self._rt.api,
                        self._openid,
                        self._msg_id,
                        content_type="markdown" if self._rt.cfg.msg_format == "markdown" else "text",
                        msg_seq=_next_msg_seq(self._rt),
                    )
                    await self._session.send(text, final=final)
                    self.sent_ok += 1
                except Exception as e:  # noqa: BLE001
                    self.failed = True
                    logger.warning(f"[qq/sink] 重开流后仍失败：{e}")
            except Exception as e:  # noqa: BLE001 — 不能让异常冒泡打断 runner
                self.failed = True
                logger.warning(f"[qq/sink] 流式下发失败：{e}")

    async def _complete(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            await self._complete_locked()

    async def _complete_locked(self) -> None:
        if self._session is None:
            return
        try:
            await self._session.send(self._session.last_sent_text, final=True)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[qq/sink] 收尾流失败（可忽略）：{e}")
        finally:
            self._session = None

    def close(self) -> None:
        self._closed = True


def _next_msg_seq(runtime: QQRuntime) -> int:
    """``msg_seq`` 用于让 ``msg_id + msg_seq`` 不重复。

    SDK 的 ``next_msg_seq()`` 是伪随机值（不是递增计数器）——API 只要求不重复，
    所以不需要自建计数器。
    """
    try:
        return int(runtime.api.next_msg_seq())
    except Exception:  # noqa: BLE001 — 极端情况下给个能用的值
        import random
        return random.randint(0, 65535)


class QQSink:
    """一次 QQ 回合的输出接收端（单流 transcript + 节流 + 降级）。"""

    def __init__(
        self,
        runtime: QQRuntime,
        openid: str,
        msg_id: str,
        *,
        base: OutputSink | None = None,
    ) -> None:
        self._rt = runtime
        self._openid = openid
        self._msg_id = msg_id
        self._base = base or OutputSink()

        self._target = _QQStreamTarget(runtime, openid, msg_id)
        self._controller = StreamingController(
            self._target,
            flush_interval=runtime.cfg.flush_interval_seconds,
            max_wait=runtime.cfg.flush_max_wait_seconds,
        )
        self._buffer = ""              # 累积 transcript（唯一真相）
        self._last_tool_line: str | None = None
        self._lock = threading.Lock()
        self._pending_text = ""        # 收尾降级用
        self._turned_complete = False

    # ── OutputSink 回调 ─────────────────────────────────────────────

    def on_text(self, text: str) -> None:
        # 413 去图重试：要求清空已下发文本。QQ 侧无法清空 → 收尾另开一条。
        if text == RETRY_CLEAR_SENTINEL:
            with self._lock:
                self._buffer = ""
                self._last_tool_line = None
            self._controller.reset()
            self._base.on_text(text)   # 全局流仍收到清理信号（Web 前端据此清屏）
            return

        self._base.on_text(text)
        clean = sanitize_qq_text(text)
        if not clean and text:
            # 整段都是 thinking/脚手架 → 不推给 QQ
            return
        with self._lock:
            self._last_tool_line = None
            self._buffer += text
            snapshot = self._buffer
        self._controller.push(snapshot)

    def on_thinking(self, text: str) -> None:
        """thinking 不推给 QQ（对齐官方插件「剥离 thinking 标签」），只走全局流。"""
        self._base.on_thinking(text)

    def on_tool_call(self, tool_name: str, tool_input: dict, tool_use_id: str) -> None:
        self._base.on_tool_call(tool_name, tool_input, tool_use_id)

        if tool_name == "ask_user":
            # 交互提示折进流尾部（省 1 次被动回复配额），而不是另发一条消息
            block = self._render_questions(tool_input)
            if block:
                with self._lock:
                    self._buffer += block
                    snapshot = self._buffer
                self._controller.push(snapshot)
            return

        line = f"🔧 正在调用 {tool_name} 工具…"
        with self._lock:
            if line == self._last_tool_line:
                return                      # 连续同名工具不重复追加
            self._last_tool_line = line
            self._buffer += line + "\n"
            snapshot = self._buffer
        self._controller.push(snapshot)

    def on_tool_result(self, tool_name: str, output: str, is_error: bool, tool_use_id: str) -> None:
        # 工具结果**不推给 QQ**（只显示"在调什么"），但全局流要收到
        self._base.on_tool_result(tool_name, output, is_error, tool_use_id)

    def on_session_start(self, exec_id: str, task_summary: str) -> None:
        self._base.on_session_start(exec_id, task_summary)

    def on_session_complete(self, exec_id: str) -> None:
        self._base.on_session_complete(exec_id)

    def on_turn_complete(self) -> None:
        """回合收尾：关流 + 降级兜底 + 通知全局流。"""
        with self._lock:
            self._turned_complete = True
            pending = self._buffer
        self._controller.finish()
        self._target.close()

        # 流从未成功发出任何片 → 降级为静态消息，避免"流式建不起来就彻底没输出"
        if self._target.sent_ok == 0 and pending.strip():
            self._fallback_static(pending)

        self._base.on_turn_complete()

    # ── 组装 ────────────────────────────────────────────────────────

    def build(self) -> OutputSink:
        """产出交给 ``runner.run(sink=...)`` 的 OutputSink。"""
        return OutputSink(
            on_text=self.on_text,
            on_thinking=self.on_thinking,
            on_tool_call=self.on_tool_call,
            on_tool_result=self.on_tool_result,
            on_session_start=self.on_session_start,
            on_session_complete=self.on_session_complete,
            on_turn_complete=self.on_turn_complete,
        )

    # ── 内部 ────────────────────────────────────────────────────────

    @staticmethod
    def _render_questions(tool_input: dict) -> str:
        """把 ask_user 的 questions 渲染成编号 markdown（用户回 "1"/"2" 即可）。"""
        questions = (tool_input or {}).get("questions") or []
        if not questions:
            return ""
        single = len(questions) == 1
        lines = ["", "---", ""]
        for qi, q in enumerate(questions, start=1):
            text = q.get("question") or ""
            lines.append(f"**{text}**" if single else f"**{qi}. {text}**")
            options = q.get("options") or []
            for oi, opt in enumerate(options, start=1):
                label = opt.get("label") or ""
                desc = opt.get("description") or ""
                lines.append(f"{oi}. {label} — {desc}" if desc else f"{oi}. {label}")
            lines.append("")
        if single:
            lines.append("_回复序号即可（如 `1`），也可直接输入其他内容。_")
        else:
            # 多问题时编号在每个问题内重新开始，单个数字有歧义——让用户按
            # 「问题序号: 选项」回答，inbound 侧只在单问题时才自动映射。
            lines.append("_请按 `问题序号: 选项序号` 回复（如 `1: 2`），也可直接输入其他内容。_")
        return "\n".join(lines) + "\n"

    def _fallback_static(self, text: str) -> None:
        """流式不可用时的兜底：用 SDK 的 send_text 发普通消息。

        按 markdown 结构切分（而不是 ``text[:4000]`` 硬截）——硬截会**静默丢内容**。
        片数受剩余被动配额限制；真放不下时在末尾显式标注已截断，不静默。
        """
        rt = self._rt
        if rt.api is None or rt.loop is None or rt.loop.is_closed():
            return

        from interfaces.qq.chunk import chunk_markdown

        chunks = chunk_markdown(text)
        check = rt.limiter.check(self._msg_id)
        # 流一个分片都没发成功 → 配额基本没用；至少留 1 片
        budget = max(1, check.remaining) if check.allowed else 1
        if len(chunks) > budget:
            chunks = chunks[:budget]
            chunks[-1] += "\n\n…（内容过长，已截断）"
        if not chunks:
            return

        async def _send() -> None:
            for i, piece in enumerate(chunks):
                try:
                    await rt.api.send_text(
                        "c2c", self._openid, piece, reply_to=self._msg_id,
                        markdown=rt.cfg.msg_format == "markdown",
                    )
                    rt.limiter.record(self._msg_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[qq/sink] 静态兜底第 {i + 1}/{len(chunks)} 片失败：{e}")
                    return
            logger.info(f"[qq/sink] 流式不可用，已降级为 {len(chunks)} 条静态消息")

        try:
            asyncio.run_coroutine_threadsafe(_send(), rt.loop)
        except RuntimeError as e:
            logger.warning(f"[qq/sink] 静态兜底提交失败：{e}")
