"""教材下载任务：官方平台源文件流式下载、断点续传、重试与进度。

数据来自国家中小学智慧教育平台：目录由 ``smartedu`` 模块抓取并缓存；每本书
下载前按其 content_id 查询详情接口取源文件地址（私有 CDN，需要登录凭据，
``X-ND-AUTH`` MAC 签名按每条 URL 现算）。凭据由浏览器登录流程抓取并缓存。

任务由 FastAPI 路由创建后在当前事件循环里后台执行；不阻塞工作区创建。
目标文件平铺写到工作区 ``教材/`` 下（重名自动加版本前缀），临时文件写在
工作区 ``.cili/tmp/textbooks/<content_id>/``。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from core.fs_utils import atomic_write_json, load_json_or_backup
from core.textbooks.smartedu import (
    BASE_HEADERS,
    PRIVATE_CDN_HOSTS,
    OfficialBook,
    OfficialCatalog,
    build_nd_auth,
    clear_credentials,
    fetch_book_source,
)

logger = logging.getLogger(__name__)

MANIFEST_NAME = ".manifest.json"
MANIFEST_SCHEMA_VERSION = 1
BOOK_KEY_RE = re.compile(r"^[0-9A-Za-z-]{8,80}$")
MAX_CONCURRENT_BOOKS = 2
MAX_ATTEMPTS = 5
ATTEMPT_BACKOFF_SECONDS = (0.25, 0.5, 1.0, 2.0, 2.0)
DOWNLOAD_BLOCK_SIZE = 256 * 1024
# 私有 CDN 短时间连打会 400：同地址退避重试（每次重新签名）比立刻换镜像更有效
_SAME_URL_400_DELAYS = (1.0, 3.0)
MIN_FREE_RATIO = 1.2
MIN_FREE_HEADROOM = 64 * 1024 * 1024


class TextbookDownloadError(RuntimeError):
    """下载任务创建或执行失败。"""


@dataclass
class BookProgress:
    """单本书的下载进度。"""

    book_key: str
    title: str
    target_path: str
    total_size: int
    chunk_count: int = 1
    split: bool = False
    status: str = "pending"
    downloaded_bytes: int = 0
    error: str = ""
    sha256: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "book_key": self.book_key,
            "title": self.title,
            "target_path": self.target_path,
            "total_size": self.total_size,
            "chunk_count": self.chunk_count,
            "split": self.split,
            "status": self.status,
            "downloaded_bytes": self.downloaded_bytes,
            "error": self.error,
            "sha256": self.sha256,
        }


@dataclass
class TextbookDownloadTask:
    """一个工作区的一次批量教材下载。"""

    task_id: str
    workspace_uuid: str
    workspace_dir: Path
    data_dir: Path
    source_commit: str
    overwrite: bool
    books: dict[str, BookProgress]
    catalog_books: dict[str, OfficialBook]
    status: str = "running"
    downloaded_bytes: int = 0
    total_bytes: int = 0
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    async_task: asyncio.Task | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workspace_uuid": self.workspace_uuid,
            "status": self.status,
            "total_books": len(self.books),
            "total_bytes": self.total_bytes,
            "downloaded_bytes": self.downloaded_bytes,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "books": [progress.public_dict() for progress in self.books.values()],
        }

    def touch(self) -> None:
        self.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _validate_book_key(book: OfficialBook) -> None:
    if not BOOK_KEY_RE.fullmatch(book.content_id):
        raise TextbookDownloadError(f"教材 content_id 非法: {book.content_id}")


def _validate_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise TextbookDownloadError("教材源文件地址非法")
    if not parsed.hostname.endswith(".ykt.cbern.com.cn"):
        raise TextbookDownloadError(f"教材源文件域名非法: {parsed.hostname}")
    return url


def _cdn_candidates(url: str) -> list[str]:
    """私有 CDN 镜像顺序：原 host 优先，再切 r1/r2/r3 中其余地址。"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host not in PRIVATE_CDN_HOSTS:
        return [url]
    others = [h for h in PRIVATE_CDN_HOSTS if h != host]
    return [url] + [
        url.replace(f"//{host}", f"//{other}", 1) for other in others
    ]


def _signed_headers(url: str) -> dict[str, str]:
    headers = dict(BASE_HEADERS)
    headers["X-ND-AUTH"] = build_nd_auth(url, "GET")
    return headers


def _target_path(workspace_dir: Path, relative: str, book_key: str) -> Path:
    parts = [part for part in relative.replace("\\", "/").split("/") if part]
    if len(parts) != 2 or parts[0] != "教材" or parts[1] in ("", ".", ".."):
        raise TextbookDownloadError(f"教材目标路径非法: {book_key}")
    root = (workspace_dir / "教材").resolve()
    target = (root / parts[1]).resolve()
    if not target.is_relative_to(root):
        raise TextbookDownloadError(f"教材目标路径越界: {book_key}")
    return target


def _manifest_path(workspace_dir: Path) -> Path:
    return workspace_dir / "教材" / MANIFEST_NAME


def _load_manifest(workspace_dir: Path) -> dict[str, Any]:
    data = load_json_or_backup(_manifest_path(workspace_dir), {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "books": {},
    })
    if not isinstance(data, dict) or not isinstance(data.get("books"), dict):
        data = {"schema_version": MANIFEST_SCHEMA_VERSION, "books": {}}
    data.setdefault("schema_version", MANIFEST_SCHEMA_VERSION)
    data.setdefault("books", {})
    return data


def _save_manifest(workspace_dir: Path, data: dict[str, Any]) -> None:
    data["schema_version"] = MANIFEST_SCHEMA_VERSION
    data["updated_at"] = _now()
    atomic_write_json(_manifest_path(workspace_dir), data)


def _record_book(workspace_dir: Path, source_commit: str, progress: BookProgress) -> None:
    manifest = _load_manifest(workspace_dir)
    manifest["source_commit"] = source_commit
    manifest["books"][progress.book_key] = {
        "title": progress.title,
        "target_path": progress.target_path,
        "status": progress.status,
        "bytes": progress.total_size,
        "sha256": progress.sha256,
        "chunk_count": 1,
        "split": False,
        "error": progress.error,
        "updated_at": _now(),
    }
    _save_manifest(workspace_dir, manifest)


def _ensure_disk_space(workspace_dir: Path, total_size: int) -> None:
    required = int(total_size * MIN_FREE_RATIO) + MIN_FREE_HEADROOM
    try:
        free = shutil.disk_usage(workspace_dir).free
    except OSError as e:
        raise TextbookDownloadError(f"无法检查磁盘空间: {e}") from e
    if free < required:
        needed_gb = required / 1024 / 1024 / 1024
        free_gb = free / 1024 / 1024 / 1024
        raise TextbookDownloadError(
            f"磁盘空间不足：预计需要 {needed_gb:.2f} GB，当前可用 {free_gb:.2f} GB"
        )


def _ensure_textbook_gitignore(workspace_dir: Path) -> None:
    """教材原件不进入 Git 自动提交；只保留 README 和 manifest。"""
    path = workspace_dir / ".gitignore"
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        normalized = [line.strip() for line in lines]
        changed = False
        for item in ("教材/**", "!教材/.manifest.json", "!教材/README.md"):
            if item not in normalized:
                lines.append(item)
                changed = True
        if changed:
            from core.fs_utils import atomic_write_text
            atomic_write_text(path, "\n".join(lines) + "\n")
    except OSError as e:
        logger.warning(f"[textbooks] 更新工作区 .gitignore 失败: {e}")


class TextbookTaskManager:
    """教材下载任务的内存管理器。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TextbookDownloadTask] = {}
        self._workspace_tasks: dict[str, TextbookDownloadTask] = {}

    def _new_task(
        self,
        *,
        workspace_uuid: str,
        workspace_dir: Path,
        data_dir: Path,
        catalog: OfficialCatalog,
        books: list[OfficialBook],
        overwrite: bool,
        flat_names: dict[str, str] | None = None,
    ) -> TextbookDownloadTask:
        if workspace_uuid in self._workspace_tasks:
            existing = self._workspace_tasks[workspace_uuid]
            if existing.async_task and not existing.async_task.done():
                raise TextbookDownloadError("该工作区已有正在运行的教材下载任务")
            self._workspace_tasks.pop(workspace_uuid, None)
        if not books:
            raise TextbookDownloadError("没有选择要下载的教材")
        seen_keys: set[str] = set()
        for book in books:
            _validate_book_key(book)
            if book.content_id in seen_keys:
                raise TextbookDownloadError(f"教材重复选择: {book.content_id}")
            seen_keys.add(book.content_id)

        progress_books: dict[str, BookProgress] = {}
        catalog_books: dict[str, OfficialBook] = {}
        for book in books:
            flat = (flat_names or {}).get(book.content_id) or f"教材/{catalog.flat_filename(book)}"
            progress = BookProgress(
                book_key=book.content_id,
                title=book.title,
                target_path=flat,
                total_size=book.total_size,
            )
            progress_books[book.content_id] = progress
            catalog_books[book.content_id] = book

        task = TextbookDownloadTask(
            task_id=secrets.token_hex(4),
            workspace_uuid=workspace_uuid,
            workspace_dir=Path(workspace_dir),
            data_dir=Path(data_dir),
            source_commit=catalog.source_commit,
            overwrite=overwrite,
            books=progress_books,
            catalog_books=catalog_books,
            total_bytes=sum(book.total_size for book in books),
        )
        self._tasks[task.task_id] = task
        self._workspace_tasks[workspace_uuid] = task
        return task

    def start(
        self,
        *,
        workspace_uuid: str,
        workspace_dir: Path | str,
        data_dir: Path | str,
        catalog: OfficialCatalog,
        books: list[OfficialBook],
        overwrite: bool = False,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        flat_names: dict[str, str] | None = None,
    ) -> TextbookDownloadTask:
        """创建并调度一个下载任务。"""
        task = self._new_task(
            workspace_uuid=workspace_uuid,
            workspace_dir=Path(workspace_dir),
            data_dir=Path(data_dir),
            catalog=catalog,
            books=books,
            overwrite=overwrite,
            flat_names=flat_names,
        )
        task.async_task = asyncio.create_task(
            self._run(task, client_factory=client_factory)
        )
        return task

    def get(self, task_id: str) -> TextbookDownloadTask | None:
        return self._tasks.get(task_id)

    def status(self, task_id: str) -> dict[str, Any] | None:
        task = self._tasks.get(task_id)
        return task.public_dict() if task else None

    def failed_book_keys(self, task_id: str) -> list[str]:
        task = self._tasks.get(task_id)
        if not task:
            return []
        return [key for key, progress in task.books.items() if progress.status == "failed"]

    async def retry(
        self,
        task_id: str,
        *,
        book_keys: list[str] | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> TextbookDownloadTask:
        """为失败或未完成的教材创建新的下载任务。"""
        old = self._tasks.get(task_id)
        if not old:
            raise TextbookDownloadError("下载任务不存在")
        if book_keys is None:
            keys = self.failed_book_keys(task_id)
        else:
            keys = book_keys
        if not keys:
            raise TextbookDownloadError("没有需要重试的教材")
        books = [old.catalog_books[key] for key in keys if key in old.catalog_books]
        if len(books) != len(keys):
            raise TextbookDownloadError("包含不在原任务中的教材")
        flat_names = {key: p.target_path for key, p in old.books.items()}
        return self.start(
            workspace_uuid=old.workspace_uuid,
            workspace_dir=old.workspace_dir,
            data_dir=old.data_dir,
            catalog=_retry_catalog(old),
            books=books,
            overwrite=True,
            client_factory=client_factory,
            flat_names=flat_names,
        )

    async def cancel(self, task_id: str) -> dict[str, Any]:
        task = self._tasks.get(task_id)
        if not task:
            raise TextbookDownloadError("下载任务不存在")
        task.cancel_event.set()
        if task.async_task and not task.async_task.done():
            task.async_task.cancel()
            try:
                await task.async_task
            except asyncio.CancelledError:
                pass
        return task.public_dict()

    async def cancel_for_workspace(self, workspace_uuid: str) -> None:
        task = self._workspace_tasks.get(workspace_uuid)
        if task and task.async_task and not task.async_task.done():
            await self.cancel(task.task_id)

    async def shutdown_all(self) -> None:
        for task in list(self._tasks.values()):
            if task.async_task and not task.async_task.done():
                await self.cancel(task.task_id)

    async def _run(
        self,
        task: TextbookDownloadTask,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None,
    ) -> None:
        try:
            _ensure_disk_space(task.workspace_dir, task.total_bytes)
            _ensure_textbook_gitignore(task.workspace_dir)
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_BOOKS)
            factory = client_factory or _default_client_factory
            async with factory() as client:

                async def run_one(book: OfficialBook) -> None:
                    async with semaphore:
                        if task.cancel_event.is_set():
                            return
                        await self._download_book(task, book, client)

                results = await asyncio.gather(
                    *(run_one(book) for book in task.catalog_books.values()),
                    return_exceptions=True,
                )
            failures = [item for item in results if isinstance(item, BaseException)]
            failed_books = [
                progress for progress in task.books.values()
                if progress.status == "failed"
            ]
            if task.cancel_event.is_set():
                task.status = "cancelled"
            elif failures or failed_books:
                task.status = "partial_failed"
                task.error = f"{len(failures) + len(failed_books)} 本教材下载失败"
            else:
                task.status = "done"
        except asyncio.CancelledError:
            task.status = "cancelled"
            task.error = "下载已取消"
            for progress in task.books.values():
                if progress.status in ("pending", "downloading"):
                    progress.status = "cancelled"
            _save_task_manifest(task)
            raise
        except TextbookDownloadError as e:
            task.status = "failed"
            task.error = str(e)
        except Exception as e:  # noqa: BLE001 —— 后台任务不允许无声消失
            logger.exception("[textbooks] 教材下载任务异常")
            task.status = "failed"
            task.error = f"下载任务异常: {e}"
        finally:
            task.touch()
            _save_task_manifest(task)

    async def _download_book(
        self,
        task: TextbookDownloadTask,
        book: OfficialBook,
        client: httpx.AsyncClient,
    ) -> None:
        progress = task.books[book.content_id]
        progress.status = "downloading"
        task.touch()
        try:
            target = _target_path(task.workspace_dir, progress.target_path, book.content_id)
            temp_dir = task.data_dir / "tmp" / "textbooks" / book.content_id
            temp_dir.mkdir(parents=True, exist_ok=True)

            if target.exists() and not task.overwrite:
                if not book.total_size or target.stat().st_size == book.total_size:
                    progress.status = "skipped"
                    size = target.stat().st_size
                    progress.total_size = progress.downloaded_bytes = size
                    task.downloaded_bytes += size
                    progress.sha256 = _file_sha256(target)
                    _record_book(task.workspace_dir, task.source_commit, progress)
                    return
                raise TextbookDownloadError(f"目标教材已存在且大小不符: {target}")

            source = await asyncio.to_thread(fetch_book_source, book.content_id)
            url = _validate_source_url(str(source["url"]))
            expected_size = int(source.get("size") or book.total_size or 0)

            part_path = temp_dir / "source.part"
            done_path = temp_dir / "source.done"
            if done_path.is_file():
                if not expected_size or done_path.stat().st_size == expected_size:
                    part_path = done_path
                else:
                    done_path.unlink(missing_ok=True)
            if part_path.is_file() and expected_size and part_path.stat().st_size > expected_size:
                part_path.unlink(missing_ok=True)

            if not (done_path.is_file() and not expected_size):
                part_path = await self._download_source(
                    task, book, url, expected_size, temp_dir, client,
                )

            size = part_path.stat().st_size
            if expected_size and size != expected_size:
                raise TextbookDownloadError(
                    f"文件大小不符: 实际 {size}，平台 {expected_size}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(part_path, target)
            progress.status = "done"
            progress.total_size = progress.downloaded_bytes = size
            progress.sha256 = _file_sha256(target)
            task.downloaded_bytes += size
            shutil.rmtree(temp_dir, ignore_errors=True)
            _record_book(task.workspace_dir, task.source_commit, progress)
        except asyncio.CancelledError:
            progress.status = "cancelled"
            raise
        except TextbookDownloadError as e:
            progress.status = "failed"
            progress.error = str(e)
            _record_book(task.workspace_dir, task.source_commit, progress)
        except Exception as e:  # noqa: BLE001 —— 单本书失败不影响其它书
            logger.exception("[textbooks] 教材下载失败: %s", book.content_id)
            progress.status = "failed"
            progress.error = f"下载失败: {e}"
            _record_book(task.workspace_dir, task.source_commit, progress)

    async def _download_source(
        self,
        task: TextbookDownloadTask,
        book: OfficialBook,
        url: str,
        expected_size: int,
        temp_dir: Path,
        client: httpx.AsyncClient,
    ) -> Path:
        """下载单本教材源文件（Range 续传 + 镜像回退 + 400 退避）。"""
        part_path = temp_dir / "source.part"
        done_path = temp_dir / "source.done"
        if done_path.is_file():
            return done_path
        candidates = _cdn_candidates(url)
        last_error: Exception | None = None
        same_url_retry = 0

        for attempt in range(MAX_ATTEMPTS):
            if task.cancel_event.is_set():
                raise asyncio.CancelledError()
            candidate = candidates[attempt % len(candidates)]
            offset = part_path.stat().st_size if part_path.exists() else 0
            if expected_size and offset > expected_size:
                offset = 0
                part_path.unlink(missing_ok=True)
            headers = _signed_headers(candidate)
            if offset:
                headers["Range"] = f"bytes={offset}-"
            try:
                async with client.stream("GET", candidate, headers=headers) as response:
                    if response.status_code == 400:
                        body = (await response.aread())[:200].decode("utf-8", "replace")
                        if same_url_retry < len(_SAME_URL_400_DELAYS):
                            delay = _SAME_URL_400_DELAYS[same_url_retry]
                            same_url_retry += 1
                            await asyncio.sleep(delay)
                            continue
                        if "InvalidArgument" in body:
                            clear_credentials()
                            raise TextbookDownloadError(
                                "平台登录已过期，请重新登录后再下载"
                            )
                        raise TextbookDownloadError("教材源文件下载失败: HTTP 400")
                    if response.status_code in (401, 403):
                        clear_credentials()
                        raise TextbookDownloadError("平台登录已过期，请重新登录后再下载")
                    if response.status_code == 416 and expected_size and offset == expected_size:
                        break
                    if response.status_code == 200 and offset:
                        offset = 0
                        part_path.unlink(missing_ok=True)
                    elif response.status_code not in (200, 206):
                        raise TextbookDownloadError(
                            f"教材源文件下载失败: HTTP {response.status_code}"
                        )
                    mode = "ab" if response.status_code == 206 else "wb"
                    with open(part_path, mode) as out:
                        async for block in response.aiter_bytes(DOWNLOAD_BLOCK_SIZE):
                            if task.cancel_event.is_set():
                                raise asyncio.CancelledError()
                            if not block:
                                continue
                            out.write(block)
                            task.books[book.content_id].downloaded_bytes += len(block)
                            task.downloaded_bytes += len(block)
                if expected_size and part_path.stat().st_size != expected_size:
                    raise TextbookDownloadError(
                        f"下载不完整: 实际 {part_path.stat().st_size}，平台 {expected_size}"
                    )
                os.replace(part_path, done_path)
                return done_path
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, OSError, TextbookDownloadError) as e:
                last_error = e
                logger.warning(
                    "[textbooks] 教材下载失败（第 %s 次，源 %s）: %s: %s",
                    attempt + 1, urlparse(candidate).hostname or candidate,
                    book.content_id, e,
                )
                if isinstance(e, TextbookDownloadError) and "登录" in str(e):
                    raise
                if attempt + 1 < MAX_ATTEMPTS:
                    await asyncio.sleep(
                        ATTEMPT_BACKOFF_SECONDS[min(attempt, len(ATTEMPT_BACKOFF_SECONDS) - 1)]
                    )
        raise TextbookDownloadError(f"教材下载失败: {book.content_id}: {last_error}")


def _retry_catalog(task: TextbookDownloadTask) -> OfficialCatalog:
    """从任务元数据构造最小目录，供重试使用。"""
    return OfficialCatalog({
        "fetched_at": task.source_commit,
        "books": [asdict(book) for book in task.catalog_books.values()],
    })


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _save_task_manifest(task: TextbookDownloadTask) -> None:
    try:
        manifest = _load_manifest(task.workspace_dir)
        manifest["source_commit"] = task.source_commit
        manifest["task_status"] = task.status
        manifest["task_error"] = task.error
        manifest["task_updated_at"] = task.updated_at
        for progress in task.books.values():
            manifest["books"][progress.book_key] = {
                "title": progress.title,
                "target_path": progress.target_path,
                "status": progress.status,
                "bytes": progress.total_size,
                "sha256": progress.sha256,
                "chunk_count": 1,
                "split": False,
                "error": progress.error,
                "updated_at": _now(),
            }
        _save_manifest(task.workspace_dir, manifest)
    except Exception as e:  # noqa: BLE001 —— manifest 失败不影响主流程
        logger.warning("[textbooks] 更新教材 manifest 失败: %s", e)


def _default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        follow_redirects=True,
        headers={"User-Agent": "cili-textbook-downloader/1.0"},
    )


textbook_tasks = TextbookTaskManager()
