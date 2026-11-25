"""main.py 启动期环境自检与修复的单元测试（不触网、不启动服务）。"""

import json
from types import SimpleNamespace

import pytest

import main as main_mod


def _fake_result(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class TestFindBrokenImports:
    """导入探测：解析、失败容错与空输入。"""

    def test_parses_probe_result(self, monkeypatch):
        payload = [{
            "dist": "fastapi",
            "module": "fastapi",
            "error": "ModuleNotFoundError: No module named 'fastapi.routing'",
            "missing_dist": "fastapi",
        }]
        stdout = "some package noise\n" + main_mod._IMPORT_PROBE_MARKER + json.dumps(payload) + "\n"
        monkeypatch.setattr(main_mod.subprocess, "run", lambda *a, **kw: _fake_result(stdout=stdout))
        assert main_mod._find_broken_imports(["fastapi"]) == payload

    def test_probe_failure_is_fail_open(self, monkeypatch):
        monkeypatch.setattr(
            main_mod.subprocess, "run",
            lambda *a, **kw: _fake_result(returncode=1, stderr="traceback\nlast line"),
        )
        assert main_mod._find_broken_imports(["fastapi"]) == []

    def test_empty_input_skips_probe(self, monkeypatch):
        calls = []
        monkeypatch.setattr(main_mod.subprocess, "run", lambda *a, **kw: calls.append(a))
        assert main_mod._find_broken_imports([]) == []
        assert calls == []


class TestInstallPackagesSelfCheck:
    """名字齐全后的两道检查（pip check + 导入探测）。"""

    @staticmethod
    def _patch_clean(monkeypatch, probe_results, probe_calls=None):
        monkeypatch.setattr(main_mod, "_load_requirements", lambda: ["fastapi", "uvicorn[standard]"])
        monkeypatch.setattr(main_mod, "_check_installed_packages", lambda: {"fastapi": "1.0", "uvicorn": "1.0"})
        monkeypatch.setattr(main_mod, "_find_broken_packages", lambda pip_exe: [])
        monkeypatch.setattr(main_mod, "_save_pip_mirror", lambda mirror: None)
        results = iter(probe_results)

        def fake_probe(names):
            if probe_calls is not None:
                probe_calls.append(list(names))
            return next(results)

        monkeypatch.setattr(main_mod, "_find_broken_imports", fake_probe)

    def test_clean_environment_touches_nothing(self, monkeypatch):
        installed_calls = []
        monkeypatch.setattr(
            main_mod, "_pip_install_one",
            lambda *a, **kw: installed_calls.append(a) or (True, 0),
        )
        probe_calls = []
        self._patch_clean(monkeypatch, [[]], probe_calls)

        assert main_mod._install_packages([""]) == (True, False)
        assert installed_calls == []
        # 常规启动只探关键路径，不做全量导入
        assert probe_calls == [list(main_mod._STARTUP_CRITICAL_DISTS)]

    def test_broken_import_force_reinstalled(self, monkeypatch):
        failure = {"dist": "fastapi", "module": "fastapi", "error": "ImportError: DLL load failed"}
        self._patch_clean(monkeypatch, [[failure], []])
        install_calls = []
        monkeypatch.setattr(
            main_mod, "_pip_install_one",
            lambda pip_exe, pkg, mirrors, idx, extra_args=(): (
                install_calls.append((pkg, extra_args)) or (True, idx)
            ),
        )

        ok, installed_new = main_mod._install_packages([""])
        assert ok is True
        assert installed_new is True  # 重装过 → 调用方重启进程
        assert install_calls == [
            ("fastapi", ("--force-reinstall", "--no-deps", "--no-cache-dir"))
        ]

    def test_missing_module_owner_also_reinstalled(self, monkeypatch):
        failure = {
            "dist": "pydantic",
            "module": "pydantic",
            "error": "ModuleNotFoundError: No module named 'pydantic_core'",
            "missing_dist": "pydantic-core",
        }
        self._patch_clean(monkeypatch, [[failure], []])
        install_calls = []
        monkeypatch.setattr(
            main_mod, "_pip_install_one",
            lambda pip_exe, pkg, mirrors, idx, extra_args=(): (
                install_calls.append(pkg) or (True, idx)
            ),
        )

        assert main_mod._install_packages([""]) == (True, True)
        assert install_calls == ["pydantic", "pydantic-core"]

    def test_critical_import_still_failing_raises(self, monkeypatch):
        failure = {"dist": "fastapi", "module": "fastapi", "error": "ImportError: broken"}
        self._patch_clean(monkeypatch, [[failure], [failure]])
        monkeypatch.setattr(main_mod, "_pip_install_one", lambda *a, **kw: (True, 0))
        monkeypatch.setattr(main_mod, "_running_in_deps", True)

        # 服务运行在 deps Python 上且关键包仍不可导入 → 报环境损坏（启动器重建）
        with pytest.raises(main_mod._EnvironmentBrokenError) as excinfo:
            main_mod._install_packages([""])
        assert excinfo.value.failures == [failure]

    def test_broken_managed_env_does_not_block_other_runtime(self, monkeypatch):
        failure = {"dist": "fastapi", "module": "fastapi", "error": "ImportError: broken"}
        self._patch_clean(monkeypatch, [[failure], [failure]])
        monkeypatch.setattr(main_mod, "_pip_install_one", lambda *a, **kw: (True, 0))
        monkeypatch.setattr(main_mod, "_running_in_deps", False)

        # 服务跑在别的解释器（如系统 Python）上：managed 工具环境损坏只告警
        assert main_mod._install_packages([""]) == (True, False)

    def test_optional_package_failure_only_warns(self, monkeypatch):
        failure = {"dist": "qrcode", "module": "qrcode", "error": "ImportError: broken"}
        self._patch_clean(monkeypatch, [[failure], [failure]])
        monkeypatch.setattr(main_mod, "_pip_install_one", lambda *a, **kw: (True, 0))

        # 可选包失败不阻断启动（也不重启），深探测模式下同样如此
        assert main_mod._install_packages([""], deep=True) == (True, False)

    def test_deep_mode_probes_all_requirements(self, monkeypatch):
        probe_calls = []
        self._patch_clean(monkeypatch, [[]], probe_calls)

        assert main_mod._install_packages([""], deep=True) == (True, False)
        assert probe_calls == [["fastapi", "uvicorn"]]


class TestEnvironmentErrorDetection:
    """启动异常是否属于环境问题。"""

    def test_module_not_found(self):
        assert main_mod._is_environment_error(ModuleNotFoundError("No module named 'fastapi'"))

    def test_wrapped_cause(self):
        try:
            try:
                raise ImportError("DLL load failed while importing pydantic_core")
            except ImportError as inner:
                raise RuntimeError("startup failed") from inner
        except RuntimeError as exc:
            assert main_mod._is_environment_error(exc)

    def test_plain_error_is_not_environment_error(self):
        assert not main_mod._is_environment_error(ValueError("bad setting.json"))


class TestRecoverEnvironment:
    """启动异常后的修复与重启。"""

    def test_reexecs_deps_python_with_force_repair(self, monkeypatch):
        monkeypatch.setenv(main_mod._ENV_REPAIR_ENV, "0")
        monkeypatch.delenv(main_mod._ENV_FORCE_REPAIR_ENV, raising=False)
        monkeypatch.setattr(main_mod.os.path, "exists", lambda path: True)
        execs = []
        monkeypatch.setattr(
            main_mod.os, "execv",
            lambda exe, argv: execs.append((exe, argv)),
        )

        main_mod._recover_environment(ModuleNotFoundError("No module named 'fastapi'"))
        assert len(execs) == 1
        assert execs[0][0] == main_mod._deps_python_exe
        # 修复在新进程里做：带上深修复标记，并记录已尝试次数
        assert main_mod.os.environ[main_mod._ENV_FORCE_REPAIR_ENV] == "1"
        assert main_mod.os.environ[main_mod._ENV_REPAIR_ENV] == "1"

    def test_stops_after_limit(self, monkeypatch):
        monkeypatch.setenv(main_mod._ENV_REPAIR_ENV, str(main_mod._MAX_ENV_REPAIRS))
        execs = []
        monkeypatch.setattr(main_mod.os, "execv", lambda *a: execs.append(a))

        main_mod._recover_environment(ModuleNotFoundError("x"))
        assert execs == []


class TestHandleBrokenEnvironment:
    """环境损坏的处理：启动器托管时交接重建，否则提示人工处理。"""

    def _exc(self):
        return main_mod._EnvironmentBrokenError([{
            "dist": "fastapi",
            "module": "fastapi",
            "error": "ModuleNotFoundError: No module named 'fastapi.routing'",
        }])

    def test_launcher_managed_exits_with_rebuild_code(self, monkeypatch):
        import core.restart as restart_mod
        monkeypatch.setattr(restart_mod, "is_launcher_managed", lambda: True)
        with pytest.raises(SystemExit) as excinfo:
            main_mod._handle_broken_environment(self._exc())
        assert excinfo.value.code == restart_mod.ENV_REBUILD_EXIT_CODE

    def test_direct_run_prints_remediation_and_exits(self, monkeypatch):
        import core.restart as restart_mod
        monkeypatch.setattr(restart_mod, "is_launcher_managed", lambda: False)
        with pytest.raises(SystemExit) as excinfo:
            main_mod._handle_broken_environment(self._exc())
        assert excinfo.value.code == 1


class TestSwitchToDepsPython:
    """系统解释器缺依赖时的运行时切换。"""

    def test_noop_when_already_in_deps(self, monkeypatch):
        monkeypatch.setattr(main_mod, "_running_in_deps", True)
        execs = []
        monkeypatch.setattr(main_mod.os, "execv", lambda *a: execs.append(a))
        main_mod._switch_to_deps_python_if_needed()
        assert execs == []

    def test_noop_when_deps_python_missing(self, monkeypatch):
        monkeypatch.setattr(main_mod, "_running_in_deps", False)
        monkeypatch.setattr(main_mod, "_deps_python_exe", "Z:/nonexistent/python.exe")
        execs = []
        monkeypatch.setattr(main_mod.os, "execv", lambda *a: execs.append(a))
        main_mod._switch_to_deps_python_if_needed()
        assert execs == []
