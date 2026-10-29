"""Chat / SSE 流 / Runner 控制 路由域（ask_user 答案处理见 routes_ask_user）。"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.config import get_workspace_data_dir
from core.event_bus import get_event_bus
from core.goal import get_goal_manager
from core.session import SessionStore
from core.stream_executor import get_stream_executor
from core.teen_mode import TeenModeDenied, get_teen_mode
from core.tools.ask_user import find_pending_ask_user
from core.turn_dispatch import PreparedTurn, execute_turn, prepare_turn
from core.turn_queue import QueuedTurn, QueueError, merge_queued_turns
from core.turn_queue import queue as turn_queue
from web.deps import (
    _SAFE_ID_RE,
    _claim_session_run,
    _get_or_create_runner,
    _is_session_idle,
    _release_session_run,
    _require_workspace,
    _validate_session_id,
    _validate_workspace_uuid,
    registry,
    teen_guard,
)
from web.goal_runner import (
    format_goal_status,
    get_runner,
    start_goal_runner,
    stop_goal_runner,
)
from web.sse import (
    drain_outbox_events,
    make_sse_callbacks,
    sse_run_response,
    sse_stream,
)

router = APIRouter()

# 后台恢复任务的强引用集合（asyncio 只持弱引用，见 resume_runner）
_resume_tasks: set[asyncio.Task] = set()


def _persist_command_exchange(sm, session_key: str, content: str, reply: str) -> None:
    """信息类命令（/help、/status、/goal status）落盘 user/assistant 两条消息。

    仅在会话空闲时写：运行中的 runner 循环正在改写同一份 messages，并发
    add_message 会打乱消息顺序。这里不认领会话 —— 控制类命令（/stop、
    /goal pause）需要能在运行中生效，不能因为它们而阻塞。
    """
    if sm is None or not _is_session_idle(session_key):
        return
    sm.add_message("user", content, flush=False)
    sm.add_message("assistant", [{"type": "text", "text": reply}], flush=False)
    sm.save()
logger = logging.getLogger(__name__)


# ---------- Models ----------

class SendMessageRequest(BaseModel):
    content: str
    images: list[dict] | None = None  # [{ "data": "base64...", "media_type": "image/png" }]
    # 驱动客户端类型（web/tui/...）：run_runner 按它 set_turn_interface，
    # 能力档案（Response Surface）与接口专属工具随回合切换。缺省视为 web。
    interface: str | None = None
    # 前端幂等键：同 client_id 重复提交返回原排队项（网络重试不重复排队）
    client_id: str | None = None


class QueueRequest(BaseModel):
    content: str = ""
    images: list[dict] | None = None
    interface: str | None = None
    client_id: str | None = None


class RevertRequest(BaseModel):
    msg_id: str  # 要撤销到的消息 ID


def _get_session(workspace_uuid: str, session_id: str) -> SessionStore | None:
    """Load a SessionStore for the given session (lightweight, no master runner)."""
    sessions_dir = get_workspace_data_dir(workspace_uuid) / "sessions"
    if not sessions_dir.exists():
        return None
    return SessionStore.load_session(session_id, sessions_dir)


def _session_key(workspace_uuid: str, session_id: str) -> str:
    return f"{workspace_uuid}:{session_id}"


def _build_user_input(content: str, images: list[dict] | None) -> Any:
    """文本 + base64 图片 → runner 入参（str 或 content blocks）。"""
    if not images:
        return content
    blocks: list[dict] = [{"type": "text", "text": content, "_valid": True}]
    for img in images:
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.get("media_type", "image/png"),
                "data": img.get("data", ""),
            },
            "_valid": True,
        })
    return blocks


def _delivery_payload(items: list[QueuedTurn]) -> dict:
    """合并批次的投递事件载荷（请求级帧与全局事件共用，避免两处漂移）。"""
    return {
        "item_ids": [it.item_id for it in items],
        "merged_count": len(items),
        "content": "\n\n".join(it.content for it in items if it.content),
        "interfaces": [it.interface for it in items],
        "has_images": any(it.has_images for it in items),
        "created_at": items[0].created_at,
    }


def _pump_queue(key: str) -> None:
    """上一回合结束后的后台泵：空闲、未暂停、无待答 ask_user / goal 时启动下一批。

    与前台流的区别只在渲染路径：后台回合走 ``runner.default_sink``（全局事件
    总线），前端 ``sse-client.js`` 的 master-resume 渲染器负责显示。``claim()``
    是原子闸门——拿不到执行权说明本回合正被前台流/另一个泵驱动，直接退出，
    由该回合在安全边界自行投递。
    """
    if turn_queue.is_paused(key) or not turn_queue.has_items(key):
        return
    runner = registry.get(key)
    if runner is None:
        return
    if find_pending_ask_user(runner.session):
        return
    goal = get_runner(key)
    if goal is not None and goal.is_running():
        return
    if not _claim_session_run(key):
        return
    try:
        items = turn_queue.drain_batch(key)
        if not items:
            return
        user_input, meta = merge_queued_turns(items)
        prepared = PreparedTurn(runner=runner, mode="run", user_input=user_input, meta=meta)
        # 后台泵回合没有前台请求流：投递事件走全局事件流，界面补一条合并用户气泡
        # （getattr duck-typing：测试替身可能没有 default_sink，缺失时只跳过广播）
        sink = getattr(runner, "default_sink", None)
        if sink is not None:
            sink.on_user_message(_delivery_payload(items))
        set_iface = getattr(runner, "set_turn_interface", None)
        try:
            if set_iface is not None:
                set_iface("web")
            execute_turn(
                prepared, runner.default_sink,
                on_error=lambda _msg: turn_queue.pause(key, "error"),
            )
        finally:
            if set_iface is not None:
                set_iface(None)
    except Exception:
        logger.exception("[queue] 后台泵执行失败")
        turn_queue.pause(key, "error")
    finally:
        _release_session_run(key)
    # 最后一轮边界之后才到达的消息：本轮结束后再泵一次
    _pump_queue(key)


def _start_pump(key: str) -> None:
    """在专用线程池里启动后台泵（不阻塞路由协程）。

    任务引用必须持有（asyncio 只持弱引用，无人引用会被 GC 中断）。
    """
    async def _run() -> None:
        await asyncio.get_running_loop().run_in_executor(get_stream_executor(), _pump_queue, key)

    task = asyncio.create_task(_run())
    _resume_tasks.add(task)
    task.add_done_callback(_resume_tasks.discard)


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

    # 青少年模式：不指定工作区时强制按白名单过滤（受限工作区事件不下发）
    teen = get_teen_mode()
    teen_filter: set[str] | None = None
    if teen.is_enabled() and not workspace_uuid:
        from web.deps import _list_all_workspaces
        teen_filter = {ws["uuid"] for ws in _list_all_workspaces()
                       if teen.workspace_access(ws["uuid"]) == "allow"}

    event_queue: queue.Queue[dict] = queue.Queue(maxsize=512)
    bus = get_event_bus()
    bus.subscribe(event_queue, workspace_uuid or None, session_id or None)

    async def generate():
        try:
            while True:
                try:
                    # 走专用池：长驻的 SSE 流不得占用默认 executor，否则请求期
                    # 短任务（如会话列表）会被饿死（见 core/stream_executor）
                    event = await asyncio.get_running_loop().run_in_executor(
                        get_stream_executor(), event_queue.get, True, 15
                    )
                    if teen_filter is not None and event.get("workspace_uuid") not in teen_filter:
                        continue
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
        _persist_command_exchange(sm, f"{workspace_uuid}:{session_id}", content, help_text)
        return StreamingResponse(sse_stream({"type": "text", "content": help_text, "session_id": session_id}), media_type="text/event-stream")

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

        model_buckets = usage.get("models") or {}
        bucket_lines = ""
        if model_buckets:
            bucket_lines = "\n**按模型用量：**\n"
            for model_name, b in model_buckets.items():
                bucket_lines += (
                    f"- **{model_name}**：{b.get('api_calls', 0)} 次调用，"
                    f"输入 {b.get('input_tokens', 0):,} / 输出 {b.get('output_tokens', 0):,} tokens\n"
                )

        status_text = f"""**当前会话状态：**

- **模型：** {runner.model.name} ({runner.model.interface_type})
- **上下文长度：** ~{context_tokens} tokens
- **请求体大小：** {body_size_str}
- **API 调用次数：** {usage['api_calls']}
- **输入 tokens：** {usage['input_tokens']:,}
- **输出 tokens：** {usage['output_tokens']:,}
- **缓存读取：** {usage.get('cache_read_tokens', 0):,} tokens
- **缓存创建：** {usage.get('cache_creation_tokens', 0):,} tokens
- **预填充速度：** {usage.get('prefill_speed', 0):,.2f} tokens/s
- **生成速度：** {usage.get('generation_speed', 0):,.2f} tokens/s
{bucket_lines}"""
        # Save to session via SessionStore
        _persist_command_exchange(
            runner.session, f"{workspace_uuid}:{session_id}", content, status_text
        )
        return StreamingResponse(sse_stream({"type": "text", "content": status_text, "session_id": session_id}), media_type="text/event-stream")

    # /goal 目标驱动循环：/goal | /goal status | /goal clear | /goal pause | /goal resume | /goal <目标>
    if content == "/goal" or content == "/goal status":
        runner = await _get_or_create_runner(workspace_uuid, session_id)
        manager = get_goal_manager(runner.session.session_dir)
        goal_text = format_goal_status(manager)
        _persist_command_exchange(
            runner.session, f"{workspace_uuid}:{session_id}", content, goal_text
        )
        return StreamingResponse(sse_stream({"type": "text", "content": goal_text, "session_id": session_id}), media_type="text/event-stream")

    if content == "/goal clear":
        runner = await _get_or_create_runner(workspace_uuid, session_id)
        manager = get_goal_manager(runner.session.session_dir)
        stop_goal_runner(f"{workspace_uuid}:{session_id}")  # 请求轮间停止（不中断进行中的单轮）
        manager.clear()
        result_text = "🗑️ 目标已清除，目标循环已停止。"
        runner.session.add_message("user", content, flush=False)
        runner.session.add_message("assistant", [{"type": "text", "text": result_text}], flush=False)
        runner.session.save()
        return StreamingResponse(sse_stream({"type": "text", "content": result_text, "session_id": session_id}), media_type="text/event-stream")

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
        return StreamingResponse(sse_stream({"type": "text", "content": result_text, "session_id": session_id}), media_type="text/event-stream")

    if content == "/goal resume":
        runner = await _get_or_create_runner(workspace_uuid, session_id)
        manager = get_goal_manager(runner.session.session_dir)
        if not manager.exists():
            result_text = "当前没有已保存的目标，请先用 `/goal <目标>` 设置目标。"
            runner.session.add_message("user", content, flush=False)
            runner.session.add_message("assistant", [{"type": "text", "text": result_text}], flush=False)
            runner.session.save()
            return StreamingResponse(sse_stream({"type": "text", "content": result_text, "session_id": session_id}), media_type="text/event-stream")
        manager.resume()
        # 同 /goal <目标>：先落恢复确认，再启动循环，保证顺序「命令 → 恢复确认 → 下一轮卡片」
        confirm_text = "▶️ 已恢复目标循环，进度实时显示。"
        runner.session.add_message("user", content, flush=False)
        runner.session.add_message("assistant", [{"type": "text", "text": confirm_text}], flush=False)
        runner.session.save()
        loop = asyncio.get_running_loop()
        # 不要覆盖 runner：start_goal_runner 返回 None（上一轮 60s 未收尾）时仍需它落提示
        resumed = await loop.run_in_executor(None, lambda: start_goal_runner(
            workspace_uuid, session_id, runner, manager))
        if resumed is None:
            result_text = "上一轮目标循环 60s 内未收尾，暂未能启动新循环，请稍后重试或 `/goal status` 查看状态。"
            runner.session.add_message("assistant", [{"type": "text", "text": result_text}], flush=False)
            runner.session.save()
            return StreamingResponse(sse_stream({"type": "text", "content": result_text, "session_id": session_id}), media_type="text/event-stream")
        return StreamingResponse(sse_stream({"type": "text", "content": confirm_text, "session_id": session_id}), media_type="text/event-stream")

    if content.startswith("/goal "):
        objective = content[len("/goal "):].strip()
        if not objective:
            result_text = "请输入目标内容，例如：`/goal 把 README 翻译成中文`"
            runner = await _get_or_create_runner(workspace_uuid, session_id)
            runner.session.add_message("user", content, flush=False)
            runner.session.add_message("assistant", [{"type": "text", "text": result_text}], flush=False)
            runner.session.save()
            return StreamingResponse(sse_stream({"type": "text", "content": result_text, "session_id": session_id}), media_type="text/event-stream")
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
        # 同 /goal resume：不要覆盖 runner，None 分支仍需它落提示
        resumed = await loop.run_in_executor(None, lambda: start_goal_runner(
            workspace_uuid, session_id, runner, manager))
        if resumed is None:
            result_text = (f"🎯 目标已设置：{objective}\n"
                           "但上一轮目标循环 60s 内未收尾，本次未自动启动，可用 `/goal resume` 恢复。")
            runner.session.add_message("assistant", [{"type": "text", "text": result_text}], flush=False)
            runner.session.save()
            return StreamingResponse(sse_stream({"type": "text", "content": result_text, "session_id": session_id}), media_type="text/event-stream")
        return StreamingResponse(sse_stream({"type": "text", "content": confirm_text, "session_id": session_id}), media_type="text/event-stream")

    # Normal message - send to runner
    # 青少年模式：时长用尽 / 时段外禁止发送新消息（进行中的回合跑到结束）。
    # 返回 SSE error 帧而非 403——前端聊天流按事件渲染，文案直接可见。
    try:
        get_teen_mode().check_send_allowed()
    except TeenModeDenied as e:
        return StreamingResponse(
            sse_stream({"type": "error", "content": str(e), "session_id": session_id}),
            media_type="text/event-stream",
        )
    runner = await _get_or_create_runner(workspace_uuid, session_id)
    session_key = _session_key(workspace_uuid, session_id)

    # 本轮是「普通消息」还是「ask_user 的答案」——判定与注入逻辑在 core.turn_dispatch
    # （接口无关，Web 与 QQ 共用）。多模态入参在这里构造好一并传入。
    user_input: Any = _build_user_input(content, getattr(request, "images", None))

    # 驱动客户端类型在返回 SSE 响应**之前**取好：取值异常走普通 HTTP 500，
    # 若留到 run_runner（executor 线程）里取，异常会吞掉结束哨兵把流挂死。
    # getattr duck-typing：web 路由测试广泛用 SimpleNamespace 伪造 request，
    # 缺属性时按缺省处理（与下方 set_iface 的 duck-typing 同一约定）。
    iface = getattr(request, "interface", None) or "web"
    client_id = getattr(request, "client_id", None)

    # 待答 ask_user：本次输入是「答案」，不排队（设计 §3.5：避免排队消息
    # 被误当成问题答案；输入框文本仍走 prepare_turn 的答案分流）
    pending_ask = find_pending_ask_user(runner.session) if content else None

    delivered: dict | None = None
    if pending_ask:
        if not _claim_session_run(session_key):
            goal_runner = get_runner(session_key)
            if goal_runner is not None and goal_runner.is_running():
                error_text = "目标循环执行中，可用 `/goal pause` 暂停或 `/goal status` 查看进度"
            else:
                error_text = "当前会话正在执行中，请等待完成后再发送消息"
            return StreamingResponse(sse_stream({"type": "error", "content": error_text, "session_id": session_id}), media_type="text/event-stream")
        prepared = prepare_turn(runner, content=content, user_input=user_input)
    else:
        # 普通消息：先入队再尝试启动——运行中不再报错，「接收」与「执行」解耦
        try:
            item = turn_queue.enqueue(session_key, QueuedTurn(
                item_id="",
                content=content,
                user_input=user_input,
                interface=iface,
                client_id=client_id,
                has_images=bool(getattr(request, "images", None)),
            ))
        except QueueError as e:
            return StreamingResponse(sse_stream({"type": "error", "content": str(e), "session_id": session_id}), media_type="text/event-stream")

        if not _claim_session_run(session_key):
            # 其他客户端 / 目标循环持有执行权：消息留在队列，由持权回合在
            # 安全边界投递，或回合结束后由后台泵启动
            return StreamingResponse(sse_stream({
                "type": "queued",
                "session_id": session_id,
                "item": item.preview(),
                "queue_paused": turn_queue.is_paused(session_key),
            }), media_type="text/event-stream")

        items = turn_queue.drain_batch(session_key)
        if not items:
            # 暂停态（并发的 /stop）：不启动，释放认领，消息留在队列
            _release_session_run(session_key)
            return StreamingResponse(sse_stream({
                "type": "queued",
                "session_id": session_id,
                "item": item.preview(),
                "queue_paused": True,
            }), media_type="text/event-stream")

        merged_input, merged_meta = merge_queued_turns(items)
        prepared = PreparedTurn(runner=runner, mode="run",
                                user_input=merged_input, meta=merged_meta)
        delivered = _delivery_payload(items)

    # Use a queue to bridge sync runner callbacks → async SSE generator
    event_queue: queue.Queue[str | None] = queue.Queue()
    cb = make_sse_callbacks(event_queue, runner)

    if delivered is not None:
        # 请求级流即时校正前端乐观气泡；全局流让其他标签页也能看到这条合并消息
        event_queue.put("data: " + json.dumps(
            {"type": "user_message", "session_id": session_id, **delivered},
            ensure_ascii=False,
        ) + "\n\n")
        default_sink = getattr(runner, "default_sink", None)
        if default_sink is not None:
            default_sink.on_user_message(delivered)

    def _on_ask_user_answered(tool_use_id: str, answer: str) -> None:
        # 先推送"已应答"事件，让前端把问题卡片标记为已提交（立即关闭），再恢复循环
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

    def run_runner():
        # 按回合声明驱动接口：能力档案（Response Surface）与专属工具随它切换。
        # duck-typing：测试替身/极简 runner 可能没有该方法，跳过即可——
        # 真正的不变量是 finally 里「复位 → 释放认领 → 结束哨兵」必须全部执行，
        # 任何一步异常都会让 SSE 流永久挂起（无哨兵）。
        set_iface = getattr(runner, "set_turn_interface", None)
        try:
            if set_iface is not None:
                set_iface(iface)
            execute_turn(
                prepared, cb,
                on_ask_user_answered=_on_ask_user_answered,
                on_error=_on_error,
            )
        finally:
            if set_iface is not None:
                set_iface(None)
            _release_session_run(session_key)
            drain_outbox_events(runner, event_queue)
            event_queue.put(None)  # sentinel: done
            # 本回合结束：仍有排队项（最后一个边界之后才到达的）→ 启动后台泵。
            # 若用户又发起了新的前台回合并抢先认领，泵会自动退出。
            _pump_queue(session_key)

    # 客户端断开时 runner 继续后台执行，仅 /stop 能中断
    return sse_run_response(run_runner, event_queue)


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/stop")
async def stop_runner(workspace_uuid: str, session_id: str):
    """直接结束当前会话正在运行的 runner（队列不暂停：排队项由泵继续）。"""
    key = _session_key(workspace_uuid, session_id)

    if key not in registry:
        return {"success": False, "message": "没有正在运行的 master runner"}

    runner = registry.get(key)
    is_running = getattr(runner, "is_running", None)
    runner_running = bool(is_running()) if callable(is_running) else False
    goal = get_runner(key)
    goal_running = goal is not None and goal.is_running()

    if runner_running or goal_running:
        stopper = getattr(runner, "stop", None)
        if callable(stopper):
            # 目标循环在跑时同样置 master._stopped：goal 轮间检查后暂停并跳出
            stopper()
        message = "已发送停止信号" if runner_running else "已发送停止信号（目标循环将在本轮结束后暂停）"
        return {"success": True, "message": message}

    return {"success": False, "message": "master runner 当前未在运行"}


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/resume")
async def resume_runner(workspace_uuid: str, session_id: str):
    """Resume runner processing if there are pending notifications.

    Called by frontend when a background sub-agent completes and master is idle.
    Drains notifications from message_bus and continues the runner loop.
    """
    key = f"{workspace_uuid}:{session_id}"

    # Check if session is idle
    if not _is_session_idle(key):
        return {"success": False, "message": "会话正在运行"}

    # Check if there are pending notifications in message_bus
    runner = registry.get(key)
    if not runner:
        return {"success": False, "message": "会话不存在"}

    session_id_check = runner.current_session_id
    if not session_id_check:
        return {"success": False, "message": "会话 ID 不存在"}

    try:
        from core.agent_mailbox import get_agent_mailbox
        mbus = get_agent_mailbox()
        has_unread = mbus.has_unread(session_id_check)
    except Exception as e:
        logger.warning(f"[mailbox] has_unread 检查失败 ({session_id_check}): {e}")
        has_unread = False

    if not has_unread:
        return {"success": False, "message": "没有待处理的通知"}

    # Claim the session
    if not _claim_session_run(key):
        return {"success": False, "message": "无法认领会话"}

    # Run runner in background thread
    async def run_resume():
        def run_runner():
            try:
                # 恢复循环处理后台通知（不追加新用户消息）。
                # 不传 sink：输出走 runner.default_sink（web 层绑定到 /api/events 全局流），
                # 本端点只负责触发，不需要转发流。
                runner.resume_from_notification()
            except Exception as e:
                logger.error(f"Resume runner error: {e}")
            finally:
                _release_session_run(key)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(get_stream_executor(), run_runner)

    # 必须持有任务引用：create_task 的返回值若无人引用，任务可能被 GC 回收而中断
    task = asyncio.create_task(run_resume())
    _resume_tasks.add(task)
    task.add_done_callback(_resume_tasks.discard)

    return {"success": True, "message": "已触发继续"}


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/revert",
             dependencies=[Depends(teen_guard("message_revert", hard=True))])
async def revert_to_message(workspace_uuid: str, session_id: str, request: RevertRequest, ws_dir: Path = Depends(_require_workspace)):
    """撤销到指定消息，删除该消息及其后面的所有消息。"""
    key = f"{workspace_uuid}:{session_id}"
    runner = registry.get(key)

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

    # 撤销后队列若保留旧消息，下一个边界会把它们又插回来——一并清空
    cleared_queue = turn_queue.clear(key)
    return {"success": True, "deleted_count": deleted_count, "cleared_queue": cleared_queue}


@router.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/status")
async def get_runner_status(workspace_uuid: str, session_id: str):
    """会话运行状态 + 队列摘要（刷新/切会话的界面据此恢复队列条）。"""
    key = _session_key(workspace_uuid, session_id)
    snap = turn_queue.snapshot(key)
    running = False
    runner = registry.get(key)
    if runner is not None:
        checker = getattr(runner, "is_running", None)
        running = bool(checker()) if callable(checker) else False
    return {
        "running": running,
        "queue_length": len(snap["items"]),
        "queue_items": snap["items"],
        "queue_paused": snap["paused"],
        "queue_reason": snap["reason"],
    }


# ----- 排队输入（运行中也能发送；队列状态在服务端） -----

@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/queue")
async def enqueue_message(workspace_uuid: str, session_id: str, request: QueueRequest,
                          ws_dir: Path = Depends(_require_workspace)):
    """仅入队（运行中/空闲都可调用）；队列变更经全局事件流广播。"""
    _validate_session_id(session_id)
    if not (ws_dir / "sessions" / session_id).exists():
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        get_teen_mode().check_send_allowed()
    except TeenModeDenied as e:
        raise HTTPException(status_code=403, detail=str(e))

    content = (request.content or "").strip()
    if not content and not request.images:
        raise HTTPException(status_code=400, detail="消息不能为空")

    key = _session_key(workspace_uuid, session_id)
    try:
        item = turn_queue.enqueue(key, QueuedTurn(
            item_id="",
            content=content,
            user_input=_build_user_input(content, request.images),
            interface=request.interface or "web",
            client_id=request.client_id,
            has_images=bool(request.images),
        ))
    except QueueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    _start_pump(key)
    return {"success": True, "item": item.preview(), "queue_paused": turn_queue.is_paused(key)}


@router.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/queue")
async def get_queue(workspace_uuid: str, session_id: str, ws_dir: Path = Depends(_require_workspace)):
    """返回队列快照（items / paused / reason）。"""
    _validate_session_id(session_id)
    key = _session_key(workspace_uuid, session_id)
    snap = turn_queue.snapshot(key)
    return {"success": True, **snap}


@router.delete("/api/workspaces/{workspace_uuid}/sessions/{session_id}/queue/{item_id}")
async def delete_queue_item(workspace_uuid: str, session_id: str, item_id: str,
                            ws_dir: Path = Depends(_require_workspace)):
    """移除一条排队项。"""
    _validate_session_id(session_id)
    if not _SAFE_ID_RE.match(item_id):
        raise HTTPException(status_code=400, detail="Invalid item_id format")
    removed = turn_queue.remove(_session_key(workspace_uuid, session_id), item_id)
    return {"success": removed}


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/queue/clear")
async def clear_queue(workspace_uuid: str, session_id: str, ws_dir: Path = Depends(_require_workspace)):
    """清空队列（会话运行中也可调用）。"""
    _validate_session_id(session_id)
    cleared = turn_queue.clear(_session_key(workspace_uuid, session_id))
    return {"success": True, "cleared": cleared}


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/queue/resume")
async def resume_queue(workspace_uuid: str, session_id: str, ws_dir: Path = Depends(_require_workspace)):
    """解除暂停并尝试启动下一批（会话空闲时）。"""
    _validate_session_id(session_id)
    key = _session_key(workspace_uuid, session_id)
    turn_queue.resume(key)
    _start_pump(key)
    return {"success": True}
