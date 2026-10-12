"""WechatSink：把 runner 的输出回调变成微信的一条完整回复。

**与 QQ 的根本差别**：iLink **不支持更新已发送的消息**，所以 QQ 那种「一条流式消息
边生成边更新」在这里做不出来。策略改成：

- 回合内：只起 typing（「正在输入…」）——微信侧唯一的进度信号
- 回合末：把累积的正文按 ``text_chunk_limit``（默认 1800）切分，逐条发出

**为什么整段清洗而不是逐块清洗**：成对标签可能被切在两次流式回调之间，逐块清洗会
漏掉（``core/text_sanitize`` 的正则要求成对出现）。

**扇出 base**：``layer_sink`` 的覆盖语义是替换而非叠加，所以这里像 ``web/sse.py``
与 QQ 一样**手动扇出到 base**——否则微信驱动的回合在 Web 的 ``/api/events`` 全局流里
就看不见了。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress
from typing import Any

from core.base_session_runner import RETRY_CLEAR_SENTINEL
from core.output_sink import OutputSink
from core.text_chunk import chunk_markdown
from interfaces.wechat.runtime import WechatRuntime
from interfaces.wechat.sanitize import sanitize_wechat_text

logger = logging.getLogger(__name__)


def render_questions(tool_input: dict) -> str:
    """把 ask_user 的 questions 渲染成编号 markdown（用户回 "1"/"2" 即可）。

    与 ``inbound.resolve_answer`` 是**一对**：这里的编号必须和那边的回映射一致，
    改一个就得改另一个。
    """
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


class WechatSink:
    """一次微信回合的输出接收端（缓冲 + 回合末切分发送）。"""

    def __init__(
        self,
        runtime: WechatRuntime,
        user_id: str,
        *,
        base: OutputSink | None = None,
    ) -> None:
        self._rt = runtime
        self._user_id = user_id
        self._base = base or OutputSink()

        self._buffer = ""              # 累积正文（唯一真相，回合末统一清洗）
        self._last_tool_line: str | None = None
        self._lock = threading.Lock()

    # ── OutputSink 回调 ─────────────────────────────────────────────

    def on_text(self, text: str) -> None:
        # 413 去图重试：要求清空已累积的正文
        if text == RETRY_CLEAR_SENTINEL:
            with self._lock:
                self._buffer = ""
                self._last_tool_line = None
            self._base.on_text(text)   # 全局流仍收到清理信号（前端据此清屏）
            return

        self._base.on_text(text)
        with self._lock:
            self._last_tool_line = None
            self._buffer += text

    def on_thinking(self, text: str) -> None:
        """thinking 不推给微信（清洗后本就不剩什么），只走全局流。"""
        self._base.on_thinking(text)

    def on_tool_call(self, tool_name: str, tool_input: dict, tool_use_id: str) -> None:
        self._base.on_tool_call(tool_name, tool_input, tool_use_id)

        if tool_name == "ask_user":
            # 交互提示折进正文（微信没有「另发一条」的余地——每条都吃配额）
            block = render_questions(tool_input)
            if block:
                with self._lock:
                    self._buffer += block
            return

        line = f"🔧 正在调用 {tool_name} 工具…"
        with self._lock:
            if line == self._last_tool_line:
                return                      # 连续同名工具不重复追加
            self._last_tool_line = line
            self._buffer += line + "\n"

    def on_tool_result(
        self, tool_name: str, output: str, is_error: bool, tool_use_id: str,
    ) -> None:
        # 工具结果不推给微信（只显示「在调什么」），但全局流要收到
        self._base.on_tool_result(tool_name, output, is_error, tool_use_id)

    def on_session_start(self, exec_id: str, task_summary: str) -> None:
        self._base.on_session_start(exec_id, task_summary)

    def on_session_complete(self, exec_id: str) -> None:
        self._base.on_session_complete(exec_id)

    def on_turn_complete(self) -> None:
        """回合收尾：把缓冲交给宿主 loop 去发（这里在 runner 线程上，不能 await）。"""
        with self._lock:
            pending = self._buffer
            self._buffer = ""
        self._submit(self._flush(pending))
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

    def _submit(self, coro: Any) -> None:
        """把协程派到宿主 loop（同 QQ 的 ``_QQStreamTarget._submit``）。"""
        loop = self._rt.loop
        if loop is None or loop.is_closed():
            logger.warning("[wechat/sink] 宿主 loop 不可用，丢弃本次回复")
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError as e:  # loop 已关闭等
            logger.warning(f"[wechat/sink] 提交发送任务失败：{e}")

    async def _flush(self, raw: str) -> None:
        """回合末：停 typing → 整段清洗 → 切分 → 逐条发。"""
        rt = self._rt
        if rt.typing is not None:
            with suppress(Exception):
                await rt.typing.stop(self._user_id)

        text = sanitize_wechat_text(raw)
        if not text:
            return
        if rt.outbound is None:
            logger.warning("[wechat/sink] 发送层未就绪，丢弃本次回复")
            return

        # 留 1 条额度给回合末的 send_file（出站文件与文本共享同一份预算）
        budget = rt.outbound.budget_left(self._user_id, reserve=1)
        chunks = chunk_markdown(text, limit=rt.cfg.text_chunk_limit)
        if len(chunks) > max(1, budget):
            keep = max(1, budget)
            chunks = chunks[:keep]
            # 不静默丢内容：截断就显式标注（照 QQ 静态兜底的做法）
            chunks[-1] += "\n\n…（内容过长，已截断）"
            logger.warning(
                f"[wechat/sink] 回复超出本轮预算，已截断为 {keep} 条"
            )

        for chunk in chunks:
            ok = await rt.outbound.send_text(self._user_id, chunk, reserve=1)
            if not ok:
                break
