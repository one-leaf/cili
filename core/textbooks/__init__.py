"""教材清单查询、分片合并与下载任务。"""

from core.textbooks.catalog import (
    CatalogBook,
    TextbookCatalog,
    TextbookCatalogError,
    get_catalog,
)
from core.textbooks.downloader import (
    TextbookDownloadError,
    TextbookTaskManager,
    textbook_tasks,
)
from core.textbooks.merge import merge_textbook_chunks

__all__ = [
    "CatalogBook",
    "TextbookCatalog",
    "TextbookCatalogError",
    "get_catalog",
    "merge_textbook_chunks",
    "TextbookDownloadError",
    "TextbookTaskManager",
    "textbook_tasks",
]
