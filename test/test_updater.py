"""自动升级模块测试"""

import json
import os
import shutil
import zipfile

from core import updater


def _make_remote_zip(tmp_path, version="v20260912", main_content="new main"):
    """构造一个"远端"代码包 zip（cili-main/main.py + version.json），返回 zip 路径。"""
    zip_path = tmp_path / "remote.zip"
    src = tmp_path / "pkg"
    src.mkdir(parents=True)
    (src / "main.py").write_text(main_content, encoding="utf-8")
    (src / "web" / "static").mkdir(parents=True)
    (src / "web" / "static" / "version.json").write_text(
        f'{{"version": "{version}"}}', encoding="utf-8")
    with zipfile.ZipFile(zip_path, "w") as zf:
        for root, _, files in os.walk(src):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, src)
                zf.write(full, f"cili-main/{rel}")
    return zip_path


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

    def test_do_upgrade(self, tmp_path, monkeypatch):
        """完整升级流程：下载（mock）→ 解压 → 覆盖，排除用户目录"""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "web" / "static").mkdir(parents=True)
        (project_root / "web" / "static" / "version.json").write_text(
            '{"version": "v20260911"}', encoding="utf-8")
        (project_root / "data").mkdir()
        (project_root / "data" / "user.json").write_text('{"keep": 1}', encoding="utf-8")
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)

        # 构造一个"远端"代码包并 mock 下载函数写入它
        zip_path = tmp_path / "remote.zip"
        src = tmp_path / "pkg"
        (src / "web" / "static").mkdir(parents=True)
        (src / "data").mkdir()
        (src / "main.py").write_text("new main", encoding="utf-8")
        (src / "web" / "static" / "version.json").write_text(
            '{"version": "v20260912"}', encoding="utf-8")
        (src / "data" / "x.txt").write_text("x", encoding="utf-8")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for root, _, files in os.walk(src):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, src)
                    zf.write(full, f"cili-main/{rel}")

        def fake_download(urls, dest, timeout=120):
            with open(dest, "wb") as f:
                f.write(zip_path.read_bytes())
            return True, ""

        monkeypatch.setattr(updater, "_download_zip", fake_download)

        result = updater.do_upgrade()
        assert result["success"] is True
        assert result["needs_restart"] is True
        assert (project_root / "main.py").read_text(encoding="utf-8") == "new main"
        # 版本文件随升级更新为远端版本
        new_version = json.loads((project_root / "web" / "static" / "version.json").read_text(encoding="utf-8"))
        assert new_version["version"] == "v20260912"
        # 用户数据目录保留
        assert (project_root / "data" / "user.json").read_text(encoding="utf-8") == '{"keep": 1}'
        assert not (project_root / "data" / "x.txt").exists()

    def test_do_upgrade_locked(self, tmp_path, monkeypatch):
        """升级锁占用时返回繁忙"""
        monkeypatch.setattr(updater, "PROJECT_ROOT", tmp_path)
        updater._upgrade_lock.acquire()
        try:
            result = updater.do_upgrade()
            assert result["success"] is False
            assert "进行中" in result["error"]
        finally:
            updater._upgrade_lock.release()

    def test_do_upgrade_rollback_on_failure(self, tmp_path, monkeypatch):
        """复制中途失败时回滚到升级前版本，升级新增的文件被删除"""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "a.py").write_text("old-a", encoding="utf-8")
        (project_root / "b.py").write_text("old-b", encoding="utf-8")
        monkeypatch.setattr(updater, "PROJECT_ROOT", project_root)

        # 远端包：a.py/b.py 更新 + 新增 c.py
        zip_path = tmp_path / "remote.zip"
        src = tmp_path / "pkg"
        src.mkdir(parents=True)
        (src / "a.py").write_text("new-a", encoding="utf-8")
        (src / "b.py").write_text("new-b", encoding="utf-8")
        (src / "c.py").write_text("new-c", encoding="utf-8")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for root, _, files in os.walk(src):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, src)
                    zf.write(full, f"cili-main/{rel}")

        def fake_download(urls, dest, timeout=120):
            with open(dest, "wb") as f:
                f.write(zip_path.read_bytes())
            return True, ""

        monkeypatch.setattr(updater, "_download_zip", fake_download)

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

        result = updater.do_upgrade()
        assert result["success"] is False
        assert "回滚" in result["error"]
        # 被覆盖的文件恢复，升级新增的文件被删除
        assert (project_root / "a.py").read_text(encoding="utf-8") == "old-a"
        assert (project_root / "b.py").read_text(encoding="utf-8") == "old-b"
        assert not (project_root / "c.py").exists()
