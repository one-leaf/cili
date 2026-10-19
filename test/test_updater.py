"""自动升级模块测试"""

import json
import os
import shutil
import zipfile

from core import updater


def _make_remote_zip(tmp_path, version="v20260912", main_content="new main", extra=None, root="cili-main"):
    """构造一个升级包 zip（{root}/main.py + version.json + extra 相对路径文件），返回 zip 路径。"""
    zip_path = tmp_path / "remote.zip"
    src = tmp_path / "pkg"
    src.mkdir(parents=True, exist_ok=True)
    (src / "main.py").write_text(main_content, encoding="utf-8")
    (src / "web" / "static").mkdir(parents=True, exist_ok=True)
    (src / "web" / "static" / "version.json").write_text(
        f'{{"version": "{version}"}}', encoding="utf-8")
    for rel, content in (extra or {}).items():
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    with zipfile.ZipFile(zip_path, "w") as zf:
        for r, _, files in os.walk(src):
            for name in files:
                full = os.path.join(r, name)
                rel = os.path.relpath(full, src)
                zf.write(full, f"{root}/{rel}")
    return zip_path


def _make_project(tmp_path, monkeypatch, version="v20260911"):
    """构造一个最小"项目"目录（version.json + main.py），返回根路径。

    同时把 updater 的 PROJECT_ROOT / VERSION_FILE 指向该目录，版本读取不落到真实项目。
    """
    project_root = tmp_path / "project"
    (project_root / "web" / "static").mkdir(parents=True)
    (project_root / "web" / "static" / "version.json").write_text(
        f'{{"version": "{version}"}}', encoding="utf-8")
    (project_root / "main.py").write_text("old main", encoding="utf-8")
    monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(updater, "VERSION_FILE", project_root / "web" / "static" / "version.json")
    return project_root


class TestVersion:
    """版本解析与比较"""

    def test_parse_version(self):
        """解析标准版本号"""
        assert updater.parse_version("1.2.3") == (1, 2, 3)
        assert updater.parse_version("10.0.0") == (10, 0, 0)
        assert updater.parse_version("1.0.0-beta") == (1, 0, 0)

    def test_parse_version_empty_segment(self):
        """空段或非数字段按 0 处理"""
        assert updater.parse_version("1..3") == (1, 0, 3)
        assert updater.parse_version("v2.0.0") == (2, 0, 0)
        assert updater.parse_version("abc") == (0,)

    def test_is_newer_version(self):
        """版本号大小比较"""
        assert updater.is_newer_version("1.1.0", "1.0.9") is True
        assert updater.is_newer_version("2.0.0", "1.9.9") is True
        assert updater.is_newer_version("1.0.0", "1.0.0") is False
        assert updater.is_newer_version("1.0.0", "1.0.1") is False

    def test_is_newer_version_padding(self):
        """段数不同的版本按 0 对齐比较"""
        assert updater.is_newer_version("1.2.0", "1.2") is False
        assert updater.is_newer_version("1.2", "1.2.0") is False
        assert updater.is_newer_version("1.3", "1.2.9") is True

    def test_is_newer_version_date(self):
        """vYYYYMMDD 日期格式版本比较（.githooks 自动生成的版本号）"""
        assert updater.is_newer_version("v20260912", "v20260911") is True
        assert updater.is_newer_version("v20260912", "v20260912") is False
        assert updater.is_newer_version("v20260910", "v20260912") is False

    def test_get_local_version(self, tmp_path, monkeypatch):
        """读取本地 version.json 的 version 字段"""
        version_file = tmp_path / "version.json"
        version_file.write_text('{"app_name": "草履虫", "version": "v20260912"}', encoding="utf-8")
        monkeypatch.setattr(updater, "VERSION_FILE", version_file)
        assert updater.get_local_version() == "v20260912"

    def test_get_local_version_missing(self, tmp_path, monkeypatch):
        """版本文件缺失时按 0.0.0 处理"""
        monkeypatch.setattr(updater, "VERSION_FILE", tmp_path / "nonexistent.json")
        assert updater.get_local_version() == "0.0.0"

    def test_check_update(self, tmp_path, monkeypatch):
        """远端版本高于本地时判定有更新"""
        version_file = tmp_path / "version.json"
        version_file.write_text('{"version": "v20260911"}', encoding="utf-8")
        monkeypatch.setattr(updater, "VERSION_FILE", version_file)
        monkeypatch.setattr(updater, "fetch_remote_version", lambda: "v20260912")
        has_update, local, remote = updater.check_update()
        assert has_update is True
        assert local == "v20260911"
        assert remote == "v20260912"

    def test_check_update_fetch_failed(self, tmp_path, monkeypatch):
        """远端拉取失败时视为无更新，remote 为 None"""
        monkeypatch.setattr(updater, "VERSION_FILE", tmp_path / "version.json")
        monkeypatch.setattr(updater, "fetch_remote_version", lambda: None)
        has_update, local, remote = updater.check_update()
        assert has_update is False
        assert remote is None

    def test_fetch_remote_version_parses_json(self, monkeypatch):
        """远端 version.json 拉取后解析 version 字段"""
        monkeypatch.setattr(updater, "_download_text", lambda urls: '{"version": "v20260912"}')
        assert updater.fetch_remote_version() == "v20260912"

    def test_fetch_remote_version_invalid_json(self, monkeypatch):
        """远端内容非法时返回 None"""
        monkeypatch.setattr(updater, "_download_text", lambda urls: "not json")
        assert updater.fetch_remote_version() is None


class TestUpgradeZipVersion:
    """升级包版本读取"""

    def test_read_zip_version(self, tmp_path):
        """从标准结构（cili-main/web/static/version.json）读取版本"""
        zip_path = _make_remote_zip(tmp_path, "v20260912")
        assert updater.read_zip_version(str(zip_path)) == "v20260912"

    def test_read_zip_version_no_root(self, tmp_path):
        """无仓库根目录的包结构（web/static/version.json 在顶层）也能读取"""
        zip_path = _make_remote_zip(tmp_path, root="")
        assert updater.read_zip_version(str(zip_path)) == "v20260912"

    def test_read_zip_version_missing_entry(self, tmp_path):
        """包内没有 version.json 时返回 None"""
        zip_path = tmp_path / "no-version.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("cili-main/main.py", "code")
        assert updater.read_zip_version(str(zip_path)) is None

    def test_read_zip_version_corrupt(self, tmp_path):
        """损坏的 zip 返回 None"""
        zip_path = tmp_path / "corrupt.zip"
        zip_path.write_bytes(b"not a zip file")
        assert updater.read_zip_version(str(zip_path)) is None


class TestUpgrade:
    """升级文件操作"""

    def test_copy_tree_excludes_dirs(self, tmp_path):
        """复制时排除 data/workspace/.git，scripts 正常复制"""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        (src / "data").mkdir(parents=True)
        (src / "workspace").mkdir()
        (src / ".git").mkdir()
        (src / "scripts").mkdir()
        (src / "core").mkdir()
        (src / "data" / "keep.txt").write_text("data", encoding="utf-8")
        (src / "workspace" / "keep.txt").write_text("ws", encoding="utf-8")
        (src / ".git" / "keep.txt").write_text("git", encoding="utf-8")
        (src / "scripts" / "upgrade.ps1").write_text("script", encoding="utf-8")
        (src / "core" / "updater.py").write_text("code", encoding="utf-8")
        (src / "main.py").write_text("main", encoding="utf-8")

        updater._copy_tree(str(src), str(dst), updater._EXCLUDE_DIRS)

        assert (dst / "main.py").read_text(encoding="utf-8") == "main"
        assert (dst / "core" / "updater.py").read_text(encoding="utf-8") == "code"
        assert (dst / "scripts" / "upgrade.ps1").read_text(encoding="utf-8") == "script"
        assert not (dst / "data").exists()
        assert not (dst / "workspace").exists()
        assert not (dst / ".git").exists()

    def test_safe_extract(self, tmp_path):
        """正常 zip 解压后返回仓库根目录"""
        zip_path = tmp_path / "pkg.zip"
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "main.py").write_text("code", encoding="utf-8")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(str(src_dir / "main.py"), "cili-main/main.py")

        dest = tmp_path / "extract"
        root = updater._safe_extract(str(zip_path), str(dest))
        assert root is not None
        assert os.path.exists(os.path.join(root, "main.py"))

    def test_safe_extract_rejects_zip_slip(self, tmp_path):
        """拒绝 ../ 穿越路径的 zip 条目"""
        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("cili-main/../../evil.txt", "evil")

        dest = tmp_path / "extract"
        assert updater._safe_extract(str(zip_path), str(dest)) is None

    def test_copy_tree_skips_identical_files(self, tmp_path, monkeypatch):
        """内容未变的文件跳过写入，仅复制新增/变更文件（与 upgrade.ps1 的检查逻辑一致）"""
        import shutil as shutil_mod

        src = tmp_path / "src"
        dst = tmp_path / "dst"
        (src / "core").mkdir(parents=True)
        (dst / "core").mkdir(parents=True)
        (src / "same.py").write_text("same", encoding="utf-8")
        (dst / "same.py").write_text("same", encoding="utf-8")
        (src / "core" / "changed.py").write_text("new", encoding="utf-8")
        (dst / "core" / "changed.py").write_text("old", encoding="utf-8")
        (src / "core" / "added.py").write_text("add", encoding="utf-8")

        copied = []
        real_copy2 = shutil_mod.copy2

        def counting_copy2(s, d, **kwargs):
            copied.append(os.path.basename(d))
            return real_copy2(s, d, **kwargs)

        monkeypatch.setattr(shutil_mod, "copy2", counting_copy2)
        updater._copy_tree(str(src), str(dst), updater._EXCLUDE_DIRS)

        assert sorted(copied) == ["added.py", "changed.py"]
        assert (dst / "same.py").read_text(encoding="utf-8") == "same"
        assert (dst / "core" / "changed.py").read_text(encoding="utf-8") == "new"
        assert (dst / "core" / "added.py").read_text(encoding="utf-8") == "add"

    def test_apply_zip(self, tmp_path, monkeypatch):
        """解压覆盖：代码文件更新、版本号随包更新，用户数据目录排除"""
        project_root = _make_project(tmp_path, monkeypatch, "v20260911")
        (project_root / "data").mkdir()
        (project_root / "data" / "user.json").write_text('{"keep": 1}', encoding="utf-8")
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)

        zip_path = _make_remote_zip(tmp_path, "v20260912", "new main",
                                    extra={"data/x.txt": "x"})

        result = updater.apply_zip(str(zip_path))
        assert result["success"] is True
        assert "v20260911 → v20260912" in result["message"]
        assert (project_root / "main.py").read_text(encoding="utf-8") == "new main"
        # 版本文件随升级更新为包内版本
        new_version = json.loads(
            (project_root / "web" / "static" / "version.json").read_text(encoding="utf-8"))
        assert new_version["version"] == "v20260912"
        # 用户数据目录保留，包内 data/ 内容不覆盖
        assert (project_root / "data" / "user.json").read_text(encoding="utf-8") == '{"keep": 1}'
        assert not (project_root / "data" / "x.txt").exists()

    def test_apply_zip_locked(self, tmp_path, monkeypatch):
        """升级锁占用时返回繁忙"""
        monkeypatch.setattr(updater, "PROJECT_ROOT", tmp_path)
        updater._upgrade_lock.acquire()
        try:
            result = updater.apply_zip(str(tmp_path / "any.zip"))
            assert result["success"] is False
            assert "进行中" in result["error"]
        finally:
            updater._upgrade_lock.release()

    def test_apply_zip_removes_stale_files(self, tmp_path, monkeypatch):
        """升级后删除新版本已移除的残余文件（含 core/cron.d/ 下旧配置与 __pycache__）"""
        project_root = _make_project(tmp_path, monkeypatch)
        # 旧版本曾发布的文件：core/cron.d/ 下系统 cron 配置 + __pycache__ 生成物
        (project_root / "core" / "cron.d").mkdir(parents=True)
        (project_root / "core" / "cron.d" / "memory_consolidation.json").write_text("old", encoding="utf-8")
        (project_root / "core" / "cron.d" / "obsolete_task.json").write_text("obsolete", encoding="utf-8")
        (project_root / "core" / "cron.d" / "__pycache__").mkdir()
        (project_root / "core" / "cron.d" / "__pycache__" / "cron_tool.cpython-311.pyc").write_bytes(b"x")
        # 本地资源目录，升级不应触碰
        (project_root / ".claude").mkdir()
        (project_root / ".claude" / "settings.local.json").write_text("local", encoding="utf-8")
        (project_root / "pytest.ini").write_text("[pytest]", encoding="utf-8")
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)

        # 新版本：core/cron.d/ 只保留 memory_consolidation.json，obsolete_task.json 已移除
        zip_path = _make_remote_zip(tmp_path, "v20260912", "new main", extra={
            "core/cron.d/memory_consolidation.json": "new",
        })

        result = updater.apply_zip(str(zip_path))
        assert result["success"] is True
        # 新版本保留的文件被覆盖
        assert (project_root / "core" / "cron.d" / "memory_consolidation.json").read_text(
            encoding="utf-8") == "new"
        # 新版本已移除的残余文件与 __pycache__ 被删除
        assert not (project_root / "core" / "cron.d" / "obsolete_task.json").exists()
        assert not (project_root / "core" / "cron.d" / "__pycache__").exists()
        # 本地资源目录/文件保留
        assert (project_root / ".claude" / "settings.local.json").read_text(
            encoding="utf-8") == "local"
        assert (project_root / "pytest.ini").read_text(encoding="utf-8") == "[pytest]"

    def test_apply_zip_rollback_on_failure(self, tmp_path, monkeypatch):
        """复制中途失败时回滚到升级前版本，升级新增的文件被删除"""
        project_root = _make_project(tmp_path, monkeypatch)
        (project_root / "a.py").write_text("old-a", encoding="utf-8")
        (project_root / "b.py").write_text("old-b", encoding="utf-8")
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)

        # 升级包：a.py/b.py 更新 + 新增 c.py
        zip_path = _make_remote_zip(tmp_path, "v20260912", extra={
            "a.py": "new-a", "b.py": "new-b", "c.py": "new-c",
        })

        # 复制到项目根：a/b/c 全部先复制，随后对 c.py 抛异常，模拟半成品升级
        real_copy_tree = updater._copy_tree

        def flaky_copy_tree(src_dir, dst, exclude_dirs):
            for item in sorted(os.listdir(src_dir)):
                if item in exclude_dirs:
                    continue
                s = os.path.join(src_dir, item)
                d = os.path.join(dst, item)
                if os.path.isdir(s):
                    real_copy_tree(s, d, exclude_dirs)
                else:
                    shutil.copy2(s, d)
                    if item == "c.py" and os.path.abspath(dst) == os.path.abspath(str(project_root)):
                        raise OSError("simulated disk full")

        monkeypatch.setattr(updater, "_copy_tree", flaky_copy_tree)

        result = updater.apply_zip(str(zip_path))
        assert result["success"] is False
        assert "回滚" in result["error"]
        # 被覆盖的文件恢复，升级新增的文件被删除
        assert (project_root / "a.py").read_text(encoding="utf-8") == "old-a"
        assert (project_root / "b.py").read_text(encoding="utf-8") == "old-b"
        assert not (project_root / "c.py").exists()


class TestApplyPendingUpgrade:
    """启动时应用脚本目录下的升级包"""

    def test_no_zip_returns_none(self, tmp_path, monkeypatch):
        """脚本目录没有升级包时返回 None"""
        monkeypatch.setattr(updater, "PROJECT_ROOT", tmp_path)
        assert updater.apply_pending_upgrade() is None

    def test_cleans_stale_downloading_temp(self, tmp_path, monkeypatch):
        """启动时清理上次下载中断残留的临时文件"""
        project_root = _make_project(tmp_path, monkeypatch)
        stale = project_root / (updater.UPGRADE_ZIP_NAME + ".downloading")
        stale.write_bytes(b"partial download")

        assert updater.apply_pending_upgrade() is None  # 无升级包
        assert not stale.exists()

    def test_applies_newer_zip_and_deletes_it(self, tmp_path, monkeypatch):
        """包版本更高：解压覆盖后删除升级包"""
        project_root = _make_project(tmp_path, monkeypatch, "v20260911")
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)

        zip_path = _make_remote_zip(tmp_path, "v20260912", "new main")
        dest = project_root / updater.UPGRADE_ZIP_NAME
        shutil.copy(zip_path, dest)

        result = updater.apply_pending_upgrade()
        assert result is not None and result["success"] is True
        assert (project_root / "main.py").read_text(encoding="utf-8") == "new main"
        # 应用成功后升级包被删除
        assert not dest.exists()

    def test_skips_and_deletes_not_newer_zip(self, tmp_path, monkeypatch):
        """包版本不高于本地：不解压覆盖，删除升级包"""
        project_root = _make_project(tmp_path, monkeypatch, "v20260911")
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)

        zip_path = _make_remote_zip(tmp_path, "v20260911", "new main")
        dest = project_root / updater.UPGRADE_ZIP_NAME
        shutil.copy(zip_path, dest)

        result = updater.apply_pending_upgrade()
        assert result is not None and result["success"] is True
        # 代码未被覆盖
        assert (project_root / "main.py").read_text(encoding="utf-8") == "old main"
        # 升级包被清理
        assert not dest.exists()

    def test_deletes_invalid_zip(self, tmp_path, monkeypatch):
        """损坏的升级包：删除后按原版本启动"""
        project_root = _make_project(tmp_path, monkeypatch)
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)

        dest = project_root / updater.UPGRADE_ZIP_NAME
        dest.write_bytes(b"garbage not a zip")

        result = updater.apply_pending_upgrade()
        assert result is not None and result["success"] is False
        assert (project_root / "main.py").read_text(encoding="utf-8") == "old main"
        assert not dest.exists()

    def test_keeps_zip_when_apply_fails(self, tmp_path, monkeypatch):
        """应用失败：保留升级包，下次启动重试"""
        project_root = _make_project(tmp_path, monkeypatch, "v20260911")
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)

        # 包内版本可读（web/static/version.json 在子目录里），但解压后没有
        # cili-main 根目录 → _safe_extract 返回 None → 应用失败
        zip_path = _make_remote_zip(tmp_path, "v20260912", root="pkg")
        dest = project_root / updater.UPGRADE_ZIP_NAME
        shutil.copy(zip_path, dest)

        result = updater.apply_pending_upgrade()
        assert result is not None and result["success"] is False
        assert (project_root / "main.py").read_text(encoding="utf-8") == "old main"
        # 升级包保留
        assert dest.exists()


class TestDownloadUpgradeZip:
    """下载升级包到脚本目录（只下载，不应用）"""

    def test_download_writes_zip_to_script_dir(self, tmp_path, monkeypatch):
        """下载成功后升级包落在脚本目录（项目根），且不改动本地代码"""
        project_root = _make_project(tmp_path, monkeypatch, "v20260911")
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)
        monkeypatch.setattr(updater, "fetch_remote_version", lambda: "v20260912")

        real_zip = _make_remote_zip(tmp_path, "v20260912", "remote code")

        def fake_download(urls, dest, timeout=120):
            with open(dest, "wb") as f:
                f.write(real_zip.read_bytes())
            return True, ""

        monkeypatch.setattr(updater, "_download_zip", fake_download)

        result = updater.download_upgrade_zip()
        assert result["success"] is True
        dest = project_root / updater.UPGRADE_ZIP_NAME
        assert dest.exists()
        assert updater.read_zip_version(str(dest)) == "v20260912"
        assert updater.is_downloading() is False
        # 只下载：本地代码不变
        assert (project_root / "main.py").read_text(encoding="utf-8") == "old main"

    def test_download_skips_same_version(self, tmp_path, monkeypatch):
        """已有相同版本的升级包时跳过重复下载"""
        project_root = _make_project(tmp_path, monkeypatch, "v20260911")
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)
        monkeypatch.setattr(updater, "fetch_remote_version", lambda: "v20260912")

        real_zip = _make_remote_zip(tmp_path, "v20260912")
        dest = project_root / updater.UPGRADE_ZIP_NAME
        shutil.copy(real_zip, dest)

        def fail_download(urls, dest_path, timeout=120):
            raise AssertionError("不应重复下载")

        monkeypatch.setattr(updater, "_download_zip", fail_download)

        result = updater.download_upgrade_zip()
        assert result["success"] is True
        assert result.get("skipped") is True
        assert dest.exists()

    def test_download_replaces_older_zip(self, tmp_path, monkeypatch):
        """已有更旧版本的升级包时重新下载替换"""
        project_root = _make_project(tmp_path, monkeypatch, "v20260911")
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)
        monkeypatch.setattr(updater, "fetch_remote_version", lambda: "v20260913")

        real_zip = _make_remote_zip(tmp_path, "v20260913")

        def fake_download(urls, dest, timeout=120):
            with open(dest, "wb") as f:
                f.write(real_zip.read_bytes())
            return True, ""

        monkeypatch.setattr(updater, "_download_zip", fake_download)

        dest = project_root / updater.UPGRADE_ZIP_NAME
        dest.write_bytes(b"stale old zip")

        result = updater.download_upgrade_zip()
        assert result["success"] is True
        assert updater.read_zip_version(str(dest)) == "v20260913"

    def test_download_remote_check_failed(self, tmp_path, monkeypatch):
        """远端版本检查失败时不落盘"""
        project_root = _make_project(tmp_path, monkeypatch)
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)
        monkeypatch.setattr(updater, "fetch_remote_version", lambda: None)

        result = updater.download_upgrade_zip()
        assert result["success"] is False
        assert not (project_root / updater.UPGRADE_ZIP_NAME).exists()

    def test_download_invalid_package_rejected(self, tmp_path, monkeypatch):
        """下载内容无效（缺版本信息）时不占用升级包路径"""
        project_root = _make_project(tmp_path, monkeypatch)
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)
        monkeypatch.setattr(updater, "fetch_remote_version", lambda: "v20260912")

        def fake_download(urls, dest, timeout=120):
            with open(dest, "wb") as f:
                f.write(b"garbage not a zip")
            return True, ""

        monkeypatch.setattr(updater, "_download_zip", fake_download)

        result = updater.download_upgrade_zip()
        assert result["success"] is False
        assert not (project_root / updater.UPGRADE_ZIP_NAME).exists()
        assert updater.is_downloading() is False

    def test_download_records_shared_result(self, tmp_path, monkeypatch):
        """下载结果记录在模块级（自动 worker 与手动下载共用，UI 轮询读取）"""
        project_root = _make_project(tmp_path, monkeypatch, "v20260911")
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)
        monkeypatch.setattr(updater, "fetch_remote_version", lambda: "v20260912")
        real_zip = _make_remote_zip(tmp_path, "v20260912")

        def fake_download(urls, dest, timeout=120):
            with open(dest, "wb") as f:
                f.write(real_zip.read_bytes())
            return True, ""

        monkeypatch.setattr(updater, "_download_zip", fake_download)
        updater.reset_last_download_result()

        result = updater.download_upgrade_zip()
        assert result["success"] is True
        assert updater.get_last_download_result() == result

    def test_busy_does_not_overwrite_result(self, monkeypatch):
        """抢锁失败（另一线程正在下载）时不写共享结果——否则会覆盖真实结果"""
        updater.reset_last_download_result()
        assert updater._download_lock.acquire(blocking=False)
        try:
            result = updater.download_upgrade_zip("v20260912")
            assert result.get("busy") is True
            assert updater.get_last_download_result() is None
        finally:
            updater._download_lock.release()

    def test_exception_becomes_recorded_result(self, monkeypatch):
        """下载流程抛异常也要变成可展示的结果并记录，且释放进行中标记"""
        updater.reset_last_download_result()

        def boom(remote_version):
            raise RuntimeError("network down")

        monkeypatch.setattr(updater, "_do_download", boom)
        result = updater.download_upgrade_zip("v20260912")
        assert result["success"] is False
        assert "network down" in result["error"]
        assert updater.get_last_download_result()["success"] is False
        assert updater.is_downloading() is False


class TestPruneStaleFiles:
    """升级残余文件清理"""

    def test_prune_removes_stale_files_in_shared_dir(self, tmp_path):
        """共享代码目录内部：新版本不存在的文件与 __pycache__ 被删除，新版本文件保留"""
        dst = tmp_path / "dst"
        src = tmp_path / "src"
        (dst / "core" / "cron.d").mkdir(parents=True)
        (dst / "core" / "cron.d" / "memory_consolidation.json").write_text("keep", encoding="utf-8")
        (dst / "core" / "cron.d" / "obsolete.json").write_text("stale", encoding="utf-8")
        (dst / "core" / "cron.d" / "__pycache__").mkdir()
        (dst / "core" / "cron.d" / "__pycache__" / "x.pyc").write_bytes(b"x")
        (src / "core" / "cron.d").mkdir(parents=True)
        (src / "core" / "cron.d" / "memory_consolidation.json").write_text("new", encoding="utf-8")

        updater._prune_stale_files(str(dst), str(src), updater._EXCLUDE_DIRS)

        assert (dst / "core" / "cron.d" / "memory_consolidation.json").exists()
        assert not (dst / "core" / "cron.d" / "obsolete.json").exists()
        assert not (dst / "core" / "cron.d" / "__pycache__").exists()

    def test_prune_preserves_local_top_level_dirs_and_files(self, tmp_path):
        """新代码包中不存在的顶层本地目录与文件整体保留"""
        dst = tmp_path / "dst"
        src = tmp_path / "src"
        (dst / ".claude").mkdir(parents=True)
        (dst / ".claude" / "settings.local.json").write_text("local", encoding="utf-8")
        (dst / "reference" / "nanobot").mkdir(parents=True)
        (dst / "reference" / "nanobot" / "main.py").write_text("ref", encoding="utf-8")
        (dst / "pytest.ini").write_text("[pytest]", encoding="utf-8")
        (dst / "core").mkdir(parents=True)
        (dst / "core" / "old.py").write_text("stale", encoding="utf-8")
        (src / "core").mkdir(parents=True)
        (src / "core" / "updater.py").write_text("code", encoding="utf-8")

        updater._prune_stale_files(str(dst), str(src), updater._EXCLUDE_DIRS)

        assert (dst / ".claude" / "settings.local.json").read_text(encoding="utf-8") == "local"
        assert (dst / "reference" / "nanobot" / "main.py").read_text(encoding="utf-8") == "ref"
        assert (dst / "pytest.ini").read_text(encoding="utf-8") == "[pytest]"
        assert not (dst / "core" / "old.py").exists()

    def test_prune_never_touches_excluded_dirs(self, tmp_path):
        """data/workspace/.git 等排除目录整体保留，即使新代码包中不存在"""
        dst = tmp_path / "dst"
        src = tmp_path / "src"
        (dst / "data" / "cili" / "cron.d").mkdir(parents=True)
        (dst / "data" / "cili" / "cron.d" / "user_tasks.json").write_text("keep", encoding="utf-8")
        (dst / "workspace").mkdir(parents=True)
        (dst / "workspace" / "note.txt").write_text("keep", encoding="utf-8")
        (src / "core").mkdir(parents=True)

        updater._prune_stale_files(str(dst), str(src), updater._EXCLUDE_DIRS)

        assert (dst / "data" / "cili" / "cron.d" / "user_tasks.json").exists()
        assert (dst / "workspace" / "note.txt").exists()

    def test_prune_removes_stale_nested_dirs(self, tmp_path):
        """共享目录内，新版本整体移除的子目录被删除"""
        dst = tmp_path / "dst"
        src = tmp_path / "src"
        (dst / "core" / "legacy" / "sub").mkdir(parents=True)
        (dst / "core" / "legacy" / "sub" / "a.py").write_text("stale", encoding="utf-8")
        (src / "core").mkdir(parents=True)
        (src / "core" / "updater.py").write_text("code", encoding="utf-8")

        updater._prune_stale_files(str(dst), str(src), updater._EXCLUDE_DIRS)

        assert not (dst / "core" / "legacy").exists()
