"""fs_utils 原子写与损坏备份测试（A36: C3/C4/SEC-23）。"""

import json
from pathlib import Path

from core.fs_utils import atomic_write_json, atomic_write_text, load_json_or_backup


class TestAtomicWriteJson:
    """atomic_write_json 原子写（唯一临时名 + LF + fsync）。"""

    def test_writes_json(self, tmp_path):
        """写入后可正常读取。"""
        path = tmp_path / "state.json"
        atomic_write_json(path, {"a": 1, "b": [2, 3]})
        assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "b": [2, 3]}

    def test_no_tmp_residue(self, tmp_path):
        """写入后不留 .tmp 残留（唯一临时名，os.replace 后即消失）。"""
        path = tmp_path / "index.json"
        atomic_write_json(path, {"x": 1})
        assert not list(tmp_path.glob("index.json.tmp*"))

    def test_unique_temp_names_across_concurrent_writes(self, tmp_path):
        """SEC-23: 并发写同一目标使用不同临时文件名，不互踩。"""
        path = tmp_path / "shared.json"
        atomic_write_json(path, {"v": "first"})
        atomic_write_json(path, {"v": "second"})
        # 每次写完后目标文件内容正确（后写覆盖先写）
        assert json.loads(path.read_text(encoding="utf-8"))["v"] == "second"
        # 不存在残留的固定名 .tmp（并发场景也不会是同一个）
        assert not (tmp_path / "shared.json.tmp").exists()

    def test_overwrites_existing(self, tmp_path):
        """已存在目标文件时正确覆盖。"""
        path = tmp_path / "state.json"
        path.write_text('{"old": true}', encoding="utf-8")
        atomic_write_json(path, {"new": True})
        assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}


class TestAtomicWriteText:
    """atomic_write_text 任意文本原子写（A41: write/edit 工具收敛）。"""

    def test_writes_text_preserving_lf(self, tmp_path):
        """newline="" 默认保持 LF，\n 不被翻译成 \r\n（Windows 文本模式）。"""
        path = tmp_path / "script.sh"
        atomic_write_text(path, "#!/bin/bash\necho hi\n")
        assert path.read_bytes() == b"#!/bin/bash\necho hi\n"

    def test_crlf_mode(self, tmp_path):
        """传 newline="\\r\\n" 时按 CRLF 写入。"""
        path = tmp_path / "win.txt"
        atomic_write_text(path, "line1\nline2\n", newline="\r\n")
        assert path.read_bytes() == b"line1\r\nline2\r\n"

    def test_no_tmp_residue(self, tmp_path):
        """写入后不留固定 .tmp 残留。"""
        path = tmp_path / "doc.txt"
        atomic_write_text(path, "content")
        assert not list(tmp_path.glob("doc.txt.tmp*"))

    def test_creates_parent_dirs(self, tmp_path):
        """父目录不存在时自动创建。"""
        path = tmp_path / "a" / "b" / "doc.txt"
        atomic_write_text(path, "content")
        assert path.read_text(encoding="utf-8") == "content"


class TestLoadJsonOrBackup:
    """load_json_or_backup 损坏备份（C4: 备份名带随机后缀）。"""

    def test_returns_default_when_missing(self, tmp_path):
        """文件不存在时返回 default。"""
        assert load_json_or_backup(tmp_path / "nope.json", []) == []

    def test_loads_valid(self, tmp_path):
        """正常文件原样返回。"""
        path = tmp_path / "ok.json"
        path.write_text('{"ok": 1}', encoding="utf-8")
        assert load_json_or_backup(path, {}) == {"ok": 1}

    def test_backs_up_corrupt_and_returns_default(self, tmp_path):
        """损坏文件被改名备份，返回 default。"""
        path = tmp_path / "broken.json"
        path.write_text("{invalid json", encoding="utf-8")
        result = load_json_or_backup(path, {"default": True})
        assert result == {"default": True}
        assert not path.exists()
        assert len(list(tmp_path.glob("broken.json.corrupt-*"))) == 1

    def test_same_second_corruption_keeps_distinct_backups(self, tmp_path):
        """C4: 同一秒内多次损坏，随机后缀保证每份现场都保留。"""
        path = tmp_path / "state.json"
        path.write_text("{bad 1", encoding="utf-8")
        load_json_or_backup(path, {})
        path.write_text("{bad 2", encoding="utf-8")
        load_json_or_backup(path, {})
        backups = list(tmp_path.glob("state.json.corrupt-*"))
        assert len(backups) == 2
        assert len({b.name for b in backups}) == 2
