"""审批规则一规则一文件：目录加载、写盘、删除与旧路径兼容。"""

from __future__ import annotations

import json

from core.security.permission_rules import PermissionRules


def _rule(rule_id: str, value: str = "git status") -> dict:
    return {
        "id": rule_id,
        "enabled": True,
        "decision": "allow",
        "tool": "*",
        "kind": "command",
        "match": {"type": "exact", "value": value},
    }


def test_loads_directory_and_prefers_it_over_legacy_file(tmp_path):
    data = tmp_path / ".cili"
    data.mkdir(parents=True)
    legacy = data / "approval_rules.json"
    legacy.write_text(json.dumps({
        "version": 1,
        "rules": [_rule("r-old")],
        "constraints": [],
    }), encoding="utf-8")
    rules_dir = data / "config" / "approval_rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "r-new.json").write_text(
        json.dumps({"version": 1, "rule": _rule("r-new", "git diff")}),
        encoding="utf-8",
    )
    (rules_dir / "_constraints.json").write_text(
        json.dumps({"version": 1, "constraints": [{"id": "c", "text": "x"}]}),
        encoding="utf-8",
    )

    rules = PermissionRules(legacy)

    ids = {r["id"] for r in rules.all_rules("workspace")}
    assert ids == {"r-new"}
    assert rules._workspace_constraints == [{"id": "c", "text": "x"}]


def test_add_and_delete_write_one_file(tmp_path):
    legacy = tmp_path / ".cili" / "approval_rules.json"
    rules_dir = tmp_path / ".cili" / "config" / "approval_rules"
    rules_dir.mkdir(parents=True)
    rules = PermissionRules(legacy)

    added = rules.add_rule(_rule("r-a"), "workspace")
    assert added and (rules_dir / "r-a.json").is_file()
    assert rules.delete_rule("r-a", "workspace") is True
    assert not (rules_dir / "r-a.json").exists()


def test_bump_only_touches_hit_rule_file(tmp_path):
    legacy = tmp_path / ".cili" / "approval_rules.json"
    rules_dir = tmp_path / ".cili" / "config" / "approval_rules"
    rules_dir.mkdir(parents=True)
    rules = PermissionRules(legacy)
    rules.add_rule(_rule("r-a"), "workspace")
    rules.add_rule(_rule("r-b", "git log"), "workspace")
    rules._last_hit_flush = 0

    rules._bump(rules.all_rules("workspace")[0])

    assert (rules_dir / "r-a.json").is_file()
    assert (rules_dir / "r-b.json").is_file()
