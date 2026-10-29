"""官方教材目录查询与下载路由域。

目录来自国家中小学智慧教育平台（``core.textbooks.smartedu``，内存 + 磁盘缓存
24 小时）。下载需要平台登录凭据：缺失时会自动打开浏览器引导手机号登录
（``smartedu_login``），抓取凭据后写入缓存目录长期生效。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.config import get_workspace_data_dir, load_workspace_config
from core.teen_mode import get_teen_mode
from core.textbooks import (
    OfficialBook,
    TextbookDownloadError,
    get_official_catalog,
    textbook_tasks,
)
from core.textbooks.markdown_store import copy_textbook_markdown
from core.textbooks.smartedu import SmartEduError
from core.textbooks.smartedu_login import credentials_ready, login_status, start_login
from web.deps import _SAFE_ID_RE, _csrf_protect, teen_guard

router = APIRouter()
logger = logging.getLogger(__name__)

LOGIN_REQUIRED_MESSAGE = (
    "需要先在浏览器完成教材平台登录（已自动打开登录页）；"
    "用手机号注册/登录后回到本页重试下载。"
)


class TextbookDownloadRequest(BaseModel):
    workspace_uuid: str
    book_keys: list[str]
    overwrite: bool = False


class TextbookRetryRequest(BaseModel):
    book_keys: list[str] | None = None


def _catalog_or_503():
    try:
        return get_official_catalog()
    except SmartEduError as e:
        raise HTTPException(status_code=503, detail=f"平台教材目录不可用: {e}") from e
    except Exception as e:  # noqa: BLE001 —— 平台异常统一转 503
        raise HTTPException(status_code=503, detail=f"平台教材目录不可用: {e}") from e


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


def _validate_books(catalog, book_keys: list[str]) -> list[OfficialBook]:
    if not book_keys:
        raise HTTPException(status_code=400, detail="没有选择要下载的教材")
    if len(book_keys) != len(set(book_keys)):
        raise HTTPException(status_code=400, detail="教材重复选择")
    books: list[OfficialBook] = []
    for key in book_keys:
        if not _SAFE_ID_RE.fullmatch(key or ""):
            raise HTTPException(status_code=400, detail=f"Invalid book_key: {key}")
        book = catalog.get_by_key(key)
        if not book:
            raise HTTPException(status_code=400, detail=f"教材不在平台目录中: {key}")
        books.append(book)
    return books


@router.get("/api/textbooks/subjects")
async def textbook_subjects(stage: str = "", system: str = "", book_types: str = "textbook"):
    """返回可选学科（``system``/``book_types`` 仅保留参数兼容）。"""
    del system, book_types
    catalog = await asyncio.to_thread(_catalog_or_503)
    return {
        "source_commit": catalog.source_commit,
        "subjects": catalog.subjects(stage=stage.strip()),
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
    """按学段/学科/版本/年级/册次返回可选教材（``system``/``book_types`` 仅兼容）。"""
    del system, high_scope, book_types
    catalog = await asyncio.to_thread(_catalog_or_503)
    books = catalog.find_books(
        stage=stage.strip(),
        subject=subject.strip(),
        edition=edition.strip(),
        grade=grade.strip(),
        semester=semester.strip(),
    )
    editions = catalog.editions(
        stage=stage.strip(),
        subject=subject.strip(),
        grade=edition_grade.strip(),
    )
    warnings: list[str] = []
    if not books:
        warnings.append("该科目暂无匹配的电子课本")
    return {
        "source_commit": catalog.source_commit,
        "editions": editions,
        "books": [book.public_dict() for book in books],
        "total_size": sum(book.total_size for book in books),
        "split_count": 0,
        "warnings": warnings,
    }


@router.get("/api/textbooks/login/status")
async def textbook_login_status():
    """查询教材平台登录状态（凭据缓存于 data/cili/cache，登录一次长期有效）。"""
    return {**login_status(), "credentials_ready": credentials_ready()}


@router.post("/api/textbooks/login",
             dependencies=[Depends(teen_guard("config_update"))])
async def textbook_login(request: Request):
    """打开浏览器引导用户手机号登录，并抓取平台凭据写入缓存。"""
    _csrf_protect(request)
    if credentials_ready():
        return {"ok": True, "message": "已有登录凭据", "running": False}
    return start_login()


@router.post("/api/textbooks/download",
             dependencies=[Depends(teen_guard("config_update"))])
async def download_textbooks(request: Request, body: TextbookDownloadRequest):
    """创建后台教材下载任务；未登录时自动打开浏览器登录页。"""
    _csrf_protect(request)
    info = _workspace_or_404(body.workspace_uuid)
    workspace_dir = info.get("directory") or ""
    if not workspace_dir:
        raise HTTPException(status_code=400, detail="Workspace directory not configured")
    catalog = await asyncio.to_thread(_catalog_or_503)
    books = _validate_books(catalog, body.book_keys)
    # 无论 PDF 是否下载成功，先把已生成的教材 Markdown 复制到 教材/
    copied = await asyncio.to_thread(copy_textbook_markdown, workspace_dir, catalog, books)

    if not credentials_ready():
        status = start_login()
        raise HTTPException(status_code=409, detail=LOGIN_REQUIRED_MESSAGE,
                            headers={"X-Cili-Login": str(status.get("running", False)).lower()})
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
        "markdown_copies": len(copied),
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
