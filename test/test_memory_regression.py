"""记忆工具回归：list/archived/stale、正文检索、consolidate 作用域。"""

from __future__ import annotations

from core.tools import get_tool_by_name


def _tool(tools):
    return get_tool_by_name(tools, "memory")


def _store(tool, name: str, content: str = "body", *, title: str = "T"):
    result = tool.execute(
        action="store", memory_type="fact", name=name,
        title=title, description=f"desc {name}", content=content,
    )
    assert not result.error, result.output
    return result


def test_list_filters_type_and_status(tools):
    t = _tool(tools)
    _store(t, "list-fact-a")
    _store(t, "list-fact-b")
    t.execute(action="store", memory_type="preference", name="list-pref",
              title="P", description="p", content="p")

    facts = t.execute(action="list", memory_type="fact")
    assert "list-fact-a" in facts.output and "list-fact-b" in facts.output
    assert "list-pref" not in facts.output

    missing = t.execute(action="list", memory_type="fact", status="archived")
    assert "No memory entries" in missing.output


def test_archived_is_readable_but_not_findable(tools):
    t = _tool(tools)
    _store(t, "archive-probe", "archived body keyword")
    assert t.store.archive("archive-probe")["archived"] is True

    found = t.execute(action="find", query="archive-probe")
    assert "No memory matches" in found.output

    listed = t.execute(action="list", memory_type="fact", status="archived")
    assert "archive-probe" in listed.output

    read = t.execute(action="read", name="archive-probe")
    assert "archived body keyword" in read.output
    assert not read.error


def test_stale_status_is_visible_in_list_and_find(tools):
    t = _tool(tools)
    _store(t, "stale-probe", "stale body")
    updated = t.execute(action="update", name="stale-probe", status="stale")
    assert not updated.error

    listed = t.execute(action="list", memory_type="fact", status="stale")
    assert "stale-probe" in listed.output

    found = t.execute(action="find", query="stale-probe", status="stale")
    assert "stale-probe" in found.output


def test_find_matches_body_and_orders_by_usage(tools):
    t = _tool(tools)
    _store(t, "body-a", "only-in-body BODYTOKEN9")
    _store(t, "body-b", "other body")
    # 读两次 body-b，使其 usage_count 高于 body-a；find 同词应按 usage 倒序
    t.execute(action="read", name="body-b")
    t.execute(action="read", name="body-b")

    by_body = t.execute(action="find", query="BODYTOKEN9")
    assert "body-a" in by_body.output

    by_bodyword = t.execute(action="find", query="body")
    assert by_bodyword.output.index("body-b") < by_bodyword.output.index("body-a")


def test_consolidate_only_calls_current_workspace(tools, monkeypatch):
    from core import memory_pipeline

    t = _tool(tools)
    seen = []

    def fake(workspace_uuid, *args, **kwargs):
        seen.append(workspace_uuid)
        return {"processed": 1, "applied": [{"op": "store", "name": "x"}],
                "archived": [], "pending_after": 0}

    monkeypatch.setattr(memory_pipeline, "run_consolidation", fake)
    result = t.execute(action="consolidate")
    assert not result.error
    assert seen == ["test-workspace"]
    assert "1 stored" in result.output


def test_stat_shows_index_path(tools):
    t = _tool(tools)
    _store(t, "stat-probe")
    result = t.execute(action="stat")
    assert "index path:" in result.output
    assert "MEMORY.md" in result.output


def test_archive_and_restore_actions(tools):
    t = _tool(tools)
    _store(t, "tool-archive", "archive action body")
    archived = t.execute(action="archive", name="tool-archive")
    assert not archived.error
    assert "Archived" in archived.output

    found = t.execute(action="find", query="tool-archive", status="archived")
    assert "tool-archive" in found.output

    restored = t.execute(action="restore", name="tool-archive")
    assert not restored.error
    assert "Restored" in restored.output
    active = t.execute(action="find", query="tool-archive")
    assert "tool-archive" in active.output


def test_find_limit_reports_truncation(tools):
    t = _tool(tools)
    for i in range(3):
        _store(t, f"limit-probe-{i}", "same body", title=f"Limit {i}")
    result = t.execute(action="find", query="limit-probe", limit=1)
    assert "Found 3 match(es)" in result.output
    assert "showing 1" in result.output
    assert any(f"limit-probe-{i}" in result.output for i in range(3))

    beyond = t.execute(action="find", query="limit-probe", offset=99)
    assert "offset 99" in beyond.output

    zero = t.execute(action="find", query="limit-probe", limit=0)
    assert zero.error
    assert "limit must be > 0" in zero.output


def test_list_limit_reports_total(tools):
    t = _tool(tools)
    for i in range(3):
        _store(t, f"list-limit-{i}", "body", title=f"List {i}")
    result = t.execute(action="list", memory_type="fact", limit=1)
    assert "3 memory entries" in result.output
    assert "showing 1" in result.output


def test_update_rejects_type_change_and_noop(tools):
    t = _tool(tools)
    _store(t, "update-guard")
    typed = t.execute(action="update", name="update-guard", memory_type="skill")
    assert typed.error
    assert "memory_type cannot be changed" in typed.output

    noop = t.execute(action="update", name="update-guard")
    assert noop.error
    assert "no fields to update" in noop.output
