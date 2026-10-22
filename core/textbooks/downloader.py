"""教材下载任务：流式下载分片、断点续传、重试与进度。

任务由 FastAPI 路由创建后在当前事件循环里后台执行；不阻塞工作区创建。
所有 URL 都必须来自教材清单并指向固定提交的 raw 地址；不接受用户传入的
任意 URL。直连失败按固定镜像前缀回退（ghproxy.net → ghfast.top → gh-proxy.com，
与升级包下载共用同一列表），续传的 ``.part`` 文件在切换源时继续使用。目标文件
平铺写到工作区 ``教材/`` 下（重名自动加版本前缀），分片与临时文件写在
工作区 ``.cili/tmp/textbooks/<book_key>/``。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from core.fs_utils import atomic_write_json, load_json_or_backup
from core.textbooks.catalog import CatalogBook, TextbookCatalog
from core.textbooks.merge import merge_textbook_chunks

logger = logging.getLogger(__name__)

MANIFEST_NAME = ".manifest.json"
MANIFEST_SCHEMA_VERSION = 1
DOWNLOAD_HOST = "raw.githubusercontent.com"
BOOK_KEY_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_CONCURRENT_BOOKS = 2
MAX_ATTEMPTS = 5
ATTEMPT_BACKOFF_SECONDS = (0.25, 0.5, 1.0, 2.0, 2.0)
DOWNLOAD_BLOCK_SIZE = 256 * 1024

# GitHub 直连失败后的镜像回退前缀（与 core/updater.py / scripts/upgrade.ps1 的镜像列表一致）
GITHUB_MIRROR_PREFIXES = (
    "https://ghproxy.net/",
    "https://ghfast.top/",
    "https://gh-proxy.com/",
)


def _candidate_urls(url: str) -> list[str]:
    """直连 + 镜像回退列表；镜像仅做前缀改写，原始 URL 仍受清单校验约束。"""
    return [url] + [prefix + url for prefix in GITHUB_MIRROR_PREFIXES]


def _source_label(candidate: str, url: str) -> str:
    if candidate == url:
        return "直连"
    return candidate.split("/")[2] if "://" in candidate else candidate
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
    chunk_count: int
    split: bool
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
    raw_base: str
    overwrite: bool
    books: dict[str, BookProgress]
    catalog_books: dict[str, CatalogBook]
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


def _validate_book_key(book: CatalogBook) -> None:
    if not BOOK_KEY_RE.fullmatch(book.book_key):
        raise TextbookDownloadError(f"教材 book_key 非法: {book.book_key}")


def _validate_file_url(book: CatalogBook, file: dict[str, Any], raw_base: str) -> str:
    url = str(file.get("url") or "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != DOWNLOAD_HOST:
        raise TextbookDownloadError(f"教材下载 URL 非法: {book.book_key}")
    if raw_base and not url.startswith(raw_base):
        raise TextbookDownloadError(f"教材下载 URL 不属于清单提交: {book.book_key}")
    return url


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


def _record_book(
    workspace_dir: Path,
    source_commit: str,
    progress: BookProgress,
) -> None:
    manifest = _load_manifest(workspace_dir)
    manifest["source_commit"] = source_commit
    manifest["books"][progress.book_key] = {
        "title": progress.title,
        "target_path": progress.target_path,
        "status": progress.status,
        "bytes": progress.total_size,
        "sha256": progress.sha256,
        "chunk_count": progress.chunk_count,
        "split": progress.split,
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
        catalog: TextbookCatalog,
        books: list[CatalogBook],
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
            if book.book_key in seen_keys:
                raise TextbookDownloadError(f"教材重复选择: {book.book_key}")
            seen_keys.add(book.book_key)

        progress_books: dict[str, BookProgress] = {}
        catalog_books: dict[str, CatalogBook] = {}
        for book in books:
            flat = (flat_names or {}).get(book.book_key) or f"教材/{catalog.flat_filename(book)}"
            progress = BookProgress(
                book_key=book.book_key,
                title=book.title,
                target_path=flat,
                total_size=book.total_size,
                chunk_count=book.chunk_count,
                split=book.split,
            )
            progress_books[book.book_key] = progress
            catalog_books[book.book_key] = book

        task = TextbookDownloadTask(
            task_id=secrets.token_hex(4),
            workspace_uuid=workspace_uuid,
            workspace_dir=Path(workspace_dir),
            data_dir=Path(data_dir),
            source_commit=catalog.source_commit,
            raw_base=catalog.raw_base,
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
        catalog: TextbookCatalog,
        books: list[CatalogBook],
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
            # retry 只重建下载行为；catalog 元数据来自原任务，避免重新读盘。
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

                async def run_one(book: CatalogBook) -> None:
                    async with semaphore:
                        if task.cancel_event.is_set():
                            return
                        await self._download_book(task, book, client)

                results = await asyncio.gather(
                    *(run_one(book) for book in task.catalog_books.values()),
                    return_exceptions=True,
                )
            # 单本书失败在 _download_book 内转为 failed 状态；这里按最终
            # 状态汇总，避免异常被吞掉后误报 done。
            failures = [item for item in results if isinstance(item, BaseException)]
            failed_books = [
                progress for progress in task.books.values()
                if progress.status == "failed"
            ]
            if task.cancel_event.is_set():
                task.status = "cancelled"
            elif failures or failed_books:
                task.status = "partial_failed"
                task.error = f"{len(failures) + len(failed_books)} 本教材下载或合并失败"
            else:
                task.status = "done"
        except asyncio.CancelledError:
            task.status = "cancelled"
            task.error = "下载已取消"
            for progress in task.books.values():
                if progress.status in ("pending", "downloading", "merging"):
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
        book: CatalogBook,
        client: httpx.AsyncClient,
    ) -> None:
        progress = task.books[book.book_key]
        progress.status = "downloading"
        task.touch()
        try:
            target = _target_path(task.workspace_dir, progress.target_path, book.book_key)
            temp_dir = task.data_dir / "tmp" / "textbooks" / book.book_key
            temp_dir.mkdir(parents=True, exist_ok=True)
            self._recount_downloaded(task, book, temp_dir)

            if target.exists() and not task.overwrite:
                if target.stat().st_size == book.total_size:
                    progress.status = "skipped"
                    progress.downloaded_bytes = book.total_size
                    task.downloaded_bytes += book.total_size
                    progress.sha256 = _file_sha256(target)
                    _record_book(task.workspace_dir, task.source_commit, progress)
                    return
                raise TextbookDownloadError(f"目标教材已存在且大小不符: {target}")

            chunk_paths: list[Path] = []
            for file in sorted(book.files, key=lambda item: int(item.get("index", 0))):
                _validate_file_url(book, file, task.raw_base)
                chunk_paths.append(await self._download_chunk(task, book, file, temp_dir, client))

            progress.status = "merging"
            task.touch()
            result = merge_textbook_chunks(
                chunk_paths,
                target,
                expected_size=book.total_size,
                overwrite=True,
            )
            progress.status = "done"
            progress.downloaded_bytes = result.size
            progress.sha256 = result.sha256
            self._recount_downloaded(task, book, temp_dir, final_size=result.size)
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
            logger.exception("[textbooks] 教材下载失败: %s", book.book_key)
            progress.status = "failed"
            progress.error = f"下载或合并失败: {e}"
            _record_book(task.workspace_dir, task.source_commit, progress)

    async def _download_chunk(
        self,
        task: TextbookDownloadTask,
        book: CatalogBook,
        file: dict[str, Any],
        temp_dir: Path,
        client: httpx.AsyncClient,
    ) -> Path:
        try:
            index = int(file.get("index", 0))
            expected_size = int(file.get("size", 0))
        except (TypeError, ValueError) as e:
            raise TextbookDownloadError(f"分片序号或大小非法: {book.book_key}") from e
        if index <= 0 or expected_size <= 0:
            raise TextbookDownloadError(f"分片序号或大小非法: {book.book_key}")
        url = _validate_file_url(book, file, task.raw_base)
        candidates = _candidate_urls(url)

        part_path = temp_dir / f"{index:04d}.part"
        done_path = temp_dir / f"{index:04d}.done"
        if done_path.is_file():
            if done_path.stat().st_size == expected_size:
                self._recount_downloaded(task, book, temp_dir)
                return done_path
            done_path.unlink(missing_ok=True)
        if part_path.is_file() and part_path.stat().st_size > expected_size:
            part_path.unlink(missing_ok=True)

        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            if task.cancel_event.is_set():
                raise asyncio.CancelledError()
            candidate = candidates[attempt % len(candidates)]
            offset = part_path.stat().st_size if part_path.exists() else 0
            if offset > expected_size:
                offset = 0
                part_path.unlink(missing_ok=True)
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                async with client.stream("GET", candidate, headers=headers) as response:
                    if response.status_code == 416 and offset == expected_size:
                        break
                    if response.status_code == 200 and offset:
                        # 服务端不支持 Range：从头下载。
                        offset = 0
                        part_path.unlink(missing_ok=True)
                        self._recount_downloaded(task, book, temp_dir)
                    elif response.status_code not in (200, 206):
                        raise TextbookDownloadError(
                            f"分片下载失败: HTTP {response.status_code}"
                        )
                    mode = "ab" if response.status_code == 206 else "wb"
                    with open(part_path, mode) as out:
                        async for block in response.aiter_bytes(DOWNLOAD_BLOCK_SIZE):
                            if task.cancel_event.is_set():
                                raise asyncio.CancelledError()
                            if not block:
                                continue
                            out.write(block)
                            task.books[book.book_key].downloaded_bytes += len(block)
                            task.downloaded_bytes += len(block)
                if part_path.stat().st_size != expected_size:
                    raise TextbookDownloadError(
                        f"分片大小不符: 实际 {part_path.stat().st_size}，清单 {expected_size}"
                    )
                os.replace(part_path, done_path)
                return done_path
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, OSError, TextbookDownloadError) as e:
                last_error = e
                logger.warning(
                    "[textbooks] 分片下载失败（第 %s 次，源 %s）: %s/%s: %s",
                    attempt + 1, _source_label(candidate, url), book.book_key, index, e,
                )
                if attempt + 1 < MAX_ATTEMPTS:
                    await asyncio.sleep(ATTEMPT_BACKOFF_SECONDS[min(attempt, len(ATTEMPT_BACKOFF_SECONDS) - 1)])
        raise TextbookDownloadError(
            f"分片下载失败: {book.book_key}/{index}: {last_error}"
        )


    def _recount_downloaded(
        self,
        task: TextbookDownloadTask,
        book: CatalogBook,
        temp_dir: Path,
        *,
        final_size: int | None = None,
    ) -> None:
        """按临时文件真实大小重算一本书的进度，避免续传重复计数。"""
        if final_size is not None:
            actual = final_size
        else:
            actual = 0
            for file in book.files:
                try:
                    index = int(file.get("index", 0))
                    expected_size = int(file.get("size", 0))
                except (TypeError, ValueError):
                    continue
                done_path = temp_dir / f"{index:04d}.done"
                part_path = temp_dir / f"{index:04d}.part"
                if done_path.is_file():
                    actual += min(done_path.stat().st_size, expected_size)
                elif part_path.is_file():
                    actual += min(part_path.stat().st_size, expected_size)
        progress = task.books[book.book_key]
        delta = actual - progress.downloaded_bytes
        progress.downloaded_bytes = actual
        task.downloaded_bytes = max(0, task.downloaded_bytes + delta)


def _retry_catalog(task: TextbookDownloadTask) -> TextbookCatalog:
    """从任务元数据构造最小 catalog，供重试使用。"""
    return TextbookCatalog({
        "source": {"commit": task.source_commit, "raw_base": task.raw_base},
        "download_policy": {},
        "merge": {},
        "books": [book.__dict__ for book in task.catalog_books.values()],
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
                "chunk_count": progress.chunk_count,
                "split": progress.split,
                "error": progress.error,
                "updated_at": _now(),
            }
        _save_manifest(task.workspace_dir, manifest)
    except Exception as e:  # noqa: BLE001 —— manifest 失败不影响主流程
        logger.warning("[textbooks] 更新教材 manifest 失败: %s", e)


def _default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        follow_redirects=False,
        headers={"User-Agent": "cili-textbook-downloader/1.0"},
    )


textbook_tasks = TextbookTaskManager()
