"""ask_user 答案处理 路由域：占位 tool_result 注入 + 审批分支 + 恢复循环。"""

from __future__ import annotations

import asyncio
import json
import logging
import queue

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.tools.approval import APPROVE_LABEL, REMEMBER_LABEL

from web.deps import (
    master_runners, _SAFE_ID_RE, _new_short_id, _claim_session_run,
    _release_session_run, _make_sse_callbacks, _sse_stream,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class AnswerAskUserRequest(BaseModel):
    tool_use_id: str  # ask_user 工具调用的 ID
    answer: str  # 用户的答案


def _safe_ask_user_filename(tool_use_id: str) -> str:
    """ask_user 答案文件的净名（非法 tool_use_id 回退随机名，防路径穿越）。"""
    if tool_use_id and _SAFE_ID_RE.match(tool_use_id):
        return f"{tool_use_id}.txt"
    return f"{_new_short_id()}.txt"


def _find_pending_ask_user(runner) -> str | None:
    """查找最后一个待回答的 ask_user 占位 tool_result，返回其 tool_use_id（无则 None）。

    占位符由 _execute_tool 生成：user 消息中的 tool_result 块带
    `_meta.completed=False` 且 `_meta.tool_name="ask_user"`。
    """
    for msg in reversed(runner.session.messages):
        if msg["role"] != "user":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_result":
                meta = block.get("_meta") or {}
                if meta.get("completed") is False and meta.get("tool_name") == "ask_user":
                    return block.get("tool_use_id") or block.get("tool_call_id")
    return None


def _build_other_answer(runner, ask_user_tool_use_id: str, content: str) -> str:
    """以用户输入作为 ask_user 的"其他"回复，按卡片 formatAnswers 格式组装（`问题 答案`）。"""
    for msg in runner.session.messages:
        if msg["role"] != "assistant":
            continue
        blocks = msg.get("content", [])
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if block.get("type") in ("tool_use", "tool_call") and block.get("id") == ask_user_tool_use_id:
                questions = (block.get("input") or {}).get("questions", [])
                parts = [f"{q.get('question', '')} {content}" for q in questions if q.get("question")]
                return "\n".join(parts) if parts else content
    return content


def _inject_ask_user_answer(runner, ask_user_tool_use_id: str, answer: str) -> bool:
    """把用户答案注入 ask_user 占位 tool_result 并标记 answered/approval 后持久化。

    返回是否找到占位符。消息块是 runner 与 session 的共享引用，就地修改
    对两者都生效（answer_ask_user 与 send_message 共用）。
    """
    logger.info(f"[ask-user] 注入答案: tool_use_id={ask_user_tool_use_id}")
    found_placeholder = False
    for msg in reversed(runner.session.messages):
        if msg["role"] != "user":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            # 检查两种字段名（tool_use_id 或 tool_call_id）
            block_tool_id = block.get("tool_use_id") or block.get("tool_call_id")
            if block.get("type") == "tool_result" and block_tool_id == ask_user_tool_use_id:
                # 替换占位符内容（内存视图；jsonl 行保持占位）
                block["content"] = answer
                found_placeholder = True
                logger.info(f"[ask-user] 找到并替换 tool_result: tool_use_id={ask_user_tool_use_id}")

                # 总是写答案到外部文件并置 completed/output_path/file_size：
                # 消息已有 seq 不会被 jsonl 重写，reload 时模型/UI 从文件恢复答案
                output_path = _safe_ask_user_filename(ask_user_tool_use_id)
                if "_meta" not in block:
                    block["_meta"] = {}
                block["_meta"].update({
                    "completed": True,
                    "output_path": output_path,
                    "file_size": len(answer.encode("utf-8")),
                })
                try:
                    ext_file = runner.session.session_dir / output_path
                    ext_file.write_text(answer, encoding="utf-8")
                    logger.info(f"[ask-user] 已写入答案文件: {output_path}")
                except Exception as e:
                    logger.warning(f"[ask-user] 写入答案文件失败: {e}")
                break
        if found_placeholder:
            break

    if not found_placeholder:
        logger.error(f"[ask-user] 未找到占位符 tool_result: tool_use_id={ask_user_tool_use_id}")
        return False

    # 在对应的 tool_use/tool_call 块上添加 _answered 标记
    found_tool_use = False
    for msg in runner.session.messages:
        if msg["role"] != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            # 同时检查 tool_use 和 tool_call 两种类型
            if block.get("type") in ("tool_use", "tool_call") and block.get("id") == ask_user_tool_use_id:
                if "_meta" not in block:
                    block["_meta"] = {}
                block["_meta"]["answered"] = True
                found_tool_use = True
                logger.info(f"[ask-user] 在 {block.get('type')} 上添加 _meta.answered=true: id={ask_user_tool_use_id}")
                break
        if found_tool_use:
            break

    if not found_tool_use:
        logger.warning(f"[ask-user] 未找到对应的 tool_use/tool_call: id={ask_user_tool_use_id}")

    # 会话级审批：若本次 ask_user 是高风险命令批准卡，按答案记录批准/拒绝并清空待批槽
    # 答案格式为 "{question} {label}"，用 label 后缀精确匹配区分三档（避免子串误判）
    store = getattr(runner, "approval_store", None)
    if store and store.pending:
        stripped = answer.rstrip()
        kind = store.pending.get("kind", "command")
        if stripped.endswith(REMEMBER_LABEL):
            store.approve(
                store.pending["decision_id"],
                store.pending["command"],
                persist=True,
                reason=store.pending.get("reason", ""),
                kind=kind,
            )
            logger.info(f"[approval] 用户批准并记住高风险命令: {store.pending['command']}")
        elif stripped.endswith(APPROVE_LABEL):
            store.approve(store.pending["decision_id"], store.pending["command"], kind=kind)
            logger.info(f"[approval] 用户批准高风险命令(本次会话): {store.pending['command']}")
        else:
            logger.info(f"[approval] 用户拒绝高风险命令: {store.pending['command']}")
        store.clear_pending()

    # 就地修改了共享消息块（未走 add_message），须置脏否则 save 短路不落盘
    runner.session.mark_dirty()
    runner.session.save()
    return True


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/answer-ask-user")
async def answer_ask_user(workspace_uuid: str, session_id: str, request: AnswerAskUserRequest):
    """用户提交 ask_user 工具的答案，后端补 tool_result 并继续 runner 循环"""
    key = f"{workspace_uuid}:{session_id}"
    runner = master_runners.get(key)
    if not runner:
        raise HTTPException(404, "Session runner not found")

    # 与 send_message 相同的原子认领，防止两个请求并发 resume 同一 runner 双循环改写 messages
    if not _claim_session_run(key):
        return StreamingResponse(_sse_stream({"type": "error", "content": "当前会话正在执行中，请等待完成后再提交答案"}), media_type="text/event-stream")

    # 使用前端提供的 tool_use_id
    ask_user_tool_use_id = request.tool_use_id
    logger.info(f"[ask-user] 尝试为 tool_use_id={ask_user_tool_use_id} 提交答案")

    if not _inject_ask_user_answer(runner, ask_user_tool_use_id, request.answer):
        raise HTTPException(400, f"Placeholder tool_result not found for tool_use_id: {ask_user_tool_use_id}")

    # 继续 runner 循环
    event_queue: queue.Queue[str | None] = queue.Queue()
    cb = _make_sse_callbacks(event_queue, runner)

    async def generate():
        loop = asyncio.get_running_loop()

        def run_agent():
            try:
                runner.resume_after_ask_user(
                    on_text=cb.on_text,
                    on_thinking=cb.on_thinking,
                    on_tool_call=cb.on_tool_call,
                    on_tool_result=cb.on_tool_result,
                    on_session_start=cb.on_session_start,
                    on_session_complete=cb.on_session_complete,
                )
            except Exception as e:
                logger.error(f"master runner error: {e}")
                err_event = json.dumps({"type": "error", "content": str(e)}, ensure_ascii=False)
                event_queue.put(f"data: {err_event}\n\n")
            finally:
                _release_session_run(key)
                event_queue.put(None)  # sentinel: done

        task = asyncio.ensure_future(loop.run_in_executor(None, run_agent))

        try:
            while True:
                try:
                    event = await asyncio.to_thread(event_queue.get, timeout=0.1)
                except Exception:
                    continue
                if event is None:
                    break
                yield event
        except asyncio.CancelledError:
            task.cancel()
            return

        done_event = json.dumps({"type": "done"}, ensure_ascii=False)
        yield f"data: {done_event}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
