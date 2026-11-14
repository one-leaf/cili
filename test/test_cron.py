"""Cron v2 测试：规则、迁移、种子、服务、引擎、投递。"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.cron import delivery, migration, rules, seeds, service, store
from core.cron import engine as engine_mod
from core.cron import sessions as cron_sessions
from core.cron.flags import cron_run_active
from core.cron.model import CronValidationError, Job, RunResult
from core.cron.sessions import ensure_cron_session
from core.session import SessionStore


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ─── rules ──────────────────────────────────────────────────────


class TestRules:
    def test_interval_first_run_uses_initial_delay(self):
        job = Job.from_dict({
            "id": "j", "name": "j",
            "schedule": {"type": "interval", "minutes": 60, "initial_delay_minutes": 5},
            "content": {"task": "x"},
        })
        now = datetime(2026, 11, 14, 10, 0, 0)
        assert rules.compute_next_run(job, None, now=now) == datetime(2026, 11, 14, 10, 5, 0)

    def test_interval_after_base(self):
        job = Job.from_dict({
            "id": "j", "name": "j",
            "schedule": {"type": "interval", "minutes": 30},
            "content": {"task": "x"},
        })
        base = datetime(2026, 11, 14, 10, 0, 0)
        assert rules.compute_next_run(job, base) == datetime(2026, 11, 14, 10, 30, 0)

    def test_cron_next_occurrence(self):
        job = Job.from_dict({
            "id": "j", "name": "j",
            "schedule": {"type": "cron", "expr": "0 9 * * *"},
            "content": {"task": "x"},
        })
        base = rules.parse_iso("2026-11-14T08:30:00")
        assert rules.compute_next_run(job, base) == datetime(2026, 11, 14, 9, 0, 0)

    def test_cron_timezone_offset(self):
        job = Job.from_dict({
            "id": "j", "name": "j",
            "schedule": {"type": "cron", "expr": "0 9 * * *", "timezone": "Asia/Shanghai"},
            "content": {"task": "x"},
        })
        base = rules.parse_iso("2026-11-14T08:30:00+08:00")
        assert rules.compute_next_run(job, base).isoformat() == "2026-11-14T09:00:00+08:00"

    def test_dst_gap_is_skipped(self):
        job = Job.from_dict({
            "id": "j", "name": "j",
            "schedule": {
                "type": "cron",
                "expr": "30 2 * * *",
                "timezone": "America/New_York",
            },
            "content": {"task": "x"},
        })
        # 2026-03-08 02:30  America/New_York 不存在（春季跳变）
        base = rules.parse_iso("2026-03-08T00:00:00-05:00")
        nxt = rules.compute_next_run(job, base)
        assert nxt.isoformat() == "2026-03-09T02:30:00-04:00"

    def test_invalid_cron_rejected(self):
        with pytest.raises(CronValidationError):
            Job.from_dict({
                "id": "j", "name": "j",
                "schedule": {"type": "cron", "expr": "bad expr"},
                "content": {"task": "x"},
            })


# ─── migration ──────────────────────────────────────────────────


class TestMigration:
    def _legacy(self, cron_env, tasks, state=None):
        legacy = Path(cron_env["workspace_dir"]) / ".cili" / "cron.d"
        (legacy / "state").mkdir(parents=True)
        _write_json(legacy / "user_tasks.json", tasks)
        if state:
            for name, payload in state.items():
                _write_json(legacy / "state" / f"{name}.json", payload)
        return legacy

    def test_migrate_success_and_delete_legacy(self, cron_env):
        ws = cron_env["workspace_uuid"]
        legacy = self._legacy(
            cron_env,
            [{
                "name": "daily-report",
                "description": "report",
                "enabled": True,
                "schedule": {"type": "interval", "minutes": 120},
                "one_time": False,
                "config": {"max_executions": 7},
                "content": {"task": "make report", "plan": ["a"]},
            }],
            {"daily-report": {"last_run": "2026-11-14T08:00:00", "run_count": 3,
                              "session_id": "sess-old", "remaining": 4}},
        )

        report = migration.migrate_workspace(ws)
        assert report["status"] == "migrated"
        root = store.cron_root(ws)
        assert (root / "daily-report" / "job.json").exists()
        assert (root / ".migrated.json").exists()
        job = store.load_job(ws, "daily-report")
        assert job.execution["max_executions"] == 7
        state = store.load_state(ws, "daily-report")
        assert state.remaining == 4 and state.run_count == 3
        assert state.cron_session_id == "sess-old"
        # 迁移不写 next_run；引擎首次加载时按 last_run 规则计算
        assert state.next_run is None
        engine_mod.CronEngine()._load_all()
        assert store.load_state(ws, "daily-report").next_run is not None
        assert not legacy.exists()

    def test_migration_is_idempotent(self, cron_env):
        ws = cron_env["workspace_uuid"]
        self._legacy(cron_env, [{
            "name": "t", "schedule": {"type": "interval", "minutes": 10},
            "content": {"task": "x"},
        }])
        assert migration.migrate_workspace(ws)["status"] == "migrated"
        assert migration.migrate_workspace(ws)["status"] == "already"

    def test_invalid_task_keeps_legacy(self, cron_env):
        ws = cron_env["workspace_uuid"]
        legacy = self._legacy(cron_env, [{
            "name": "bad", "schedule": {"type": "interval", "minutes": 0},
            "content": {"task": "x"},
        }])
        with pytest.raises(migration.v1.MigrationError):
            migration.migrate_workspace(ws)
        assert legacy.exists()
        assert not store.cron_root(ws).exists()

    def test_python_content_file_copied(self, cron_env):
        ws = cron_env["workspace_uuid"]
        legacy = self._legacy(cron_env, [{
            "name": "dyn", "schedule": {"type": "interval", "minutes": 10},
            "content": "task.py",
        }])
        (legacy / "task.py").write_text(
            "def get_tasks():\n    return [{'task': 'dynamic'}]\n", encoding="utf-8"
        )
        migration.migrate_workspace(ws)
        job = store.load_job(ws, "dyn")
        assert job.content == "task.py"
        assert (store.job_dir(ws, "dyn") / "task.py").exists()

    def test_system_state_migrated_to_system_workspace(self, cron_env):
        system_state = (
            Path(cron_env["root"]) / "system" / ".cili" / "cron.d" / "state"
        )
        _write_json(system_state / "memory-consolidation.json", {
            "last_run": "2026-11-14T08:00:00",
            "run_count": 2,
            "session_id": "sess-sys",
            "remaining": 10,
        })
        migration.migrate_system_states(["memory-consolidation"])
        state = store.load_state("system", "memory-consolidation")
        assert state.run_count == 2 and state.cron_session_id == "sess-sys"
        assert not (system_state / "memory-consolidation.json").exists()


# ─── seeds ──────────────────────────────────────────────────────


class TestSeeds:
    def test_install_and_idempotent(self, cron_env):
        first = seeds.install_seeds()
        assert {item["status"] for item in first} == {"installed"}
        second = seeds.install_seeds()
        assert {item["status"] for item in second} == {"unchanged"}
        job = store.load_job("system", "memory-consolidation")
        assert job.managed and job.scope == "global"
        assert job.seed_checksum

    def test_reinstall_after_delete(self, cron_env):
        seeds.install_seeds()
        store.delete_job("system", "office-pdf-sync")
        result = seeds.install_seeds()
        assert any(
            item["id"] == "office-pdf-sync" and item["status"] == "installed"
            for item in result
        )

    def test_corrupt_job_reinstalled(self, cron_env):
        seeds.install_seeds()
        path = store.job_path("system", "office-pdf-sync")
        path.write_text("{not json", encoding="utf-8")
        result = seeds.install_seeds()
        assert any(
            item["id"] == "office-pdf-sync" and item["status"] == "reinstalled"
            for item in result
        )
        assert store.load_job("system", "office-pdf-sync") is not None
        assert list(path.parent.glob("job.json.corrupt-*"))

    def test_revision_upgrade_keeps_state(self, cron_env, monkeypatch, tmp_path):
        seed_dir = tmp_path / "seeds"
        seed_dir.mkdir()
        for path in seeds.SEED_DIR.glob("*.json"):
            shutil.copy(path, seed_dir / path.name)
        monkeypatch.setattr(seeds, "SEED_DIR", seed_dir)

        seeds.install_seeds()
        state = store.load_state("system", "memory-consolidation")
        state.remaining = 5
        store.save_state("system", "memory-consolidation", state)

        path = seed_dir / "memory-consolidation.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["seed_revision"] = 2
        data["description"] = "v2 description"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        seeds.install_seeds()
        job = store.load_job("system", "memory-consolidation")
        assert job.description == "v2 description"
        assert job.seed_revision == 2
        assert store.load_state("system", "memory-consolidation").remaining == 5

    def test_checksum_drift_restored(self, cron_env):
        seeds.install_seeds()
        path = store.job_path("system", "memory-consolidation")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["description"] = "tampered"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        result = seeds.install_seeds()
        assert any(item["status"] == "restored" for item in result)
        assert store.load_job("system", "memory-consolidation").description != "tampered"


# ─── service ────────────────────────────────────────────────────


class TestService:
    def test_create_with_origin_and_paths(self, cron_env):
        ws = cron_env["workspace_uuid"]
        job = service.create_job(
            {
                "name": "New Task",
                "workspace_uuid": ws,
                "schedule": {"type": "interval", "minutes": 10},
                "content": {"task": "do"},
                "execution": {"timeout_seconds": 30},
            },
            origin={"workspace_uuid": ws, "session_id": "sess-1", "interface": "web"},
        )
        assert job.id == "new-task"
        assert job.delivery["mode"] == "origin"
        assert job.origin["session_id"] == "sess-1"
        assert store.job_path(ws, job.id).exists()
        state = store.load_state(ws, job.id)
        assert state.next_run and state.remaining == 9999

    def test_unique_id_suffix(self, cron_env):
        ws = cron_env["workspace_uuid"]
        first = service.create_job({
            "name": "same", "workspace_uuid": ws, "schedule": {"type": "interval", "minutes": 10},
            "content": {"task": "x"},
        })
        second = service.create_job({
            "name": "same", "workspace_uuid": ws, "schedule": {"type": "interval", "minutes": 10},
            "content": {"task": "x"},
        })
        assert first.id == "same"
        assert second.id == "same-2"

    def test_update_schedule_resets_next_run(self, cron_env):
        ws = cron_env["workspace_uuid"]
        job = service.create_job({
            "name": "u", "workspace_uuid": ws, "schedule": {"type": "interval", "minutes": 10},
            "content": {"task": "x"},
        })
        before = store.load_state(ws, job.id).next_run
        time.sleep(1.1)
        service.update_job(ws, job.id, {"schedule": {"type": "interval", "minutes": 60}})
        after = store.load_state(ws, job.id).next_run
        assert after != before
        assert service.get_job(ws, job.id).schedule["minutes"] == 60

    def test_enable_resets_remaining(self, cron_env):
        ws = cron_env["workspace_uuid"]
        job = service.create_job({
            "name": "e", "workspace_uuid": ws, "schedule": {"type": "interval", "minutes": 10},
            "content": {"task": "x"}, "execution": {"max_executions": 3},
        })
        service.set_enabled(ws, job.id, False)
        state = store.load_state(ws, job.id)
        state.remaining = 0
        store.save_state(ws, job.id, state)
        service.set_enabled(ws, job.id, True)
        assert store.load_state(ws, job.id).remaining == 3

    def test_delete(self, cron_env):
        ws = cron_env["workspace_uuid"]
        job = service.create_job({
            "name": "d", "workspace_uuid": ws, "schedule": {"type": "interval", "minutes": 10},
            "content": {"task": "x"},
        })
        service.delete_job(ws, job.id)
        assert store.load_job(ws, job.id) is None

    def test_managed_task_read_only(self, cron_env):
        seeds.install_seeds()
        with pytest.raises(service.CronError, match="只读"):
            service.set_enabled("system", "memory-consolidation", False)
        with pytest.raises(service.CronError, match="只读"):
            service.delete_job("system", "memory-consolidation")
        with pytest.raises(service.CronError, match="只读"):
            service.run_now("system", "memory-consolidation")


# ─── sessions / delivery ────────────────────────────────────────


class TestSessionsAndDelivery:
    def test_each_job_gets_its_own_session(self, cron_env):
        ws = cron_env["workspace_uuid"]
        job_a = service.create_job({
            "name": "a", "workspace_uuid": ws, "schedule": {"type": "interval", "minutes": 10},
            "content": {"task": "x"},
        })
        job_b = service.create_job({
            "name": "b", "workspace_uuid": ws, "schedule": {"type": "interval", "minutes": 10},
            "content": {"task": "x"},
        })
        state_a = store.load_state(ws, job_a.id)
        state_b = store.load_state(ws, job_b.id)
        session_a = ensure_cron_session(ws, job_a, state_a)
        session_b = ensure_cron_session(ws, job_b, state_b)
        assert session_a.session_id != session_b.session_id
        assert session_a.name == "[Cron] a"
        # 同一任务复用
        again = ensure_cron_session(ws, job_a, store.load_state(ws, job_a.id))
        assert again.session_id == session_a.session_id

    def test_deliver_to_origin_session(self, cron_env):
        ws = cron_env["workspace_uuid"]
        sessions_dir = Path(cron_env["workspace_dir"]) / ".cili" / "sessions"
        target = SessionStore.create_new_session(sessions_dir, "Target")
        job = service.create_job(
            {
                "name": "notify",
                "workspace_uuid": ws,
                "schedule": {"type": "interval", "minutes": 10},
                "content": {"task": "x"},
            },
            origin={"workspace_uuid": ws, "session_id": target.session_id, "interface": "web"},
        )
        state = store.load_state(ws, job.id)
        result = RunResult(
            job_id=job.id, slot="s1", status="ok", final_text="hello", duration_ms=1
        )
        receipt = delivery.deliver(job, state, result)
        assert receipt["status"] == "delivered"
        loaded = SessionStore.load_session(target.session_id, sessions_dir)
        assert loaded.messages[-1]["content"] == "hello"
        assert loaded.messages[-1]["_meta"]["cron_delivery"]["job_id"] == job.id

    def test_missing_target_falls_back_to_cron_session(self, cron_env):
        ws = cron_env["workspace_uuid"]
        job = service.create_job(
            {
                "name": "fallback",
                "workspace_uuid": ws,
                "schedule": {"type": "interval", "minutes": 10},
                "content": {"task": "x"},
            },
            origin={"workspace_uuid": ws, "session_id": "missing", "interface": "web"},
        )
        state = store.load_state(ws, job.id)
        receipt = delivery.deliver(
            job, state, RunResult(job_id=job.id, slot="s1", status="ok", final_text="text")
        )
        assert receipt["status"] == "fallback"
        assert state.cron_session_id
        cron_session = SessionStore.load_session(
            state.cron_session_id, cron_sessions.workspace_sessions_dir(ws)
        )
        assert cron_session is not None


# ─── engine ─────────────────────────────────────────────────────


class TestEngine:
    def _due_job(self, ws, **overrides):
        data = {
            "name": "due",
            "workspace_uuid": ws,
            "schedule": {"type": "interval", "minutes": 60},
            "content": {"task": "x"},
            "misfire": {"policy": "once", "grace_minutes": 120},
        }
        data.update(overrides)
        job = service.create_job(data)
        state = store.load_state(ws, job.id)
        state.next_run = (datetime.now() - timedelta(minutes=1)).isoformat()
        store.save_state(ws, job.id, state)
        return job

    def test_due_job_runs_and_records_history(self, cron_env, monkeypatch):
        ws = cron_env["workspace_uuid"]
        job = self._due_job(ws)
        calls: list[str] = []

        def fake_execute(job, state, slot, scheduled_iso, *, timeout_seconds):
            calls.append(slot)
            return RunResult(job_id=job.id, slot=slot, status="ok",
                             final_text="done", duration_ms=5)

        monkeypatch.setattr(engine_mod.executor, "execute", fake_execute)
        engine = engine_mod.CronEngine()
        engine._load_all()
        engine._tick(time.time())
        deadline = time.time() + 5
        while engine._queue_depth.get(ws, 0) and time.time() < deadline:
            time.sleep(0.02)
        state = store.load_state(ws, job.id)
        assert calls and calls[0].startswith("due@")
        assert state.last_status == "ok"
        assert state.history[0]["status"] == "ok"
        assert state.remaining == 9998
        engine.stop()

    def test_misfire_skip_does_not_execute(self, cron_env, monkeypatch):
        ws = cron_env["workspace_uuid"]
        job = self._due_job(ws, misfire={"policy": "skip", "grace_minutes": 120})
        state = store.load_state(ws, job.id)
        state.next_run = (datetime.now() - timedelta(hours=3)).isoformat()
        store.save_state(ws, job.id, state)
        calls: list[str] = []
        monkeypatch.setattr(
            engine_mod.executor,
            "execute",
            lambda *a, **k: calls.append("x"),
        )
        engine = engine_mod.CronEngine()
        engine._load_all()
        engine._tick(time.time())
        assert calls == []
        state = store.load_state(ws, job.id)
        assert state.missed_count == 1
        assert rules.parse_iso(state.next_run).timestamp() > time.time()
        engine.stop()

    def test_at_task_fires_once_and_is_deleted(self, cron_env, monkeypatch):
        ws = cron_env["workspace_uuid"]
        at_time = (datetime.now() - timedelta(seconds=5)).isoformat()
        job = service.create_job({
            "name": "once", "workspace_uuid": ws,
            "schedule": {"type": "at", "time": at_time},
            "content": {"task": "x"},
            "misfire": {"policy": "once", "grace_minutes": 120},
        })
        assert job.one_time is True
        calls: list[str] = []
        monkeypatch.setattr(
            engine_mod.executor,
            "execute",
            lambda job, state, slot, iso, *, timeout_seconds: (
                calls.append(slot) or RunResult(job_id=job.id, slot=slot, status="ok", final_text="done")
            ),
        )
        engine = engine_mod.CronEngine()
        engine._load_all()
        engine._tick(time.time())
        deadline = time.time() + 5
        while engine._queue_depth.get(ws, 0) and time.time() < deadline:
            time.sleep(0.02)
        assert calls
        assert store.load_job(ws, job.id) is None  # one-time 删除
        engine.stop()

    def test_running_slot_recovered_only_on_startup(self, cron_env):
        ws = cron_env["workspace_uuid"]
        job = service.create_job({
            "name": "recover", "workspace_uuid": ws,
            "schedule": {"type": "interval", "minutes": 60},
            "content": {"task": "x"},
        })
        state = store.load_state(ws, job.id)
        state.running = {"slot": "recover@x", "started_at": "2026-11-14 10:00:00"}
        store.save_state(ws, job.id, state)

        engine = engine_mod.CronEngine()
        engine._load_all()  # reload 路径：运行中的槽位保持，不误判 interrupted
        assert store.load_state(ws, job.id).running is not None

        engine._load_all(recover_running=True)  # 启动路径：标记 interrupted
        recovered = store.load_state(ws, job.id)
        assert recovered.running is None
        assert recovered.last_status == "interrupted"
        assert recovered.history[0]["status"] == "interrupted"
        engine.stop()

    def test_manual_run_is_extra_fire(self, cron_env, monkeypatch):
        ws = cron_env["workspace_uuid"]
        job = service.create_job({
            "name": "manual", "workspace_uuid": ws, "schedule": {"type": "interval", "minutes": 60},
            "content": {"task": "x"},
        })
        before = store.load_state(ws, job.id).next_run
        monkeypatch.setattr(
            engine_mod.executor,
            "execute",
            lambda job, state, slot, iso, *, timeout_seconds: RunResult(
                job_id=job.id, slot=slot, status="ok", final_text="manual"
            ),
        )
        engine = engine_mod.CronEngine()
        engine._load_all()
        assert engine.run_now(ws, job.id)
        deadline = time.time() + 5
        while engine._queue_depth.get(ws, 0) and time.time() < deadline:
            time.sleep(0.02)
        state = store.load_state(ws, job.id)
        assert state.next_run == before
        assert state.remaining == 9999
        assert any(item.get("extra") for item in state.history)
        engine.stop()


# ─── tool guards ────────────────────────────────────────────────


class TestTool:
    def _tool(self, ws):
        from core.tools.cron_tool import CronTool

        tool = CronTool(
            cwd=str(Path(ws) / "x"),
            workspace_uuid=ws,
            session=SimpleNamespace(session_id="sess-tool", session_dir=None),
        )
        return tool

    def test_recursion_guard(self, cron_env):
        ws = cron_env["workspace_uuid"]
        tool = self._tool(ws)
        token = cron_run_active.set(True)
        try:
            result = tool.execute(
                action="create",
                name="nested",
                schedule={"type": "interval", "minutes": 10},
                task="x",
            )
        finally:
            cron_run_active.reset(token)
        assert result.error
        assert "cron run" in result.output

    def test_managed_tool_action_rejected(self, cron_env):
        seeds.install_seeds()
        tool = self._tool(cron_env["workspace_uuid"])
        result = tool.execute(action="disable", name="memory-consolidation")
        assert result.error
        assert "只读" in result.output
