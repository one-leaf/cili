"""用量记账契约：各自记账 + 读取时聚合。

每个 runner 只把**自己的累计用量**写进 exec 日志 metadata.usage；
会话总量在读取时由 ``usage_total_for_session_dir`` 聚合（主会话 + 各子执行），
工具层不做任何用量转发。
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.session import SessionStore, usage_total_for_session_dir
from core.session_runner import SessionRunner
from core.usage import add_usage


class TestAddUsage:
    def test_merges_counters_and_models(self):
        target = {"input_tokens": 10, "models": {"m1": {"input_tokens": 10}}}
        add_usage(target, {
            "input_tokens": 5, "output_tokens": 2,
            "models": {"m2": {"input_tokens": 5}},
        })
        assert target["input_tokens"] == 15
        assert target["output_tokens"] == 2
        assert target["models"]["m1"]["input_tokens"] == 10
        assert target["models"]["m2"]["input_tokens"] == 5

    def test_ignores_speed_fields(self):
        """速度滚动平均不参与合并（子执行的速度不能代表父级）。"""
        target = {"prefill_speed": 100.0, "generation_speed": 20.0}
        add_usage(target, {"prefill_speed": 9999, "generation_speed": 9999,
                           "input_tokens": 1})
        assert target["prefill_speed"] == 100.0
        assert target["generation_speed"] == 20.0

    def test_bad_values_are_zero(self):
        target = {}
        add_usage(target, {"input_tokens": "12", "output_tokens": None})
        assert target["input_tokens"] == 12
        assert target["output_tokens"] == 0


def _write_exec(session_dir: Path, exec_id: str, usage: dict | None) -> None:
    exec_dir = session_dir / exec_id
    exec_dir.mkdir(parents=True, exist_ok=True)
    meta = {"status": "completed"}
    if usage is not None:
        meta["usage"] = usage
    (exec_dir / "index.json").write_text(
        json.dumps({"exec_id": exec_id, "metadata": meta, "messages": []}),
        encoding="utf-8",
    )


class TestSessionUsageTotal:
    def test_aggregates_master_and_sub_executions(self, tmp_path):
        session_dir = tmp_path / "sessions" / "s1"
        session_dir.mkdir(parents=True)
        _write_exec(session_dir, "exec_a", {"input_tokens": 100, "output_tokens": 10})
        _write_exec(session_dir, "exec_b", {"input_tokens": 50, "api_calls": 1,
                                            "models": {"lite": {"input_tokens": 50}}})

        total = usage_total_for_session_dir(
            session_dir, base={"input_tokens": 7, "models": {"master": {"input_tokens": 7}}},
        )
        assert total["input_tokens"] == 157
        assert total["output_tokens"] == 10
        assert total["api_calls"] == 1
        assert total["models"]["master"]["input_tokens"] == 7
        assert total["models"]["lite"]["input_tokens"] == 50

    def test_skips_broken_and_legacy_logs(self, tmp_path):
        session_dir = tmp_path / "sessions" / "s1"
        session_dir.mkdir(parents=True)
        _write_exec(session_dir, "exec_ok", {"input_tokens": 5})
        _write_exec(session_dir, "exec_legacy", None)  # 旧日志无 usage
        broken = session_dir / "exec_broken"
        broken.mkdir()
        (broken / "index.json").write_text("{not json", encoding="utf-8")

        total = usage_total_for_session_dir(session_dir, base={})
        assert total["input_tokens"] == 5

    def test_session_store_get_usage_total(self, tmp_path):
        store = SessionStore("s1", tmp_path / "sessions")
        store.update_usage(input_tokens=3)
        _write_exec(store.session_dir, "exec_a", {"input_tokens": 4})
        assert store.get_usage_total()["input_tokens"] == 7


class TestRunnerUsageRecord:
    def _runner(self, tmp_path, **kwargs):
        config = MagicMock()
        config.system.max_iterations = 20
        with patch("core.session_runner.create_tools") as tools, \
             patch("core.session_runner.create_llm_client") as client:
            tools.return_value = []
            client.return_value = MagicMock()
            runner = SessionRunner(config, role="worker", task="t", exec_id="exec_t", **kwargs)
        runner.session_dir = tmp_path
        runner.model = SimpleNamespace(name="mock-model")
        return runner

    def test_exec_log_records_own_usage(self, tmp_path):
        runner = self._runner(tmp_path)
        runner._update_usage(input_tokens=11, output_tokens=4, api_calls=1)
        runner._save_progress(1, phase="running", force=True)

        data = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
        usage = data["metadata"]["usage"]
        assert usage["input_tokens"] == 11
        assert usage["api_calls"] == 1

    def test_usage_base_seeds_continuation_epoch(self, tmp_path):
        """续任 epoch 从上一 epoch 累计值继续，日志里始终是 AgentRun 累计用量。"""
        base = {"input_tokens": 500, "output_tokens": 50,
                "models": {"va/model": {"input_tokens": 500, "output_tokens": 50}}}
        runner = self._runner(tmp_path, usage_base=base)
        runner._update_usage(input_tokens=10, output_tokens=2, model="va/model")
        runner._save_progress(1, phase="running", force=True)

        data = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
        usage = data["metadata"]["usage"]
        assert usage["input_tokens"] == 510
        assert usage["output_tokens"] == 52
        assert usage["models"]["va/model"]["input_tokens"] == 510
