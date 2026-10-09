"""Chat / SSE 流 / Runner 控制 路由域（ask_user 答案处理见 routes_ask_user）。"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.config import load_config, get_workspace_data_dir
from core.event_bus import get_event_bus
from core.goal import get_goal_manager
from core.memory_pipeline import memory_enabled, schedule_extraction
from core.session import SessionStore

from web.deps import (
    master_runners, _get_or_create_runner, _require_workspace,
    _SAFE_ID_RE, _validate_session_id, _validate_workspace_uuid,
    _claim_session_run, _release_session_run, _make_sse_callbacks, _sse_stream,
)
from web.goal_runner import format_goal_status, get_runner, start_goal_runner, stop_goal_runner
from web.routes_ask_user import (
    _find_pending_ask_user, _build_other_answer, _inject_ask_user_answer,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------- Models ----------

class SendMessageRequest(BaseModel):
    content: str
    images: list[dict] | None = None  # [{ "data": "base64...", "media_type": "image/png" }]


class RevertRequest(BaseModel):
    msg_id: str  # 要撤销到的消息 ID


def _get_session(workspace_uuid: str, session_id: str) -> SessionStore | None:
    """Load a SessionStore for the given session (lightweight, no master runner)."""
    sessions_dir = get_workspace_data_dir(workspace_uuid) / "sessions"
    if not sessions_dir.exists():
        return None
    return SessionStore.load_session(session_id, sessions_dir)


# ----- Tool Output Streaming -----

@router.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/stream/{tool_use_id}")
async def stream_tool_output(
    workspace_uuid: str,
    session_id: str,
    tool_use_id: str,
    offset: int = 0,
    ws_dir: Path = Depends(_require_workspace),
):
    """读取工具输出文件的新增内容，供前端轮询实时显示。

    文件命名格式为 {tool_use_id}_{tool_name}.txt，此接口通过 glob 匹配查找。

    Args:
        tool_use_id: 工具调用 ID（文件名的前缀部分）
        offset: 从第几个字节开始读取（前端记录上次位置）

    Returns:
        {content: 新内容, offset: 新位置, exists: 文件是否存在}
    """
    # 安全检查：tool_use_id 只允许字母、数字、下划线、短横线
    if not _SAFE_ID_RE.match(tool_use_id):
        raise HTTPException(status_code=400, detail="Invalid tool_use_id")
    _validate_session_id(session_id)

    sessions_dir = ws_dir / "sessions"
    session_dir = sessions_dir / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    # 查找匹配 {tool_use_id}.txt 或 {tool_use_id}_*.txt 的文件
    # 同时在 session 目录和 exec_* 子目录中搜索（子代理的输出在 exec_* 目录）
    matches = list(session_dir.glob(f"{tool_use_id}.txt")) + list(session_dir.glob(f"{tool_use_id}_*.txt"))
    # 搜索 exec_* 子目录
    for exec_dir in session_dir.glob("exec_*"):
        if exec_dir.is_dir():
            matches.extend(exec_dir.glob(f"{tool_use_id}.txt"))
            matches.extend(exec_dir.glob(f"{tool_use_id}_*.txt"))

    if not matches:
        return {"content": "", "offset": 0, "exists": False}

    output_file = matches[0]  # 只取第一个匹配

    try:
        file_size = output_file.stat().st_size
        if offset >= file_size:
            # 没有新内容
            return {"content": "", "offset": file_size, "exists": True}

        with open(output_file, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            new_content = f.read()

        return {
            "content": new_content,
            "offset": file_size,
            "exists": True,
        }
    except Exception as e:
        logger.warning(f"Failed to read stream file for {tool_use_id}: {e}")
        return {"content": "", "offset": offset, "exists": True}


# ----- Global Event Stream -----

@router.get("/api/events")
async def stream_global_events(workspace_uuid: str = "", session_id: str = ""):
    """全局 SSE 事件流：实时推送 worker 子代理消息与工具输出增量。

    与现有 POST SSE（request-scoped，只能推 master 同步事件）互补——事件总线
    广播后台线程产生的异步事件（worker 逐 token 消息、工具实时输出），
    前端通过 EventSource 订阅。EventSource 只支持 GET，鉴权走 ?token= 查询参数
    （check_access_control 中间件支持）。

    可选 workspace_uuid/session_id 过滤；不传则接收所有事件。
    """
    if session_id:
        _validate_session_id(session_id)
    if workspace_uuid:
        _validate_workspace_uuid(workspace_uuid)

    event_queue: queue.Queue[dict] = queue.Queue(maxsize=512)
    bus = get_event_bus()
    bus.subscribe(event_queue, workspace_uuid or None, session_id or None)

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.to_thread(event_queue.get, True, 15)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    # 心跳（SSE 注释行，客户端忽略），防中间代理空闲断连
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(event_queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ----- Chat -----

@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/messages")
async def send_message(workspace_uuid: str, session_id: str, request: SendMessageRequest):
    """Send a message and get a streaming SSE response."""
    content = request.content.strip()

    # Handle special commands
    if content == "/help":
        help_text = """**特殊命令：**

- `/help` - 显示本帮助信息
- `/status` - 显示当前会话状态（上下文长度、用量等）
- `/goal <目标>` - 设置长期目标并自动循环执行（`/goal status|pause|resume|clear` 管理）

**工具使用：**
直接描述你要完成的任务即可，AI 会自动选择合适的工具。
"""
        # Save to session via SessionStore
        sm = _get_session(workspace_uuid, session_id)
        if sm:
            sm.add_message("user", content, flush=False)
            sm.add_message("assistant", [{"type": "text", "text": help_text}], flush=False)
            sm.save()
        return StreamingResponse(_sse_stream({"type": "text", "content": help_text}), media_type="text/event-stream")

    if content == "/status":
        runner = await _get_or_create_runner(workspace_uuid, session_id)
        usage = runner.get_usage()

        # 使用 runner 内部的 token 计数方法（更准确）
        messages = runner.session.get_valid_messages()
        context_tokens = runner._count_messages_tokens(messages)
        body_size = runner._estimate_request_body_size(messages)

        # Format body size
        if body_size > 1_000_000:
            body_size_str = f"{body_size / 1_000_000:.2f} MB"
        else:
            body_size_str = f"{body_size / 1_000:.1f} KB"

        status_text = f"""**当前会话状态：**

- **模型：** {runner.config.model.name} ({runner.config.model.interface_type})
- **上下文长度：** ~{context_tokens} tokens
- **请求体大小：** {body_size_str}
- **API 调用次数：** {usage['api_calls']}
- **输入 tokens：** {usage['input_tokens']:,}
- **输出 tokens：** {usage['output_tokens']:,}
- **缓存读取：** {usage.get('cache_read_tokens', 0):,} tokens
- **缓存创建：** {usage.get('cache_creation_tokens', 0):,} tokens
- **预填充速度：** {usage.get('prefill_speed', 0):,.2f} tokens/s
- **生成速度：** {usage.get('generation_speed', 0):,.2f} tokens/s
"""
        # Save to session via SessionStore
        runner.session.add_message("user", content, flush=False)
        runner.session.add_message("assistant", [{"type": "text", "text": status_text}], flush=False)
        runner.session.save()
        return StreamingResponse(_sse_stream({"type": "text", "content": status_text}), media_type="text/event-stream")

    # /goal 目标驱动循环：/goal | /goal status | /goal clear | /goal pause | /goal resume | /goal <目标>
    if content == "/goal" or content == "/goal status":
        runner = await _get_or_create_runner(workspace_uuid, session_id)
        manager = get_goal_manager(runner.session.session_dir)
        goal_text = format_goal_status(manager)
        runner.session.add_message("user", content, flush=False)
        runner.session.add_message("assistant", [{"type": "text", "text": goal_text}], flush=False)
        runner.session.save()
        return StreamingResponse(_sse_stream({"type": "text", "content": goal_text}), media_type="text/event-stream")

    if content == "/goal clear":
        runner = await _get_or_create_runner(workspace_uuid, session_id)
        manager = get_goal_manager(runner.session.session_dir)
        stop_goal_runner(f"{workspace_uuid}:{session_id}")  # 请求轮间停止（不中断进行中的单轮）
        manager.clear()
        result_text = "🗑️ 目标已清除，目标循环已停止。"
        runner.session.add_message("user", content, flush=False)
        runner.session.add_message("assistant", [{"type": "text", "text": result_text}], flush=False)
        runner.session.save()
        return StreamingResponse(_sse_stream({"type": "text", "content": result_text}), media_type="text/event-stream")

    if content == "/goal pause":
        runner = await _get_or_create_runner(workspace_uuid, session_id)
        manager = get_goal_manager(runner.session.session_dir)
        if not manager.exists():
            result_text = "当前没有目标，无需暂停。"
        else:
            stop_goal_runner(f"{workspace_uuid}:{session_id}")
            manager.pause()
            result_text = "⏸️ 目标循环已暂停，可用 `/goal resume` 恢复。"
        runner.session.add_message("user", content, flush=False)
        runner.session.add_message("assistant", [{"type": "text", "text": result_text}], flush=False)
        runner.session.save()
        return StreamingResponse(_sse_stream({"type": "text", "content": result_text}), media_type="text/event-stream")

    if content == "/goal resume":
        runner = await _get_or_create_runner(workspace_uuid, session_id)
        manager = get_goal_manager(runner.session.session_dir)
        if not manager.exists():
            result_text = "当前没有已保存的目标，请先用 `/goal <目标>` 设置目标。"
            runner.session.add_message("user", content, flush=False)
            runner.session.add_message("assistant", [{"type": "text", "text": result_text}], flush=False)
            runner.session.save()
            return StreamingResponse(_sse_stream({"type": "text", "content": result_text}), media_type="text/event-stream")
        manager.resume()
        # 同 /goal <目标>：先落恢复确认，再启动循环，保证顺序「命令 → 恢复确认 → 下一轮卡片」
        confirm_text = "▶️ 已恢复目标循环，进度实时显示。"
        runner.session.add_message("user", content, flush=False)
        runner.session.add_message("assistant", [{"type": "text", "text": confirm_text}], flush=False)
        runner.session.save()
        loop = asyncio.get_running_loop()
        runner = await loop.run_in_executor(None, lambda: start_goal_runner(
            workspace_uuid, session_id, runner, manager))
        if runner is None:
            result_text = "上一轮目标循环 60s 内未收尾，暂未能启动新循环，请稍后重试或 `/goal status` 查看状态。"
            runner.session.add_message("assistant", [{"type": "text", "text": result_text}], flush=False)
            runner.session.save()
            return StreamingResponse(_sse_stream({"type": "text", "content": result_text}), media_type="text/event-stream")
        return StreamingResponse(_sse_stream({"type": "text", "content": confirm_text}), media_type="text/event-stream")

    if content.startswith("/goal "):
        objective = content[len("/goal "):].strip()
        if not objective:
            result_text = "请输入目标内容，例如：`/goal 把 README 翻译成中文`"
            runner = await _get_or_create_runner(workspace_uuid, session_id)
            runner.session.add_message("user", content, flush=False)
            runner.session.add_message("assistant", [{"type": "text", "text": result_text}], flush=False)
            runner.session.save()
            return StreamingResponse(_sse_stream({"type": "text", "content": result_text}), media_type="text/event-stream")
        runner = await _get_or_create_runner(workspace_uuid, session_id)
        manager = get_goal_manager(runner.session.session_dir)
        manager.set(objective)
        # 先落用户目标 + 确认文本，再启动循环：若先启动 runner，daemon 线程可能
        # 抢先落占位消息，导致会话顺序变成「轮次卡片 → 用户目标 → 已设置」（显示错乱）
        confirm_text = (f"🎯 已设置目标并开始执行：{objective}\n"
                        "进度实时显示，`/goal status` 查看状态，`/goal pause` 暂停。")
        runner.session.add_message("user", content, flush=False)
        runner.session.add_message("assistant", [{"type": "text", "text": confirm_text}], flush=False)
        runner.session.save()
        loop = asyncio.get_running_loop()
        runner = await loop.run_in_executor(None, lambda: start_goal_runner(
            workspace_uuid, session_id, runner, manager))
        if runner is None:
            result_text = (f"🎯 目标已设置：{objective}\n"
                           "但上一轮目标循环 60s 内未收尾，本次未自动启动，可用 `/goal resume` 恢复。")
            runner.session.add_message("assistant", [{"type": "text", "text": result_text}], flush=False)
            runner.session.save()
            return StreamingResponse(_sse_stream({"type": "text", "content": result_text}), media_type="text/event-stream")
        return StreamingResponse(_sse_stream({"type": "text", "content": confirm_text}), media_type="text/event-stream")

    # Normal message - send to runner
    runner = await _get_or_create_runner(workspace_uuid, session_id)

    # Prevent concurrent execution on the same session
    session_key = f"{workspace_uuid}:{session_id}"
    if not _claim_session_run(session_key):
        goal_runner = get_runner(session_key)
        if goal_runner is not None and goal_runner.is_running():
            error_text = "目标循环执行中，可用 `/goal pause` 暂停或 `/goal status` 查看进度"
        else:
            error_text = "当前会话正在执行中，请等待完成后再发送消息"
        return StreamingResponse(_sse_stream({"type": "error", "content": error_text}), media_type="text/event-stream")

    # ask_user 待回答时：直接把用户输入作为"其他"回复提交（等价于在卡片输入"其他"）。
    # 输入不会作为新 user message 追加，而是注入占位 tool_result 后恢复循环；
    # 前端无需改动，靠推送 tool_result(ask_user) 事件让问题卡片立即关闭。
    pending_ask_user_id: str | None = None
    ask_user_answer: str | None = None
    if content:
        pending_ask_user_id = _find_pending_ask_user(runner)
        if pending_ask_user_id:
            ask_user_answer = _build_other_answer(runner, pending_ask_user_id, content)
            if not _inject_ask_user_answer(runner, pending_ask_user_id, ask_user_answer):
                pending_ask_user_id = None  # 竞态：占位符已消失，回退普通消息

    # Use a queue to bridge sync runner callbacks → async SSE generator
    event_queue: queue.Queue[str | None] = queue.Queue()
    cb = _make_sse_callbacks(event_queue, runner)

    async def generate():
        # Run the runner loop in a background thread
        loop = asyncio.get_running_loop()

        def run_runner():
            try:
                if pending_ask_user_id is not None:
                    # 用户输入已作为 ask_user 的"其他"回复注入占位 tool_result。
                    # 先推送"已应答"事件，让前端把问题卡片标记为已提交（立即关闭），
                    # 再恢复循环；不追加新 user message，避免 LLM 双重处理输入。
                    close_event = json.dumps({
                        "type": "tool_result",
                        "tool": "ask_user",
                        "content": ask_user_answer,
                        "is_error": False,
                        "tool_use_id": pending_ask_user_id,
                    }, ensure_ascii=False)
                    event_queue.put(f"data: {close_event}\n\n")

                    runner.resume_after_ask_user(
                        on_text=cb.on_text,
                        on_thinking=cb.on_thinking,
                        on_tool_call=cb.on_tool_call,
                        on_tool_result=cb.on_tool_result,
                        on_session_start=cb.on_session_start,
                        on_session_complete=cb.on_session_complete,
                    )
                else:
                    # Build user_input: str or list[dict] for multimodal
                    user_input = request.content
                    if request.images:
                        content_blocks: list[dict] = [
                            {"type": "text", "text": request.content, "_valid": True}
                        ]
                        for img in request.images:
                            content_blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": img.get("media_type", "image/png"),
                                    "data": img.get("data", ""),
                                },
                                "_valid": True,
                            })
                        user_input = content_blocks

                    runner.run(
                        user_input=user_input,
                        on_text=cb.on_text,
                        on_thinking=cb.on_thinking,
                        on_tool_call=cb.on_tool_call,
                        on_tool_result=cb.on_tool_result,
                        on_session_start=cb.on_session_start,
                        on_session_complete=cb.on_session_complete,
                    )

                # v3 记忆：回合结束后后台提取（不阻塞 SSE 流；失败只记日志）
                try:
                    sm = getattr(runner, "session", None)
                    if sm is not None and memory_enabled(runner.workspace_uuid or ""):
                        schedule_extraction(
                            runner.workspace_uuid or "",
                            runner.current_session_id or "",
                            list(sm.messages),
                        )
                except Exception:
                    logger.exception("Failed to schedule memory extraction")

                # Git 版本管理：回合结束后自动提交并同步远程（后台线程，不阻塞 SSE 流）
                try:
                    from core.config import load_workspace_config
                    workspace_cfg = load_workspace_config(runner.workspace_uuid or "")
                    if workspace_cfg.get("git_enabled", False):
                        import threading
                        from core.workspace_git import auto_commit_workspace
                        threading.Thread(
                            target=auto_commit_workspace,
                            args=(runner.workspace_uuid, runner.current_session_id or ""),
                            kwargs={"sync_remote": True},
                            daemon=True,
                        ).start()
                except Exception:
                    logger.exception("Failed to schedule git auto-commit")
            except Exception as e:
                logger.error(f"master runner error: {e}")
                # 持久化错误消息到会话（error_notice → UI 可见但不发给 LLM）
                try:
                    sm = getattr(runner, "session", None)
                    if sm is not None:
                        sm.add_message("assistant", f"错误: {e}", _meta={"error_notice": True})
                        sm.save()
                except Exception:
                    logger.exception("Failed to persist error message")
                err_event = json.dumps({"type": "error", "content": str(e)}, ensure_ascii=False)
                event_queue.put(f"data: {err_event}\n\n")
            finally:
                _release_session_run(session_key)
                event_queue.put(None)  # sentinel: done

        task = asyncio.ensure_future(loop.run_in_executor(None, run_runner))

        # Stream events from queue to client (use to_thread to avoid blocking the event loop)
        try:
            while True:
                try:
                    # Use to_thread so the blocking queue.get() doesn't block the async loop
                    event = await asyncio.to_thread(event_queue.get, True, 0.5)
                    if event is None:
                        break
                    yield event
                except queue.Empty:
                    continue
        except asyncio.CancelledError:
            # Client disconnected — let the runner keep running in the background.
            # The runner only stops when the user explicitly clicks the stop button
            # (which calls the /stop endpoint). This prevents browser refresh or
            # network glitches from aborting long-running tasks.
            logger.info("Client disconnected, runner continues running in background")
            return

        # Send done signal
        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/stop")
async def stop_runner(workspace_uuid: str, session_id: str):
    """Stop the currently running runner for a session."""
    key = f"{workspace_uuid}:{session_id}"
    if key not in master_runners:
        return {"success": False, "message": "没有正在运行的 master runner"}

    runner = master_runners[key]
    if not runner.is_running():
        return {"success": False, "message": "master runner 当前未在运行"}

    runner.stop()
    return {"success": True, "message": "已发送停止信号"}


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/resume")
async def resume_runner(workspace_uuid: str, session_id: str):
    """Resume runner processing if there are pending notifications.

    Called by frontend when a background sub-agent completes and master is idle.
    Drains notifications from message_bus and continues the runner loop.
    """
    from web.deps import _claim_session_run, _release_session_run, _make_sse_callbacks, _is_session_idle
    import asyncio

    key = f"{workspace_uuid}:{session_id}"

    # Check if session is idle
    if not _is_session_idle(key):
        return {"success": False, "message": "会话正在运行"}

    # Check if there are pending notifications in message_bus
    runner = master_runners.get(key)
    if not runner:
        return {"success": False, "message": "会话不存在"}

    session_id_check = getattr(runner, "_session_id", None)
    if not session_id_check:
        return {"success": False, "message": "会话 ID 不存在"}

    try:
        from core.agent_mailbox import get_agent_mailbox
        mbus = get_agent_mailbox()
        has_unread = mbus.has_unread(session_id_check)
    except Exception:
        has_unread = False

    if not has_unread:
        return {"success": False, "message": "没有待处理的通知"}

    # Claim the session
    if not _claim_session_run(key):
        return {"success": False, "message": "无法认领会话"}

    # Run runner in background thread
    async def run_and_stream():
        event_queue: queue.Queue[str | None] = queue.Queue(maxsize=256)
        callbacks = _make_sse_callbacks(event_queue, runner)

        def run_runner():
            try:
                runner.run(
                    on_text=callbacks.on_text,
                    on_thinking=callbacks.on_thinking,
                    on_tool_call=callbacks.on_tool_call,
                    on_tool_result=callbacks.on_tool_result,
                    on_session_start=callbacks.on_session_start,
                    on_session_complete=callbacks.on_session_complete,
                )
            except Exception as e:
                logger.error(f"Resume runner error: {e}")
            finally:
                _release_session_run(key)
                event_queue.put(None)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, run_runner)

        # Drain remaining events
        while True:
            try:
                event = event_queue.get_nowait()
                if event is None:
                    break
            except queue.Empty:
                break

    # Start background task
    asyncio.create_task(run_and_stream())

    return {"success": True, "message": "已触发继续"}


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/revert")
async def revert_to_message(workspace_uuid: str, session_id: str, request: RevertRequest, ws_dir: Path = Depends(_require_workspace)):
    """撤销到指定消息，删除该消息及其后面的所有消息。"""
    key = f"{workspace_uuid}:{session_id}"
    runner = master_runners.get(key)

    # 如果 runner 存在且正在运行，拒绝操作
    if runner and runner.is_running():
        raise HTTPException(400, "Runner 正在运行中，无法撤销")

    msg_id = request.msg_id

    # 优先使用内存中的 runner（revert 会原地截断共享 messages 并物理截断 jsonl）
    if runner:
        try:
            deleted_count = runner.session.revert_to_message(msg_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
    # 如果 runner 不在内存中，直接从磁盘读取并迁移/重建
    else:
        sm = SessionStore(session_id, ws_dir / "sessions")
        if not sm.load():
            raise HTTPException(404, "Session not found")
        try:
            deleted_count = sm.revert_to_message(msg_id)
        except ValueError as e:
            raise HTTPException(404, str(e))

    return {"success": True, "deleted_count": deleted_count}


@router.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/status")
async def get_runner_status(workspace_uuid: str, session_id: str):
    """Check if a runner is running for a session."""
    key = f"{workspace_uuid}:{session_id}"
    if key not in master_runners:
        return {"running": False}

    runner = master_runners[key]
    return {"running": runner.is_running()}
