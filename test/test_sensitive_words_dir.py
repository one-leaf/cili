"""敏感词目录布局：settings + rules 一词一文件。"""

from __future__ import annotations

import json

from core.sensitive_words import load_rules, read_config, write_config


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_reads_directory_config(tmp_path):
    base = tmp_path / ".cili" / "config" / "sensitive_words"
    _write(base / "settings.json", {"enabled": True, "fixed": {"phone": False}})
    _write(base / "rules" / "abc.json", {"word": "秘密项目", "replacement": "某项目"})

    cfg = read_config(tmp_path / ".cili")
    assert cfg["enabled"] is True
    assert cfg["rules"] == [{"word": "秘密项目", "replacement": "某项目"}]
    rules = load_rules(tmp_path / ".cili")
    assert rules.replace_text("秘密项目启动") == "某项目启动"


def test_write_updates_split_files(tmp_path):
    base = tmp_path / ".cili" / "config" / "sensitive_words"
    base.mkdir(parents=True)
    write_config(tmp_path / ".cili", {
        "enabled": True,
        "fixed": {"phone": True},
        "rules": [{"word": "alpha", "replacement": "A"}],
    })

    assert json.loads((base / "settings.json").read_text(encoding="utf-8"))["fixed"]["phone"] is True
    rule_files = list((base / "rules").glob("*.json"))
    assert len(rule_files) == 1

    write_config(tmp_path / ".cili", {
        "enabled": True,
        "fixed": {"phone": True},
        "rules": [{"word": "beta", "replacement": "B"}],
    })
    rule_files = list((base / "rules").glob("*.json"))
    assert len(rule_files) == 1
    assert json.loads(rule_files[0].read_text(encoding="utf-8"))["word"] == "beta"
