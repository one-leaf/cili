"""ask_user 答案处理 路由域：占位 tool_result 注入 + 审批分支 + 恢复循环。"""

from __future__ import annotations

import json
import logging
import queue

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.teen_mode import get_teen_mode
from core.tools.ask_user import apply_answer, classify_pending
from core.turn_dispatch import PreparedTurn, execute_turn
from web.deps import (
    _claim_session_run,
    _release_session_run,
    registry,
)
from web.sse import (
    drain_outbox_events,
    make_sse_callbacks,
    sse_run_response,
    sse_stream,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class AnswerAskUserRequest(BaseModel):
    tool_use_id: str  # ask_user / 审批卡工具调用的 ID
    answer: str | None = None  # 旧客户端：自由文本答案
    decision: str | None = None  # 审批决策：approve | remember | deny | abort
    answers: list[dict] | None = None  # 提问卡结构化答案


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/answer-ask-user")
async def answer_ask_user(workspace_uuid: str, session_id: str, request: AnswerAskUserRequest):
    """用户提交 ask_user 工具的答案，后端补 tool_result 并继续 runner 循环"""
    get_teen_mode().guard_workspace(workspace_uuid, source="web")
    key = f"{workspace_uuid}:{session_id}"
    runner = registry.get(key)
    if not runner:
        raise HTTPException(404, "Session runner not found")

    # 与 send_message 相同的原子认领，防止两个请求并发 resume 同一 runner 双循环改写 messages
    if not _claim_session_run(key):
        return StreamingResponse(sse_stream({"type": "error", "content": "当前会话正在执行中，请等待完成后再提交答案", "session_id": session_id}), media_type="text/event-stream")

    # 使用前端提供的 tool_use_id
    ask_user_tool_use_id = request.tool_use_id
    logger.info(
        f"[ask-user] 提交答案: tool_use_id={ask_user_tool_use_id} "
        f"decision={request.decision!r} answers={bool(request.answers)}"
    )

    if request.decision is None and request.answer is None and request.answers is None:
        _release_session_run(key)
        raise HTTPException(400, "缺少答案：需要 decision / answers / answer 之一")

    classified = classify_pending(runner.session, runner.permission_store)
    if request.decision is not None and (classified is None or classified[0] != "approval"):
        _release_session_run(key)
        raise HTTPException(400, "当前待决卡片不是审批卡，不接受 decision")
    if request.answers is not None and (classified is None or classified[0] != "question"):
        _release_session_run(key)
        raise HTTPException(400, "当前待决卡片不是提问卡，不接受 answers")

    try:
        outcome = apply_answer(
            runner.session, ask_user_tool_use_id,
            text=request.answer,
            decision=request.decision,
            answers=request.answers,
            permission_store=runner.permission_store,
            permission_rules=getattr(runner, "permission_rules", None),
        )
    except Exception:
        _release_session_run(key)
        raise

    if outcome == "not_found":
        _release_session_run(key)
        raise HTTPException(400, f"Placeholder tool_result not found for tool_use_id: {ask_user_tool_use_id}")
    if outcome == "expired":
        _release_session_run(key)
        return StreamingResponse(
            sse_stream({"type": "error", "content": "该审批已超时，已按拒绝处理", "session_id": session_id}),
            media_type="text/event-stream",
        )

    # 继续 runner 循环（本端点无后续输入来源，客户端断开时取消执行）
    event_queue: queue.Queue[str | None] = queue.Queue()
    cb = make_sse_callbacks(event_queue, runner)

    def _on_ask_user_answered(tool_use_id: str, answer: str) -> None:
        # 前端据 tool_result(ask_user) 关闭卡片（提问卡与审批卡共用）
        close_event = json.dumps({
            "type": "tool_result",
            "tool": "ask_user",
            "content": answer,
            "is_error": False,
            "tool_use_id": tool_use_id,
        }, ensure_ascii=False)
        event_queue.put(f"data: {close_event}\n\n")

    def _on_error(msg: str) -> None:
        err_event = json.dumps({"type": "error", "content": msg}, ensure_ascii=False)
        event_queue.put(f"data: {err_event}\n\n")

    # 复用 core 的回合执行（resume / aborted 分流 + 异常落盘），按历史行为不跑收尾钩子
    prepared = PreparedTurn(
        runner=runner,
        mode="aborted" if outcome == "aborted" else "resume",
        pending_ask_user_id=ask_user_tool_use_id,
        answer=request.answer or request.decision or "",
    )

    def run_runner():
        # 与 routes_chat 相同的按回合接口声明；duck-typing 与不变量见彼处注释
        set_iface = getattr(runner, "set_turn_interface", None)
        try:
            if set_iface is not None:
                set_iface("web")
            execute_turn(prepared, cb, on_ask_user_answered=_on_ask_user_answered,
                         on_error=_on_error, after_turn_hooks=False)
        finally:
            if set_iface is not None:
                set_iface(None)
            _release_session_run(key)
            drain_outbox_events(runner, event_queue)
            event_queue.put(None)  # sentinel: done

    # 断连时显式让 runner 停下：线程不可取消，仅 task.cancel() 不起作用
    return sse_run_response(
        run_runner, event_queue, cancel_on_disconnect=True, on_disconnect=runner.stop
    )
