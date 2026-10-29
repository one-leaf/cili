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
    TextbookDownloadError,
    get_official_catalog,
    textbook_tasks,
)
from core.textbooks.markdown_store import copy_textbook_markdown
from core.workspace_wizard import (
    CATEGORIES,
    LOCATIONS,
    WorkspaceWizardError,
    create_workspaces,
    documents_root,
    grade_options,
    preview_workspaces,
    stage_options,
    study_subjects_by_stage,
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


def _textbook_editions() -> dict[str, dict[str, list[dict[str, Any]]]]:
    """按官方目录统计各学段、学科实际存在的教材版本。

    官方把五•四学制作为独立学段，因此不再区分「学制」维度。
    """
    try:
        catalog = get_official_catalog()
    except Exception:  # noqa: BLE001 —— 平台目录不可用时返回空映射
        return {}
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for stage in catalog.stage_options():
        subjects: dict[str, list[dict[str, Any]]] = {}
        for item in catalog.subjects(stage=stage):
            subject = item["subject"]
            editions = catalog.editions(stage=stage, subject=subject)
            if not editions:
                continue
            names = [entry["edition"] for entry in editions]
            if "统编版" in names:
                top = "统编版"
            elif "人教版" in names:
                top = "人教版"
            else:
                top = max(editions, key=lambda entry: entry["count"])["edition"]
            subjects[subject] = [
                {
                    "name": entry["edition"],
                    "publisher": "",
                    "default": entry["edition"] == top,
                    "grades": entry["grades"],
                    "count": entry["count"],
                }
                for entry in editions
            ]
        if subjects:
            result[stage] = subjects
    return result


@router.get("/api/workspace-wizard/options")
async def workspace_wizard_options():
    """返回向导选项：学段/年级/学科与教材版本均来自官方平台目录。"""

    def build() -> tuple[list[str], dict[str, list[str]], dict[str, list[str]],
                          dict[str, dict[str, list[dict[str, Any]]]]]:
        try:
            stages = stage_options()
        except Exception:  # noqa: BLE001 —— 平台目录不可用时前端降级为空列表
            return [], {}, {}, {}
        grades = {stage: grade_options(stage) for stage in stages}
        subjects = study_subjects_by_stage()
        editions = _textbook_editions()
        return stages, grades, subjects, editions

    stages, grades, subjects, editions = await asyncio.to_thread(build)
    return {
        "categories": list(CATEGORIES.values()),
        "locations": _locations(),
        "stages": stages,
        "grades": grades,
        "textbook_editions": editions,
        "study_subjects": subjects,
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
    """为学习项目工作区启动匹配教材的下载任务（官方目录 + 浏览器登录凭据）。"""
    catalog = get_official_catalog()
    stage = str(profile.get("stage") or "")
    grade = str(profile.get("grade") or "")
    tasks: list[dict[str, Any]] = []
    for item in created:
        subject = str(item.get("subject") or "")
        if not subject:
            continue
        edition = str((item.get("profile") or {}).get("textbook_edition") or "")
        books = catalog.find_books(
            stage=stage,
            subject=subject,
            edition=edition,
            grade=grade,
        )
        if not books:
            continue
        # 与 PDF 下载成功与否无关：先把已生成的教材 Markdown 复制到 教材/
        copied = copy_textbook_markdown(item["directory"], catalog, books)
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
            "markdown_copies": len(copied),
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
        except TextbookDownloadError as e:
            warnings.append(f"教材下载任务创建失败: {e}")
        except Exception as e:  # noqa: BLE001 —— 教材下载失败不回滚工作区
            logger.exception("[workspace-wizard] 启动教材下载失败")
            warnings.append(f"教材下载任务创建失败: {e}")

    return {**result, "textbook_tasks": textbook_tasks_response, "warnings": warnings}
