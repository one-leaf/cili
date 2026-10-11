"""接口无关的回合驱动：认领后的「跑什么 / 怎么收尾」编排。

这段逻辑原先内联在 ``web/routes_chat.py``（约 110 行）与 ``web/routes_ask_user.py``
里，被困在 web 路由中——第二个接入端（QQ）要用只能复制一份。抽出后 Web 与 QQ
共用同一套语义。

职责边界
--------
**本模块负责**（接口无关）：
- 待答 ask_user 的判定与占位 tool_result 注入
- 「新消息 run」与「ask_user 答案 resume」的分流
- 回合后的收尾钩子：v3 记忆提取、git 自动提交（都走后台线程，不阻塞）
- 异常时的错误消息落盘（``error_notice``，UI 可见但不发给 LLM）

**调用方负责**（接口相关）：
- workspace/session 解析、``registry.get_or_create``、运行权认领与释放
- 构造 sink、把 ``execute_turn`` 放进 executor、把结果编码成自己的传输格式
- 忙碌时的提示文案、ask_user 卡片关闭通知、错误展示

``execute_turn`` 是**同步**的（内部调阻塞的 ``runner.run``），调用方需自行放进
executor——Web 走 ``sse_run_response``，QQ 走 ``loop.run_in_executor``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal

from core.output_sink import OutputSink
from core.session_runner import SessionRunner
from core.tools.ask_user import (
    build_other_answer,
    find_pending_ask_user,
    inject_ask_user_answer,
)

logger = logging.getLogger(__name__)


@dataclass
class PreparedTurn:
    """一次回合的准备结果。"""

    runner: SessionRunner
    mode: Literal["run", "resume"]
    user_input: Any = None                      # mode == "run" 时的入参
    pending_ask_user_id: str | None = None      # mode == "resume" 时的占位 tool_use_id
    answer: str | None = None                   # mode == "resume" 时注入的答案


def prepare_turn(
    runner: SessionRunner,
    *,
    content: str,
    user_input: Any = None,
) -> PreparedTurn:
    """判定这一轮是「普通消息」还是「ask_user 的答案」。

    与 Web 既有行为一致：会话里若有待答的 ask_user 占位符，就把用户这次输入当作
    该问题的「其他」回复注入，**不追加新 user message**（避免 LLM 双重处理），
    然后恢复循环。

    注入有竞态：占位符可能已消失，此时回退为普通消息。
    """
    pending_id = find_pending_ask_user(runner.session) if content else None
    if pending_id:
        answer = build_other_answer(runner.session, pending_id, content)
        if inject_ask_user_answer(runner.session, pending_id, answer, runner.approval_store):
            return PreparedTurn(
                runner=runner,
                mode="resume",
                pending_ask_user_id=pending_id,
                answer=answer,
            )
        # 竞态：占位符已消失，回退普通消息
        logger.info("[turn_dispatch] ask_user 占位符已消失，回退为普通消息")

    return PreparedTurn(runner=runner, mode="run", user_input=user_input if user_input is not None else content)


def execute_turn(
    prepared: PreparedTurn,
    sink: OutputSink,
    *,
    on_ask_user_answered: Callable[[str, str], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    after_turn_hooks: bool = True,
) -> None:
    """同步执行一个回合（调用方负责放进 executor）。

    ``on_ask_user_answered(tool_use_id, answer)`` 在 resume **之前**调用，让接入端
    有机会先把「已应答」告知用户（Web 推 tool_result 帧以关闭问题卡片）。

    ``on_error(msg)`` 在异常且已落盘错误消息后调用，供接入端展示。

    ``after_turn_hooks=False`` 跳过记忆提取/git 自动提交。Web 的
    ``/answer-ask-user`` 端点历史上不做这两件事，抽取时按原样保留（避免重构
    顺手改变行为）；QQ 侧走 ``dispatch_turn`` 默认开启。
    """
    runner = prepared.runner
    try:
        if prepared.mode == "resume":
            if on_ask_user_answered is not None and prepared.pending_ask_user_id:
                on_ask_user_answered(prepared.pending_ask_user_id, prepared.answer or "")
            runner.resume_after_ask_user(sink=sink)
        else:
            runner.run(user_input=prepared.user_input, sink=sink)

        if after_turn_hooks:
            schedule_after_turn_hooks(runner)
    except Exception as e:  # noqa: BLE001 —— 任何异常都要落盘并告知接入端
        logger.error(f"master runner error: {e}")
        persist_turn_error(runner, e)
        if on_error is not None:
            on_error(str(e))


def schedule_after_turn_hooks(runner: SessionRunner) -> None:
    """回合后的收尾钩子：记忆提取 + git 自动提交（均后台线程，不阻塞）。"""
    _schedule_memory_extraction(runner)
    _schedule_git_commit(runner)


def _schedule_memory_extraction(runner: SessionRunner) -> None:
    """v3 记忆：回合结束后后台提取（不阻塞；失败只记日志）。"""
    try:
        sm = getattr(runner, "session", None)
        if sm is None:
            return
        from core.memory_pipeline import memory_enabled, schedule_extraction
        if memory_enabled(runner.workspace_uuid or ""):
            schedule_extraction(
                runner.workspace_uuid or "",
                runner.current_session_id or "",
                list(sm.messages),
            )
    except Exception:
        logger.exception("Failed to schedule memory extraction")


def _schedule_git_commit(runner: SessionRunner) -> None:
    """Git 版本管理：回合结束后自动提交并同步远程（后台线程，不阻塞）。"""
    try:
        from core.config import load_workspace_config
        workspace_cfg = load_workspace_config(runner.workspace_uuid or "")
        if not workspace_cfg.get("git_enabled", False):
            return
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


def persist_turn_error(runner: SessionRunner, exc: BaseException) -> None:
    """把回合异常落盘为 error_notice 消息（UI 可见但不发给 LLM）。"""
    try:
        sm = getattr(runner, "session", None)
        if sm is not None:
            sm.add_message("assistant", f"错误: {exc}", meta={"error_notice": True})
            sm.save()
    except Exception:
        logger.exception("Failed to persist error message")
