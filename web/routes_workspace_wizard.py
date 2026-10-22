"""新建工作区向导路由：选项、预检与批量创建。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.config import PROJECT_ROOT, get_workspace_data_dir
from core.textbooks import (
    TextbookCatalogError,
    TextbookDownloadError,
    get_catalog,
    textbook_tasks,
)
from core.textbooks.catalog import EDITION_PUBLISHERS, edition_sort_key
from core.workspace_wizard import (
    CATEGORIES,
    GRADES,
    LOCATIONS,
    STAGES,
    STUDY_SUBJECTS,
    SYSTEMS,
    WorkspaceWizardError,
    create_workspaces,
    documents_root,
    preview_workspaces,
)
from web.deps import _csrf_protect, _list_all_workspaces, teen_guard

router = APIRouter()
logger = logging.getLogger(__name__)


class WorkspaceWizardRequest(BaseModel):
    category: str
    name: str
    location: str = "cili"
    slug: str = ""
    custom_directory: str = ""
    profile: dict[str, Any] = {}
    subjects: list[str] = []
    allow_existing: bool = False


class WorkspaceWizardCreateRequest(WorkspaceWizardRequest):
    overwrite_agents: bool = False
    git_enabled: bool = True
    memory_enabled: bool = True
    download_textbooks: bool = True


def _wizard_error(e: WorkspaceWizardError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


def _locations() -> list[dict[str, str]]:
    roots = {
        "cili": str(PROJECT_ROOT / "workspace"),
        "home": str(Path.home() / "cili"),
        "documents": str(documents_root()),
        "custom": "",
    }
    return [{**item, "parent": roots[item["id"]]} for item in LOCATIONS]


def _sorted_editions(entries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """版本选项按名称排序；默认选中最通行的版本（统编版 > 人教版 > 书目最多）。"""
    names = sorted(entries, key=edition_sort_key)
    for preferred in ("统编版", "人教版"):
        if preferred in entries:
            top = preferred
            break
    else:
        top = max(names, key=lambda name: entries[name]["count"])
    return [
        {
            "name": name,
            "publisher": EDITION_PUBLISHERS.get(name, ""),
            "default": name == top,
            "grades": sorted(entries[name]["grades"]),
        }
        for name in names
    ]


def _textbook_editions() -> dict[str, dict[str, dict[str, list[dict[str, Any]]]]]:
    """从教材清单统计各学制、学段、学科实际存在的教材版本（仅正文教材）。"""
    entries: dict[str, dict[str, dict[str, dict[str, dict[str, Any]]]]] = {
        system: {stage: {} for stage in STAGES} for system in SYSTEMS
    }
    try:
        catalog = get_catalog()
    except TextbookCatalogError:
        pass
    else:
        for book in catalog.books:
            stages = entries.get(book.system)
            if not stages or book.stage not in STAGES or not book.edition:
                continue
            if book.book_type != "textbook":
                continue
            subjects = stages[book.stage].setdefault(book.subject, {})
            item = subjects.setdefault(book.edition, {"count": 0, "grades": set()})
            item["count"] += 1
            item["grades"].add(book.grade or "")
    return {
        system: {
            stage: {
                subject: _sorted_editions(subject_entries)
                for subject, subject_entries in subjects.items()
            }
            for stage, subjects in stages.items()
        }
        for system, stages in entries.items()
    }


@router.get("/api/workspace-wizard/options")
async def workspace_wizard_options():
    """返回向导静态选项。"""
    return {
        "categories": list(CATEGORIES.values()),
        "locations": _locations(),
        "stages": list(STAGES),
        "systems": list(SYSTEMS),
        "grades": GRADES,
        "textbook_editions": _textbook_editions(),
        "study_subjects": STUDY_SUBJECTS,
        "defaults": {
            "location": "cili",
            "git_enabled": True,
            "memory_enabled": True,
            "download_textbooks": True,
        },
    }


@router.post("/api/workspace-wizard/preview")
async def workspace_wizard_preview(request: WorkspaceWizardRequest):
    """预检向导要创建的工作区；不写文件。"""
    try:
        return await asyncio.to_thread(
            preview_workspaces,
            category=request.category,
            base_name=request.name,
            location=request.location,
            slug=request.slug,
            custom_directory=request.custom_directory,
            profile=request.profile,
            subjects=request.subjects,
            existing_workspaces=_list_all_workspaces(),
            allow_existing=request.allow_existing,
        )
    except WorkspaceWizardError as e:
        raise _wizard_error(e) from e


def _start_textbook_tasks(created: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    """为学习项目工作区启动匹配教材的下载任务。"""
    catalog = get_catalog()
    stage = str(profile.get("stage") or "")
    system = str(profile.get("system") or "六三制")
    grade = str(profile.get("grade") or "")
    tasks: list[dict[str, Any]] = []
    for item in created:
        subject = str(item.get("subject") or "")
        if not subject:
            continue
        edition = str((item.get("profile") or {}).get("textbook_edition") or "")
        books = catalog.find_books(
            stage=stage,
            system=system,
            subject=subject,
            edition=edition,
            grade=grade,
            book_types=("textbook",),
            high_scope="all_textbook_volumes",
        )
        if not books:
            continue
        task = textbook_tasks.start(
            workspace_uuid=str(item["uuid"]),
            workspace_dir=item["directory"],
            data_dir=get_workspace_data_dir(str(item["uuid"])),
            catalog=catalog,
            books=books,
            overwrite=False,
        )
        tasks.append({
            "workspace_uuid": item["uuid"],
            "subject": subject,
            "task_id": task.task_id,
            "total_books": len(books),
            "total_bytes": task.total_bytes,
        })
    return tasks


@router.post("/api/workspace-wizard/create",
             dependencies=[Depends(teen_guard("config_update"))])
async def workspace_wizard_create(request: Request, body: WorkspaceWizardCreateRequest):
    """创建一个或多个工作区；学习项目可自动启动教材下载。"""
    _csrf_protect(request)
    try:
        result = await asyncio.to_thread(
            create_workspaces,
            category=body.category,
            base_name=body.name,
            location=body.location,
            slug=body.slug,
            custom_directory=body.custom_directory,
            profile=body.profile,
            subjects=body.subjects,
            existing_workspaces=_list_all_workspaces(),
            allow_existing=body.allow_existing,
            overwrite_agents=body.overwrite_agents,
            git_enabled=body.git_enabled,
            memory_enabled=body.memory_enabled,
        )
    except WorkspaceWizardError as e:
        raise _wizard_error(e) from e

    textbook_tasks_response: list[dict[str, Any]] = []
    warnings = list(result.get("warnings") or [])
    if body.category == "study" and body.download_textbooks and result.get("created"):
        try:
            textbook_tasks_response = _start_textbook_tasks(result["created"], body.profile)
        except TextbookCatalogError as e:
            warnings.append(f"教材清单不可用，已跳过自动下载: {e}")
        except TextbookDownloadError as e:
            warnings.append(f"教材下载任务创建失败: {e}")
        except Exception as e:  # noqa: BLE001 —— 教材下载失败不回滚工作区
            logger.exception("[workspace-wizard] 启动教材下载失败")
            warnings.append(f"教材下载任务创建失败: {e}")

    return {**result, "textbook_tasks": textbook_tasks_response, "warnings": warnings}
