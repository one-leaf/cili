"""自动升级模块测试"""

import json
import os
import zipfile

from core import updater


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
        """读取本地 footer.json 的 version 字段"""
        version_file = tmp_path / "footer.json"
        version_file.write_text('{"app_name": "草履虫", "version": "v20260912"}', encoding="utf-8")
        monkeypatch.setattr(updater, "VERSION_FILE", version_file)
        assert updater.get_local_version() == "v20260912"

    def test_get_local_version_missing(self, tmp_path, monkeypatch):
        """版本文件缺失时按 0.0.0 处理"""
        monkeypatch.setattr(updater, "VERSION_FILE", tmp_path / "nonexistent.json")
        assert updater.get_local_version() == "0.0.0"

    def test_check_update(self, tmp_path, monkeypatch):
        """远端版本高于本地时判定有更新"""
        version_file = tmp_path / "footer.json"
        version_file.write_text('{"version": "v20260911"}', encoding="utf-8")
        monkeypatch.setattr(updater, "VERSION_FILE", version_file)
        monkeypatch.setattr(updater, "fetch_remote_version", lambda: "v20260912")
        has_update, local, remote = updater.check_update()
        assert has_update is True
        assert local == "v20260911"
        assert remote == "v20260912"

    def test_check_update_fetch_failed(self, tmp_path, monkeypatch):
        """远端拉取失败时视为无更新，remote 为 None"""
        monkeypatch.setattr(updater, "VERSION_FILE", tmp_path / "footer.json")
        monkeypatch.setattr(updater, "fetch_remote_version", lambda: None)
        has_update, local, remote = updater.check_update()
        assert has_update is False
        assert remote is None

    def test_fetch_remote_version_parses_json(self, monkeypatch):
        """远端 footer.json 拉取后解析 version 字段"""
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
        (project_root / "web" / "static" / "footer.json").write_text(
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
        (src / "web" / "static" / "footer.json").write_text(
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
        new_footer = json.loads((project_root / "web" / "static" / "footer.json").read_text(encoding="utf-8"))
        assert new_footer["version"] == "v20260912"
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
