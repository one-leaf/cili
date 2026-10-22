"""工作区向导服务测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

import core.config as config_mod
from core.config import PROJECT_ROOT
from core.workspace_wizard import (
    WorkspaceWizardError,
    create_workspaces,
    preview_workspaces,
    slugify,
    validate_workspace_directory,
)


def _profile(**overrides):
    data = {
        "learner_name": "学生A",
        "stage": "初中",
        "system": "六三制",
        "grade": "八年级",
        "textbook_edition": "人教版",
        "exam_region": "广东",
        "focus": "同步学习",
        "daily_minutes": 60,
    }
    data.update(overrides)
    return data


def _use_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")


def test_slugify_keeps_chinese_and_rejects_reserved():
    assert slugify("学生A 八年级 数学") == "学生A-八年级-数学"
    assert slugify("My App: v2") == "My-App-v2"
    assert slugify("CON") == "workspace"
    assert slugify("") == "workspace"


def test_preview_study_creates_one_workspace_per_subject(tmp_path):
    preview = preview_workspaces(
        category="study",
        base_name="学生A",
        location="custom",
        custom_directory=str(tmp_path / "projects"),
        profile=_profile(),
        subjects=["数学", "英语", "数学"],
    )
    assert preview["group"]["name"] == "学生A-初中"
    assert [item["subject"] for item in preview["workspaces"]] == ["数学", "英语"]
    assert preview["workspaces"][0]["directory"] != preview["workspaces"][1]["directory"]
    assert "课前小测" in preview["workspaces"][0]["agents_md"]
    assert preview["workspaces"][0]["scaffold"]["files"]


def test_preview_requires_subject_and_stage():
    with pytest.raises(WorkspaceWizardError, match="科目"):
        preview_workspaces(
            category="study", base_name="学生A", location="custom",
            custom_directory=str(Path("E:/tmp/empty")), profile=_profile(),
            subjects=[],
        )
    with pytest.raises(WorkspaceWizardError, match="学段"):
        preview_workspaces(
            category="study", base_name="学生A", location="custom",
            custom_directory=str(Path("E:/tmp/empty")),
            profile=_profile(stage=""), subjects=["数学"],
        )


def test_preview_rejects_duplicate_workspace_name(tmp_path):
    existing = [{"uuid": "w1", "name": "学生A", "directory": str(tmp_path / "w1")}]
    with pytest.raises(WorkspaceWizardError, match="同名工作区"):
        preview_workspaces(
            category="office", base_name="学生A", location="cili",
            existing_workspaces=existing,
        )
    with pytest.raises(WorkspaceWizardError, match="同名工作区"):
        create_workspaces(
            category="office", base_name="学生A", location="cili",
            existing_workspaces=existing, git_enabled=False,
        )


def test_preview_rejects_duplicate_study_title(tmp_path):
    existing = [{"uuid": "w1", "name": "八年级-数学", "directory": str(tmp_path / "w1")}]
    with pytest.raises(WorkspaceWizardError, match="同名工作区"):
        preview_workspaces(
            category="study", base_name="学生A", location="cili",
            profile=_profile(), subjects=["数学"],
            existing_workspaces=existing,
        )


def test_study_grade_linked_to_system():
    with pytest.raises(WorkspaceWizardError, match="六年级"):
        preview_workspaces(
            category="study", base_name="学生A", location="cili",
            profile=_profile(grade="六年级"), subjects=["数学"],
        )
    with pytest.raises(WorkspaceWizardError, match="学制非法"):
        preview_workspaces(
            category="study", base_name="学生A", location="cili",
            profile=_profile(system="不明学制"), subjects=["数学"],
        )
    preview = preview_workspaces(
        category="study", base_name="学生A", location="cili",
        profile=_profile(system="五四制", grade="六年级"), subjects=["数学"],
    )
    assert preview["workspaces"][0]["name"] == "六年级-数学"


def test_subject_edition_override(tmp_path):
    preview = preview_workspaces(
        category="study", base_name="学生A", location="cili",
        profile=_profile(subject_overrides={"数学": {"textbook_edition": "北师大版"}}),
        subjects=["数学", "英语"],
    )
    profiles = {item["subject"]: item["profile"] for item in preview["workspaces"]}
    assert profiles["数学"]["textbook_edition"] == "北师大版"
    assert profiles["英语"]["textbook_edition"] == "人教版"


def test_blank_name_uses_parent_directory_directly(tmp_path):
    custom = tmp_path / "MyApp"
    custom.mkdir()
    preview = preview_workspaces(
        category="office", base_name="", location="custom",
        custom_directory=str(custom),
    )
    item = preview["workspaces"][0]
    assert Path(item["directory"]) == custom.resolve()
    assert item["slug"] == ""
    assert item["name"] == "MyApp"


def test_blank_name_rejected_for_cili_location():
    with pytest.raises(WorkspaceWizardError, match="父目录"):
        preview_workspaces(category="office", base_name="", location="cili")


def test_invalid_slug_source_rejected(tmp_path):
    with pytest.raises(WorkspaceWizardError, match="目录名非法"):
        preview_workspaces(
            category="office", base_name="CON", location="custom",
            custom_directory=str(tmp_path / "ok"),
        )


def test_rejects_directory_containing_workspace_parent(tmp_path):
    with pytest.raises(WorkspaceWizardError, match="工作区父目录"):
        validate_workspace_directory(PROJECT_ROOT.parent)


def test_create_study_workspaces_and_initial_files(tmp_path, monkeypatch):
    _use_index(tmp_path, monkeypatch)
    uuids = iter(["abcd1234", "efgh5678"])
    result = create_workspaces(
        category="study",
        base_name="学生A",
        location="custom",
        custom_directory=str(tmp_path / "projects"),
        profile=_profile(),
        subjects=["数学", "英语"],
        git_enabled=False,
        memory_enabled=True,
        new_uuid=lambda: next(uuids),
    )
    assert len(result["created"]) == 2
    assert result["created"][0]["uuid"] == "abcd1234"
    assert result["group"]["name"] == "学生A-初中"

    math_dir = Path(result["created"][0]["directory"])
    english_dir = Path(result["created"][1]["directory"])
    assert (math_dir / "AGENTS.md").is_file()
    assert (math_dir / "学习计划.md").is_file()
    assert (math_dir / "教材" / "README.md").is_file()
    assert (english_dir / ".cili" / "sessions").is_dir()
    assert not (math_dir / ".git").exists()

    entries = config_mod.load_workspaces_index()
    assert {entry["uuid"] for entry in entries} == {"abcd1234", "efgh5678"}
    assert all(entry["study_group_id"] == result["group"]["id"] for entry in entries)


def test_create_does_not_overwrite_existing_agents(tmp_path, monkeypatch):
    _use_index(tmp_path, monkeypatch)
    custom = tmp_path / "projects"
    agents = custom / "数学" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text("# 用户已有说明\n", encoding="utf-8")
    result = create_workspaces(
        category="study", base_name="学生A", location="custom",
        custom_directory=str(custom), profile=_profile(), subjects=["数学"],
        allow_existing=True, git_enabled=False, new_uuid=lambda: "abcd1234",
    )
    assert result["created"]
    assert agents.read_text(encoding="utf-8") == "# 用户已有说明\n"


def test_create_fails_without_creating_when_preview_invalid(tmp_path, monkeypatch):
    _use_index(tmp_path, monkeypatch)
    with pytest.raises(WorkspaceWizardError, match="科目"):
        create_workspaces(
            category="study", base_name="学生A", location="custom",
            custom_directory=str(tmp_path / "projects"), profile=_profile(),
            subjects=[], git_enabled=False,
        )
    assert not list((tmp_path / "projects").glob("*")) if (tmp_path / "projects").exists() else True
