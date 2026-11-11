# -*- coding: utf-8 -*-
"""动态路径权限：静态前缀、opaque 目标、会话批准与规则落盘。"""

import os

from core.security.path_policy import PathPolicy
from core.security.permission_rules import PermissionRules, build_granted_rule
from core.security.python_paths import collect_python_targets
from core.tools.permission import PermissionStore


def test_fstring_prefix_target(tmp_path):
    target = collect_python_targets(
        'open(f".cili/tmp/pricing/{name}.txt", "w")', str(tmp_path)
    )[0]
    assert target.dynamic_kind == "prefixed"
    assert target.static_prefix == os.path.realpath(tmp_path / ".cili/tmp/pricing")
    assert target.suggested_rule["type"] == "prefix"
    assert "文件名运行时确定" in target.display


def test_opaque_target_has_no_prefix(tmp_path):
    target = collect_python_targets(
        'for path, url in targets:\n    open(path, "w")', str(tmp_path)
    )[0]
    assert target.dynamic_kind == "opaque"
    assert target.static_prefix is None
    assert target.suggested_rule is None
    assert "无法静态解析" in target.display


def test_opaque_decision_ids_are_unique(tmp_path):
    a = collect_python_targets(
        'for path, url in targets:\n    open(path, "w")', str(tmp_path)
    )[0]
    b = collect_python_targets(
        'for path, url in targets:\n    open("x", "w")\n    open(path, "w")',
        str(tmp_path),
    )[0]
    policy = PathPolicy(
        workspace_root=str(tmp_path), cwd=str(tmp_path),
        permission_store=PermissionStore(),
    )
    assert policy.decision_id(a) != policy.decision_id(b)


def test_dynamic_session_approval(tmp_path):
    store = PermissionStore()
    target = collect_python_targets(
        'for path, url in targets:\n    open(path, "w")', str(tmp_path)
    )[0]
    policy = PathPolicy(
        workspace_root=str(tmp_path), cwd=str(tmp_path),
        permission_store=store,
    )
    did = policy.decision_id(target)

    assert policy.permission_policy.path_verdict(
        "path:write", None, decision_id=did
    ).action == "ask"

    store.approve(did, target.raw, kind="path:write")
    assert policy.permission_policy.path_verdict(
        "path:write", None, decision_id=did
    ).action == "allow"


def test_opaque_rule_cannot_be_persisted():
    result = build_granted_rule(kind="path:write", tool="python", action="path")
    assert result.rule is None
    assert result.reason


def test_prefixed_rule_persisted_and_matched(tmp_path):
    target = collect_python_targets(
        'open(f".cili/tmp/pricing/{name}.txt", "w")', str(tmp_path)
    )[0]
    result = build_granted_rule(
        kind="path:write", tool="python", action=target.display,
        suggested=target.suggested_rule, granted_by="user",
    )
    assert result.rule is not None
    assert result.rule["match"]["type"] == "prefix"

    rules = PermissionRules(tmp_path / "approval_rules.json")
    added = rules.add_rule(result.rule, "workspace")
    assert added is not None
    assert rules.evaluate_path(
        "path:write",
        os.path.realpath(tmp_path / ".cili/tmp/pricing/other.txt"),
        tool="python",
    )[0] == "allow"


def test_opaque_question_hides_remember_option():
    from core.tools.permission import REMEMBER_LABEL, build_permission_question
    q = build_permission_question({
        "kind": "path:write",
        "display": "写入运行时确定的路径（path；无法静态解析）",
        "dynamic_kind": "opaque",
        "decision_id": "d-opaque",
    })
    assert REMEMBER_LABEL not in q
    assert "无法生成持久规则" in q


def test_prefixed_question_keeps_remember_option():
    from core.tools.permission import REMEMBER_LABEL, build_permission_question
    q = build_permission_question({
        "kind": "path:write",
        "display": "写入 C:/tmp/pricing 下的文件（文件名运行时确定）",
        "dynamic_kind": "prefixed",
        "decision_id": "d-prefixed",
    })
    assert REMEMBER_LABEL in q


def test_prefixed_path_verdict_hits_rule(tmp_path):
    target = collect_python_targets(
        'open(f".cili/tmp/pricing/{name}.txt", "w")', str(tmp_path)
    )[0]
    built = build_granted_rule(
        kind="path:write", tool="python", action=target.display,
        suggested=target.suggested_rule, granted_by="user",
    )
    rules = PermissionRules(tmp_path / "approval_rules.json")
    rules.add_rule(built.rule, "workspace")
    policy = PathPolicy(
        workspace_root=str(tmp_path), cwd=str(tmp_path),
        permission_rules=rules, permission_store=PermissionStore(),
    )
    verdict = policy.permission_policy.path_verdict(
        "path:write", None, static_prefix=target.static_prefix, tool="python",
    )
    assert verdict.action == "allow"


def test_relative_path_rule_is_rejected(tmp_path):
    rules = PermissionRules(tmp_path / "approval_rules.json")
    rule = {
        "kind": "path:write", "decision": "allow", "tool": "python",
        "match": {"type": "prefix", "value": "f'.cili/tmp"},
    }
    assert rules.add_rule(rule, "workspace") is None


def test_permission_meta_exposes_dynamic_fields(tmp_path):
    target = collect_python_targets(
        'for path, url in targets:\n    open(path, "w")', str(tmp_path)
    )[0]
    policy = PathPolicy(
        workspace_root=str(tmp_path), cwd=str(tmp_path),
        permission_store=PermissionStore(),
    )
    meta = policy.permission_meta(target, tool="python")
    assert meta["dynamic_kind"] == "opaque"
    assert meta["suggested_rule"] is None
    assert meta["display"]
    assert "无法静态解析" in meta["reason"]
