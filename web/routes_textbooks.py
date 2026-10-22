"""教材清单查询与自动下载路由域。

清单来自 ``core/templates/textbooks/china_textbook_catalog.json``。查询接口只读
清单；下载接口创建后台任务，把教材平铺写入工作区 ``教材/``。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.config import get_workspace_data_dir, load_workspace_config
from core.teen_mode import get_teen_mode
from core.textbooks import (
    CatalogBook,
    TextbookCatalogError,
    TextbookDownloadError,
    get_catalog,
    textbook_tasks,
)
from web.deps import _SAFE_ID_RE, _csrf_protect, teen_guard

router = APIRouter()
logger = logging.getLogger(__name__)


class TextbookDownloadRequest(BaseModel):
    workspace_uuid: str
    book_keys: list[str]
    overwrite: bool = False


class TextbookRetryRequest(BaseModel):
    book_keys: list[str] | None = None


def _catalog_or_400():
    try:
        return get_catalog()
    except TextbookCatalogError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _parse_book_types(raw: str, catalog) -> tuple[str, ...]:
    if not raw:
        return catalog.default_book_types
    types = tuple(item.strip() for item in raw.split(",") if item.strip())
    return types or catalog.default_book_types


def _workspace_or_404(workspace_uuid: str) -> dict[str, Any]:
    if not _SAFE_ID_RE.fullmatch(workspace_uuid or ""):
        raise HTTPException(status_code=400, detail="Invalid workspace_uuid format")
    info = load_workspace_config(workspace_uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if info.get("system") or workspace_uuid == "system":
        raise HTTPException(status_code=403, detail="System workspace cannot download textbooks")
    get_teen_mode().guard_workspace(workspace_uuid, source="web")
    return info


def _validate_books(catalog, book_keys: list[str]) -> list[CatalogBook]:
    if not book_keys:
        raise HTTPException(status_code=400, detail="没有选择要下载的教材")
    if len(book_keys) != len(set(book_keys)):
        raise HTTPException(status_code=400, detail="教材重复选择")
    books: list[CatalogBook] = []
    for key in book_keys:
        if not _SAFE_ID_RE.fullmatch(key or ""):
            raise HTTPException(status_code=400, detail=f"Invalid book_key: {key}")
        book = catalog.get_by_key(key)
        if not book:
            raise HTTPException(status_code=400, detail=f"教材不在清单中: {key}")
        books.append(book)
    return books


@router.get("/api/textbooks/subjects")
async def textbook_subjects(stage: str = "", system: str = "", book_types: str = "textbook"):
    """返回可选学科；供向导/设置页联动。"""
    catalog = _catalog_or_400()
    types = _parse_book_types(book_types, catalog)
    return {
        "source_commit": catalog.source_commit,
        "subjects": catalog.subjects(
            stage=stage.strip(), system=system.strip(), book_types=types,
        ),
    }


@router.get("/api/textbooks/options")
async def textbook_options(
    stage: str = "",
    system: str = "",
    subject: str = "",
    edition: str = "",
    grade: str = "",
    semester: str = "",
    high_scope: str = "all_textbook_volumes",
    book_types: str = "textbook",
    edition_grade: str = "",
):
    """按学段/系统/学科/版本/年级/学期返回可选教材；edition_grade 用于过滤版本列表。"""
    catalog = _catalog_or_400()
    types = _parse_book_types(book_types, catalog)
    books = catalog.find_books(
        stage=stage.strip(),
        system=system.strip(),
        subject=subject.strip(),
        edition=edition.strip(),
        grade=grade.strip(),
        semester=semester.strip(),
        high_scope=high_scope.strip() or "all_textbook_volumes",
        book_types=types,
    )
    editions = catalog.editions(
        stage=stage.strip(),
        system=system.strip(),
        subject=subject.strip(),
        grade=edition_grade.strip(),
        book_types=types,
    )
    warnings: list[str] = []
    if not books:
        warnings.append("该科目暂无匹配的电子课本")
    return {
        "source_commit": catalog.source_commit,
        "editions": editions,
        "books": [book.public_dict() for book in books],
        "total_size": sum(book.total_size for book in books),
        "split_count": sum(1 for book in books if book.split),
        "warnings": warnings,
    }


@router.post("/api/textbooks/download",
             dependencies=[Depends(teen_guard("config_update"))])
async def download_textbooks(request: Request, body: TextbookDownloadRequest):
    """创建后台教材下载任务。"""
    _csrf_protect(request)
    info = _workspace_or_404(body.workspace_uuid)
    workspace_dir = info.get("directory") or ""
    if not workspace_dir:
        raise HTTPException(status_code=400, detail="Workspace directory not configured")

    catalog = _catalog_or_400()
    books = _validate_books(catalog, body.book_keys)
    try:
        task = textbook_tasks.start(
            workspace_uuid=body.workspace_uuid,
            workspace_dir=workspace_dir,
            data_dir=get_workspace_data_dir(body.workspace_uuid),
            catalog=catalog,
            books=books,
            overwrite=body.overwrite,
        )
    except TextbookDownloadError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    logger.info(
        "[textbooks] 创建下载任务: workspace=%s task=%s books=%s",
        body.workspace_uuid, task.task_id, len(books),
    )
    return {
        "task_id": task.task_id,
        "total_books": len(books),
        "total_bytes": task.total_bytes,
        "status": task.status,
    }


def _task_or_404(task_id: str):
    if not _SAFE_ID_RE.fullmatch(task_id or ""):
        raise HTTPException(status_code=400, detail="Invalid task_id format")
    task = textbook_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    get_teen_mode().guard_workspace(task.workspace_uuid, source="web")
    return task


@router.get("/api/textbooks/download/{task_id}")
async def textbook_download_status(task_id: str):
    """查询教材下载任务进度。"""
    task = _task_or_404(task_id)
    return task.public_dict()


@router.post("/api/textbooks/download/{task_id}/retry",
             dependencies=[Depends(teen_guard("config_update"))])
async def retry_textbook_download(
    task_id: str,
    request: Request,
    body: TextbookRetryRequest | None = None,
):
    """重试任务中失败的教材；可传入要重试的 book_keys。"""
    _csrf_protect(request)
    _task_or_404(task_id)
    try:
        task = await textbook_tasks.retry(
            task_id,
            book_keys=body.book_keys if body else None,
        )
    except TextbookDownloadError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {
        "task_id": task.task_id,
        "total_books": len(task.books),
        "total_bytes": task.total_bytes,
        "status": task.status,
    }


@router.post("/api/textbooks/download/{task_id}/cancel",
             dependencies=[Depends(teen_guard("config_update"))])
async def cancel_textbook_download(task_id: str, request: Request):
    """取消教材下载任务；已完成的教材保留。"""
    _csrf_protect(request)
    _task_or_404(task_id)
    try:
        return await textbook_tasks.cancel(task_id)
    except TextbookDownloadError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
