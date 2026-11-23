"""官方教材目录、源文件下载与进度任务。"""

from core.textbooks.downloader import (
    TextbookDownloadError,
    TextbookTaskManager,
    textbook_tasks,
)
from core.textbooks.pack_store import dataset_catalog as get_pack_catalog
from core.textbooks.smartedu import (
    OfficialBook,
    OfficialCatalog,
    SmartEduError,
    fetch_book_source,
    normalize_subject,
)
from core.textbooks.smartedu import (
    get_catalog as get_platform_catalog,
)

__all__ = [
    "OfficialBook",
    "OfficialCatalog",
    "SmartEduError",
    "fetch_book_source",
    "normalize_subject",
    "get_pack_catalog",
    "get_platform_catalog",
    "TextbookDownloadError",
    "TextbookTaskManager",
    "textbook_tasks",
]
