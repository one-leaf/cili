"""ask_user 答案处理 路由域：占位 tool_result 注入 + 审批分支 + 恢复循环。"""

from __future__ import annotations

import asyncio
import json
import logging
import queue

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.tools.ask_user import inject_ask_user_answer

from web.deps import (
    registry, _claim_session_run, _release_session_run,
)
from web.sse import make_sse_callbacks, sse_run_response, sse_stream

router = APIRouter()
logger = logging.getLogger(__name__)


class AnswerAskUserRequest(BaseModel):
    tool_use_id: str  # ask_user 工具调用的 ID
    answer: str  # 用户的答案


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/answer-ask-user")
async def answer_ask_user(workspace_uuid: str, session_id: str, request: AnswerAskUserRequest):
    """用户提交 ask_user 工具的答案，后端补 tool_result 并继续 runner 循环"""
    key = f"{workspace_uuid}:{session_id}"
    runner = registry.get(key)
    if not runner:
        raise HTTPException(404, "Session runner not found")

    # 与 send_message 相同的原子认领，防止两个请求并发 resume 同一 runner 双循环改写 messages
    if not _claim_session_run(key):
        return StreamingResponse(sse_stream({"type": "error", "content": "当前会话正在执行中，请等待完成后再提交答案"}), media_type="text/event-stream")

    # 使用前端提供的 tool_use_id
    ask_user_tool_use_id = request.tool_use_id
    logger.info(f"[ask-user] 尝试为 tool_use_id={ask_user_tool_use_id} 提交答案")

    if not inject_ask_user_answer(runner.session, ask_user_tool_use_id, request.answer,
                                  runner.approval_store):
        raise HTTPException(400, f"Placeholder tool_result not found for tool_use_id: {ask_user_tool_use_id}")

    # 继续 runner 循环（本端点无后续输入来源，客户端断开时取消执行）
    event_queue: queue.Queue[str | None] = queue.Queue()
    cb = make_sse_callbacks(event_queue, runner)

    def run_runner():
        try:
            runner.resume_after_ask_user(sink=cb)
        except Exception as e:
            logger.error(f"master runner error: {e}")
            err_event = json.dumps({"type": "error", "content": str(e)}, ensure_ascii=False)
            event_queue.put(f"data: {err_event}\n\n")
        finally:
            _release_session_run(key)
            event_queue.put(None)  # sentinel: done

    # 断连时显式让 runner 停下：线程不可取消，仅 task.cancel() 不起作用
    return sse_run_response(
        run_runner, event_queue, cancel_on_disconnect=True, on_disconnect=runner.stop
    )
