"""WechatSink：把 runner 的输出回调变成微信的一条完整回复。

**与 QQ 的根本差别**：iLink **不支持更新已发送的消息**，所以 QQ 那种「一条流式消息
边生成边更新」在这里做不出来。策略改成节拍制：

- 工具调用累计到 **1/2/4/8/16/32/64 次**时各发一条进度消息（指数节拍：短回合最多
  1-2 条不打扰，长回合也有界——7 条进度封顶，给正文正好留第 8 条）
- 回合末：正文（**不含工具轨迹**——轨迹已在进度里）按 ``text_chunk_limit``（默认
  1800）切分，逐条发出；上次进度之后若还有工具调用，正文前补一行累计数

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
from interfaces.wechat.protocol import WechatApiError, WechatAuthError
from interfaces.wechat.runtime import WechatRuntime
from interfaces.wechat.sanitize import sanitize_wechat_text

logger = logging.getLogger(__name__)

# 进度节拍：第 k 条进度消息在累计 PROGRESS_CHECKPOINTS[k-1] 次工具调用后发出。
# 指数间隔 = 短回合最多 1-2 条、长回合也有界（7 条封顶，给正文留第 8 条）。
PROGRESS_CHECKPOINTS = (1, 2, 4, 8, 16, 32, 64)


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
    """一次微信回合的输出接收端（工具进度节拍 + 正文缓冲 + 回合末切分发送）。"""

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

        self._buffer = ""              # 累积正文（只含回答与 ask_user，不含工具轨迹）
        self._tool_count = 0           # 本回合累计工具调用次数
        self._progress_sent = 0        # 已发出的进度消息条数
        self._tools_since: list[str] = []   # 上条进度之后的工具名（进度里去重展示）
        self._lock = threading.Lock()

    # ── OutputSink 回调 ─────────────────────────────────────────────

    def on_text(self, text: str) -> None:
        # 413 去图重试：要求清空已累积的正文
        if text == RETRY_CLEAR_SENTINEL:
            with self._lock:
                self._buffer = ""
            self._base.on_text(text)   # 全局流仍收到清理信号（前端据此清屏）
            return

        self._base.on_text(text)
        with self._lock:
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

        with self._lock:
            self._tool_count += 1
            self._tools_since.append(tool_name)
            due = (
                self._rt.cfg.tool_progress
                and self._progress_sent < len(PROGRESS_CHECKPOINTS)
                and self._tool_count >= PROGRESS_CHECKPOINTS[self._progress_sent]
            )
        if due:
            self._progress_sent += 1
            with self._lock:
                names = self._tools_since
                self._tools_since = []
                count = self._tool_count
            self._submit(self._send_progress(count, names))

    def on_tool_result(
        self, tool_name: str, output: str, is_error: bool, tool_use_id: str,
    ) -> None:
        # 工具结果不推给微信（进度消息已交代「在干什么」），但全局流要收到
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

    async def _send_progress(self, count: int, names: list[str]) -> None:
        """按节拍发一条工具进度（累计 1/2/4/8/16/32/64 次时各一条）。

        进度是心跳不是内容：发送失败（含 -2 配额、-14 登录失效）直接丢弃，
        不进 defer 队列也不重试——不能让心跳挤掉正文的位置。
        """
        rt = self._rt
        if rt.outbound is None:
            return
        shown = "、".join(dict.fromkeys(names[:3]))
        more = f" 等 {len(names)} 次" if len(names) > 3 else ""
        text = f"🔧 已调用 {count} 次工具（{shown}{more}），仍在进行…"
        try:
            await rt.outbound.send_text(self._user_id, text, defer=False)
        except Exception:  # noqa: BLE001 — 心跳失败只记日志
            logger.warning("[wechat/sink] 进度消息发送失败（忽略）")

    async def _flush(self, raw: str) -> None:
        """回合末：停 typing → 整段清洗 → 切分 → 逐条发。"""
        rt = self._rt
        if rt.typing is not None:
            with suppress(Exception):
                await rt.typing.stop(self._user_id)

        text = sanitize_wechat_text(raw)
        with self._lock:
            tools_pending = list(self._tools_since)
            self._tools_since = []
        # 收尾累计行只在「确实发过进度」时补——进度关着的时候，工具调用
        # 本来就不产生任何消息，不该凭空冒出一行累计数
        if self._progress_sent and tools_pending:
            text = f"🔧 累计 {self._tool_count} 次工具调用\n\n{text}".strip()
        if not text:
            return
        if rt.outbound is None:
            logger.warning("[wechat/sink] 发送层未就绪，丢弃本次回复")
            return

        # 留 1 条额度给回合末的 send_user_file（出站文件与文本共享同一份预算）
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

        try:
            for chunk in chunks:
                ok = await rt.outbound.send_text(self._user_id, chunk, reserve=1)
                if not ok:
                    break
        except WechatAuthError:
            # 登录失效：发不进任何地方，只能记日志提示重新扫码
            logger.error("[wechat/sink] 登录已失效，本回合回复未能送达")
        except WechatApiError as e:
            logger.warning(f"[wechat/sink] 正文发送失败：{e}")
