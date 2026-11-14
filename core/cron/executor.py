"""Cron 执行器：每任务独立会话 + 完整 agent 回合 + 超时。"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from core.config import get_workspace_data_dir, load_config, load_workspace_config
from core.cron.flags import cron_run_active
from core.cron.model import Job, RunResult, State
from core.cron.sessions import ensure_cron_session
from core.session_runner import SessionRunner

logger = logging.getLogger(__name__)

_MAX_FINAL_TEXT = 20_000
_STOP_GRACE_SECONDS = 10

_runner_lock = threading.Lock()
_runners: dict[str, SessionRunner] = {}
_poisoned: set[str] = set()


def is_poisoned(workspace_uuid: str) -> bool:
    with _runner_lock:
        return workspace_uuid in _poisoned


def any_running() -> bool:
    with _runner_lock:
        runners = list(_runners.values())
    for runner in runners:
        try:
            if runner.is_running():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def clear_poison(workspace_uuid: str | None = None) -> None:
    with _runner_lock:
        if workspace_uuid is None:
            _poisoned.clear()
        else:
            _poisoned.discard(workspace_uuid)


def _workspace_cwd(workspace_uuid: str) -> str:
    ws_config = load_workspace_config(workspace_uuid)
    if ws_config and ws_config.get("directory"):
        return str(ws_config["directory"])
    return str(Path(get_workspace_data_dir(workspace_uuid)).parent)


def _get_runner(workspace_uuid: str) -> SessionRunner | None:
    with _runner_lock:
        if workspace_uuid in _poisoned:
            return None
        runner = _runners.get(workspace_uuid)
        if runner is not None:
            return None if runner.is_running() else runner
        try:
            runner = SessionRunner(
                load_config(),
                role="master",
                cwd=_workspace_cwd(workspace_uuid),
                workspace_uuid=workspace_uuid,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[cron-exec] failed to create runner for %s: %s", workspace_uuid, exc)
            return None
        _runners[workspace_uuid] = runner
        logger.info("[cron-exec] runner created for workspace %s", workspace_uuid or "default")
        return runner


def _content_items(content: Any, job: Job | None = None) -> list[dict[str, Any]]:
    if isinstance(content, dict):
        return [content]
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    if isinstance(content, str) and content.strip():
        text = content.strip()
        if job is not None and text.endswith(".py"):
            from core.cron import store

            path = store.job_dir(job.workspace_uuid or "system", job.id) / Path(text).name
            if path.exists():
                try:
                    import importlib.util

                    spec = importlib.util.spec_from_file_location(
                        f"cron_task_{job.id}", path
                    )
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        fn = getattr(module, "get_tasks", None)
                        if callable(fn):
                            result = fn()
                            items = result if isinstance(result, list) else [result]
                            normalized = [
                                item for item in items if isinstance(item, dict)
                            ]
                            if normalized:
                                return normalized
                except Exception as exc:  # noqa: BLE001
                    logger.error("[cron-exec] dynamic task file failed: %s", exc)
        return [{"task": text, "plan": []}]
    return []


def _build_prompt(
    job: Job, state: State, item: dict[str, Any], scheduled_iso: str
) -> str:
    lines = [
        "[定时任务触发]",
        "",
        f"任务：{job.name}" + (f"（{job.description}）" if job.description else ""),
        f"计划时间：{scheduled_iso}",
        f"执行次数：第 {state.run_count + 1} 次",
        "",
        "## 任务",
        str(item.get("task") or item.get("description") or job.description or job.name),
    ]
    plan = item.get("plan") or []
    if plan:
        lines.extend(["", "## 执行计划"])
        for index, step in enumerate(plan, 1):
            lines.append(f"{index}. {step}")
    lines.extend(
        [
            "",
            "## 输出约定",
            "- 完成后用一段简洁的中文总结本次结果（将作为通知投递给用户）。",
            "- 如果本次没有值得通知的内容，只输出 [SILENT]。",
            "- 如果任务失败，输出以 [CRON_FAILURE] 开头并说明原因。",
        ]
    )
    return "\n".join(lines)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(part for part in parts if part)
    return ""


def _extract_final_text(runner: SessionRunner, start_index: int) -> str:
    session = getattr(runner, "session", None)
    if session is None:
        return ""
    texts: list[str] = []
    for message in session.messages[start_index:]:
        if message.get("role") != "assistant":
            continue
        if message.get("_meta", {}).get("valid") is False:
            continue
        text = _message_text(message).strip()
        if text:
            texts.append(text)
    return "\n\n".join(texts)[:_MAX_FINAL_TEXT]


def _run_with_timeout(
    runner: SessionRunner, prompt: str, timeout_seconds: int
) -> tuple[bool, BaseException | None]:
    """返回 (timed_out, error)。"""
    holder: dict[str, BaseException | None] = {"error": None}

    def _worker() -> None:
        token = cron_run_active.set(True)
        try:
            runner.run(prompt, streaming=False)
        except BaseException as exc:  # noqa: BLE001 —— 记录后交给主线程判断
            holder["error"] = exc
        finally:
            cron_run_active.reset(token)

    thread = threading.Thread(target=_worker, daemon=True, name="CronRun")
    thread.start()
    if timeout_seconds and timeout_seconds > 0:
        thread.join(timeout_seconds)
    else:
        thread.join()

    if thread.is_alive():
        logger.warning("[cron-exec] run exceeded %ss, stopping runner", timeout_seconds)
        try:
            runner.stop()
        except Exception:  # noqa: BLE001
            pass
        thread.join(_STOP_GRACE_SECONDS)
        if thread.is_alive():
            logger.error(
                "[cron-exec] runner did not stop after timeout; workspace poisoned"
            )
            with _runner_lock:
                _poisoned.add(runner.workspace_uuid)
                _runners.pop(runner.workspace_uuid, None)
            return True, RuntimeError("runner did not stop after timeout")
        return True, None

    return False, holder["error"]


def execute(
    job: Job,
    state: State,
    slot: str,
    scheduled_iso: str,
    *,
    timeout_seconds: int,
) -> RunResult:
    started = time.monotonic()
    workspace_uuid = job.workspace_uuid or "system"

    runner = _get_runner(workspace_uuid)
    if runner is None:
        return RunResult(
            job_id=job.id,
            slot=slot,
            status="skipped",
            error="runner busy or poisoned",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        session = ensure_cron_session(workspace_uuid, job, state)
        if runner.current_session_id != session.session_id:
            runner.switch_session(session.session_id)
        if runner.current_session_id != session.session_id:
            raise RuntimeError(f"failed to switch to cron session {session.session_id}")

        if str(job.execution.get("context") or "fresh") == "fresh":
            runner.invalidate_all_messages()

        items = _content_items(job.content, job)
        if not items:
            return RunResult(
                job_id=job.id,
                slot=slot,
                status="error",
                error="job content is empty",
                duration_ms=int((time.monotonic() - started) * 1000),
                session_id=session.session_id,
            )

        texts: list[str] = []
        error: BaseException | None = None
        timed_out = False
        for item in items:
            start_index = len(runner.session.messages)
            prompt = _build_prompt(job, state, item, scheduled_iso)
            timed_out, error = _run_with_timeout(runner, prompt, timeout_seconds)
            texts.append(_extract_final_text(runner, start_index))
            if timed_out or error is not None:
                break

        try:
            runner.session.save()
        except Exception:  # noqa: BLE001
            logger.exception("[cron-exec] session save failed")

        final_text = "\n\n".join(text for text in texts if text).strip()
        if final_text == "[SILENT]":
            final_text = ""
        status = "ok"
        if timed_out:
            status = "timeout"
        elif error is not None:
            status = "error"
        elif final_text.startswith("[CRON_FAILURE]"):
            status = "error"
            error = RuntimeError(final_text.splitlines()[0])
            final_text = final_text[len("[CRON_FAILURE]") :].strip()

        return RunResult(
            job_id=job.id,
            slot=slot,
            status=status,
            final_text=final_text,
            error=str(error) if error is not None else None,
            duration_ms=int((time.monotonic() - started) * 1000),
            session_id=session.session_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[cron-exec] task %s failed", job.id)
        return RunResult(
            job_id=job.id,
            slot=slot,
            status="error",
            error=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def shutdown() -> None:
    with _runner_lock:
        runners = list(_runners.values())
        _runners.clear()
    for runner in runners:
        try:
            runner.stop()
        except Exception:  # noqa: BLE001
            pass
