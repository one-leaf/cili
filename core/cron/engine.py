"""调度引擎：事件驱动唤醒、槽位认领、错过触发、并发队列。"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from core.config import get_all_workspace_uuids
from core.cron import delivery, executor, rules, store
from core.cron.model import Job, RunResult, State, now_str

logger = logging.getLogger(__name__)

_POLL_FALLBACK_SECONDS = 60
_MAX_QUEUE_PER_WORKSPACE = 8
_MAX_GLOBAL_CONCURRENCY = 4


@dataclass
class LoadedJob:
    job: Job
    state: State
    next_ts: float | None = None


@dataclass
class _Dispatch:
    job: Job = field(repr=False)
    state: State = field(repr=False)
    slot: str = ""
    scheduled_iso: str = ""
    extra: bool = False


class CronEngine:
    def __init__(self) -> None:
        self._jobs: dict[tuple[str, str], LoadedJob] = {}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._queue_depth: dict[str, int] = {}
        self._global_sem = threading.BoundedSemaphore(_MAX_GLOBAL_CONCURRENCY)

    # ─── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._load_all(recover_running=True)
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="CronEngine"
        )
        self._thread.start()
        logger.info("[cron] engine started with %d task(s)", len(self._jobs))

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        with self._lock:
            executors = list(self._executors.values())
            self._executors.clear()
        for pool in executors:
            pool.shutdown(wait=False, cancel_futures=True)
        executor.shutdown()
        logger.info("[cron] engine stopped")

    def reload(self) -> None:
        self._load_all()
        self._wake.set()

    def _load_all(self, *, recover_running: bool = False) -> None:
        with self._lock:
            loaded: dict[tuple[str, str], LoadedJob] = {}
            for workspace_uuid in get_all_workspace_uuids():
                for job in store.list_jobs(workspace_uuid):
                    try:
                        state = store.load_state(workspace_uuid, job.id)
                        changed = self._recover_and_init(
                            workspace_uuid, job, state, recover_running=recover_running
                        )
                        if changed:
                            store.save_state(workspace_uuid, job.id, state)
                        loaded[(workspace_uuid, job.id)] = LoadedJob(
                            job=job,
                            state=state,
                            next_ts=self._state_next_ts(job, state),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "[cron] failed to load %s/%s: %s",
                            workspace_uuid,
                            job.id,
                            exc,
                        )
            self._jobs = loaded

    def _recover_and_init(
        self, workspace_uuid: str, job: Job, state: State, *, recover_running: bool = False
    ) -> bool:
        changed = False
        if state.remaining is None:
            state.remaining = int(job.execution.get("max_executions", 9999))
            changed = True
        if state.running and recover_running:
            slot = str(state.running.get("slot") or "")
            state.push_history(
                {
                    "slot": slot,
                    "started_at": state.running.get("started_at"),
                    "duration_ms": None,
                    "status": "interrupted",
                    "error": "process exited while task was running",
                    "delivery": None,
                },
                int(job.execution.get("max_runs_kept", 20)),
            )
            state.running = None
            state.last_status = "interrupted"
            logger.warning("[cron] %s/%s: recovered interrupted slot %s", workspace_uuid, job.id, slot)
            changed = True
        if state.next_run:
            try:
                rules.parse_iso(state.next_run, rules.timezone_for(job))
            except Exception:  # noqa: BLE001 —— 损坏的 next_run 重新计算
                logger.warning(
                    "[cron] %s/%s: invalid next_run %r, recomputing",
                    workspace_uuid,
                    job.id,
                    state.next_run,
                )
                state.next_run = None
                changed = True
        if state.next_run is None:
            try:
                base = (
                    rules.parse_iso(state.last_run, rules.timezone_for(job))
                    if state.last_run
                    else None
                )
            except Exception:  # noqa: BLE001
                base = None
            next_dt = rules.compute_next_run(job, base)
            state.next_run = next_dt.isoformat()
            changed = True
        return changed

    def _state_next_ts(self, job: Job, state: State) -> float | None:
        if not state.next_run:
            return None
        try:
            return rules.parse_iso(state.next_run, rules.timezone_for(job)).timestamp()
        except Exception:  # noqa: BLE001
            logger.warning("[cron] invalid next_run %r for %s", state.next_run, job.id)
            return None

    # ─── loop ───────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            try:
                self._tick(now)
            except Exception:  # noqa: BLE001
                logger.exception("[cron] scheduler tick failed")
            timeout = self._sleep_timeout()
            self._wake.wait(timeout)
            self._wake.clear()

    def _sleep_timeout(self) -> float:
        with self._lock:
            future = [
                item.next_ts
                for item in self._jobs.values()
                if item.job.enabled and item.next_ts is not None
            ]
        if not future:
            return _POLL_FALLBACK_SECONDS
        delta = min(future) - time.time()
        return max(1.0, min(_POLL_FALLBACK_SECONDS, delta))

    def _tick(self, now: float) -> None:
        with self._lock:
            items = list(self._jobs.items())
        for key, loaded in items:
            if self._stop.is_set():
                return
            if not loaded.job.enabled or loaded.next_ts is None or loaded.next_ts > now:
                continue
            workspace_uuid, _ = key
            if executor.is_poisoned(workspace_uuid):
                logger.debug("[cron] skip %s: workspace poisoned", loaded.job.id)
                continue
            self._handle_due(key, loaded, now)

    def _handle_due(self, key: tuple[str, str], loaded: LoadedJob, now: float) -> None:
        job = loaded.job
        state = loaded.state
        grace = int(job.misfire.get("grace_minutes", 120)) * 60
        overdue = max(0.0, now - float(loaded.next_ts or now))

        policy = str(job.misfire.get("policy") or "once")
        if job.schedule.get("type") == "at" and (overdue > grace or policy == "skip"):
            with self._lock:
                missed_slot = str(state.next_run)
                job.enabled = False
                state.missed_count += 1
                state.last_status = "missed"
                state.next_run = None
                state.push_history(
                    {
                        "slot": missed_slot,
                        "started_at": None,
                        "duration_ms": 0,
                        "status": "missed",
                        "error": "one-shot task expired before execution",
                        "delivery": None,
                    },
                    int(job.execution.get("max_runs_kept", 20)),
                )
                store.save_job(job)
                store.save_state(job.workspace_uuid, job.id, state)
                loaded.next_ts = None
            logger.warning("[cron] one-shot task %s expired, disabled", job.id)
            return

        if overdue > grace or policy == "skip":
            with self._lock:
                state.missed_count += 1
                next_dt = rules.compute_next_run(job, rules.ts_to_now_for(job))
                state.next_run = next_dt.isoformat()
                loaded.next_ts = next_dt.timestamp()
                store.save_state(job.workspace_uuid, job.id, state)
            logger.info(
                "[cron] %s missed a slot (overdue %.0fs), skipped", job.id, overdue
            )
            return

        if self._queue_depth.get((job.workspace_uuid or "system"), 0) >= _MAX_QUEUE_PER_WORKSPACE:
            with self._lock:
                state.push_history(
                    {
                        "slot": str(state.next_run),
                        "started_at": None,
                        "duration_ms": 0,
                        "status": "skipped_busy",
                        "error": "workspace queue is full",
                        "delivery": None,
                    },
                    int(job.execution.get("max_runs_kept", 20)),
                )
                next_dt = rules.compute_next_run(job, rules.ts_to_now_for(job))
                state.next_run = next_dt.isoformat()
                loaded.next_ts = next_dt.timestamp()
                store.save_state(job.workspace_uuid, job.id, state)
            return

        dispatch = self._claim(key, loaded)
        if dispatch is None:
            return
        self._submit(dispatch, now)

    def _claim(self, key: tuple[str, str], loaded: LoadedJob) -> _Dispatch | None:
        job = loaded.job
        state = loaded.state
        with self._lock:
            if state.running:
                return None
            scheduled_iso = str(state.next_run or now_str())
            try:
                scheduled_dt = rules.parse_iso(
                    scheduled_iso, rules.timezone_for(job)
                )
            except Exception:  # noqa: BLE001
                scheduled_dt = rules.ts_to_now_for(job)
            slot = f"{job.id}@{scheduled_iso}"
            state.running = {"slot": slot, "started_at": now_str()}
            state.last_slot = slot
            if job.schedule.get("type") == "at":
                state.next_run = None
                loaded.next_ts = None
            else:
                next_dt = rules.compute_next_run(job, scheduled_dt)
                state.next_run = next_dt.isoformat()
                loaded.next_ts = next_dt.timestamp()
            store.save_state(job.workspace_uuid, job.id, state)
            return _Dispatch(
                job=job,
                state=state,
                slot=slot,
                scheduled_iso=scheduled_iso,
            )

    def _submit(self, dispatch: _Dispatch, now: float | None = None) -> None:
        workspace_uuid = dispatch.job.workspace_uuid or "system"
        pool = self._executor_for(workspace_uuid)
        self._queue_depth[workspace_uuid] = self._queue_depth.get(workspace_uuid, 0) + 1

        def _run() -> None:
            try:
                with self._global_sem:
                    result = executor.execute(
                        dispatch.job,
                        dispatch.state,
                        dispatch.slot,
                        dispatch.scheduled_iso,
                        timeout_seconds=int(
                            dispatch.job.execution.get("timeout_seconds", 900)
                        ),
                    )
                self._finish(dispatch, result)
            finally:
                self._queue_depth[workspace_uuid] = max(
                    0, self._queue_depth.get(workspace_uuid, 1) - 1
                )

        pool.submit(_run)

    def _executor_for(self, workspace_uuid: str) -> ThreadPoolExecutor:
        with self._lock:
            pool = self._executors.get(workspace_uuid)
            if pool is None:
                pool = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix=f"Cron-{workspace_uuid or 'default'}",
                )
                self._executors[workspace_uuid] = pool
            return pool

    # ─── finish ─────────────────────────────────────────────────

    def _finish(self, dispatch: _Dispatch, result: RunResult) -> None:
        job = dispatch.job
        workspace_uuid = job.workspace_uuid or "system"
        key = (workspace_uuid, job.id)
        with self._lock:
            loaded = self._jobs.get(key)
            if loaded is None:
                logger.info("[cron] %s removed during run, dropping result", job.id)
                return
            state = store.load_state(workspace_uuid, job.id)
            state.running = None
            state.last_run = now_str()
            state.last_status = result.status
            state.last_error = result.error
            state.last_duration_ms = result.duration_ms

            if result.status != "skipped" and not dispatch.extra:
                state.run_count += 1
                remaining = (
                    state.remaining
                    if state.remaining is not None
                    else int(job.execution.get("max_executions", 9999))
                )
                state.remaining = remaining - 1
                if state.remaining <= 0:
                    job.enabled = False
                    store.save_job(job)
                    logger.info("[cron] %s reached max_executions, disabled", job.id)

            delivery_receipt = delivery.deliver(job, state, result)
            state.last_delivery = delivery_receipt

            state.push_history(
                {
                    "slot": dispatch.slot,
                    "started_at": (dispatch.state.running or {}).get("started_at"),
                    "duration_ms": result.duration_ms,
                    "status": result.status,
                    "error": result.error,
                    "delivery": delivery_receipt.get("status"),
                    "extra": dispatch.extra,
                },
                int(job.execution.get("max_runs_kept", 20)),
            )
            store.save_state(workspace_uuid, job.id, state)
            loaded.state = state
            loaded.next_ts = self._state_next_ts(job, state)

            if job.one_time and result.status != "skipped":
                store.delete_job(workspace_uuid, job.id)
                self._jobs.pop(key, None)
                logger.info("[cron] one-time task %s deleted after run", job.id)

    # ─── public API ─────────────────────────────────────────────

    def run_now(self, workspace_uuid: str, job_id: str) -> bool:
        """手动触发：extra fire，不消耗 remaining、不影响 next_run。"""
        key = (workspace_uuid, job_id)
        with self._lock:
            loaded = self._jobs.get(key)
            if loaded is None or loaded.state.running:
                return False
            state = loaded.state
            state.running = {
                "slot": f"{job_id}@manual-{now_str()}",
                "started_at": now_str(),
                "extra": True,
            }
            store.save_state(workspace_uuid, job_id, state)
            dispatch = _Dispatch(
                job=loaded.job,
                state=state,
                slot=str(state.running["slot"]),
                scheduled_iso=now_str(),
                extra=True,
            )
        self._submit(dispatch)
        return True

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._jobs.values())
        result: list[dict[str, Any]] = []
        for loaded in items:
            item = loaded.job.to_dict()
            item["state"] = {
                "next_run": loaded.state.next_run,
                "last_run": loaded.state.last_run,
                "last_status": loaded.state.last_status,
                "last_error": loaded.state.last_error,
                "run_count": loaded.state.run_count,
                "remaining": loaded.state.remaining,
                "running": loaded.state.running,
                "missed_count": loaded.state.missed_count,
                "last_delivery": loaded.state.last_delivery,
            }
            item["schedule_summary"] = rules.schedule_summary(loaded.job)
            result.append(item)
        result.sort(key=lambda item: (item.get("workspace_uuid") or "", item["id"]))
        return result

    def get_task(self, workspace_uuid: str, job_id: str) -> LoadedJob | None:
        with self._lock:
            return self._jobs.get((workspace_uuid, job_id))

    def history(self, workspace_uuid: str, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
        state = store.load_state(workspace_uuid, job_id)
        return state.history[: max(1, min(int(limit), 100))]

    def any_running(self) -> bool:
        with self._lock:
            return any(item.state.running for item in self._jobs.values()) or executor.any_running()

    def wake(self) -> None:
        self._wake.set()


_engine: CronEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> CronEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = CronEngine()
        return _engine
