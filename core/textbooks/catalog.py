"""ChinaTextbook 教材清单加载与匹配。

清单由 ``scripts/build_china_textbook_catalog.py`` 从固定提交生成，路径为
``core/templates/textbooks/china_textbook_catalog.json``。本模块只做读取、
基础校验与查询，不访问网络。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "templates"
    / "textbooks"
    / "china_textbook_catalog.json"
)

DEFAULT_BOOK_TYPES = ("textbook",)
HIGH_SCHOOL = "高中"
SUBJECT_ALIASES = {
    "生物学": "生物",
    "体育与健康": "体育",
    "人文地理": "地理",
    "语文·书法练习指导": "语文",
}


def normalize_subject(subject: str) -> str:
    """把常见学科别名映射为清单里的规范学科名。"""
    return SUBJECT_ALIASES.get(subject, subject)


def _load_edition_publishers() -> dict[str, str]:
    """从模板目录加载版本 → 出版单位核对数据（由 scripts/build_china_textbook_editions.py 生成）。"""
    path = Path(__file__).resolve().parents[1] / "templates" / "textbooks" / "china_textbook_editions.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    editions = data.get("editions")
    if not isinstance(editions, list):
        return {}
    return {
        item["name"]: str(item.get("publisher") or "")
        for item in editions
        if isinstance(item, dict) and item.get("name")
    }


def edition_sort_key(name: str) -> tuple[int, bytes]:
    """版本名的展示排序键：GB2312 编码序近似拼音序；生僻字兜底按 UTF-8。"""
    try:
        return (0, name.encode("gb2312"))
    except UnicodeEncodeError:
        return (1, name.encode("utf-8"))


# 合并册年级标签覆盖的实际年级；空年级（全一册/未标注）视为覆盖所有年级
GRADE_COVERAGE: dict[str, tuple[str, ...]] = {
    "1至2年级": ("一年级", "二年级"),
    "3至4年级": ("三年级", "四年级"),
    "5至6年级": ("五年级", "六年级"),
}


# 教材版本简称 → 出版单位（说明）。数据文件随仓库分发；加载失败时下拉退化为只显示简称。
EDITION_PUBLISHERS: dict[str, str] = _load_edition_publishers()


class TextbookCatalogError(ValueError):
    """教材清单缺失或格式不合法。"""


@dataclass(frozen=True)
class CatalogBook:
    """清单中的一本书；保留下载需要的最小字段。"""

    id: str
    book_key: str
    stage: str
    system: str
    subject: str
    subject_raw: str
    edition: str
    edition_raw: str
    grade: str
    semester: str
    book_type: str
    high_type: str
    volume: str
    title: str
    source_path: str
    target_path: str
    split: bool
    chunk_count: int
    total_size: int
    files: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CatalogBook":
        required = (
            "id", "book_key", "stage", "system", "subject", "subject_raw",
            "edition", "edition_raw", "grade", "semester", "book_type",
            "high_type", "volume", "title", "source_path", "target_path",
            "split", "chunk_count", "total_size", "files",
        )
        missing = [key for key in required if key not in data]
        if missing:
            raise TextbookCatalogError(f"教材条目缺少字段: {', '.join(missing)}")
        files = tuple(data.get("files") or [])
        if not files:
            raise TextbookCatalogError(f"教材条目没有下载文件: {data.get('id', '')}")
        return cls(
            id=str(data["id"]),
            book_key=str(data["book_key"]),
            stage=str(data["stage"]),
            system=str(data["system"]),
            subject=str(data["subject"]),
            subject_raw=str(data["subject_raw"]),
            edition=str(data["edition"]),
            edition_raw=str(data["edition_raw"]),
            grade=str(data["grade"]),
            semester=str(data["semester"]),
            book_type=str(data["book_type"]),
            high_type=str(data["high_type"]),
            volume=str(data["volume"]),
            title=str(data["title"]),
            source_path=str(data["source_path"]),
            target_path=str(data["target_path"]),
            split=bool(data["split"]),
            chunk_count=int(data["chunk_count"]),
            total_size=int(data["total_size"]),
            files=files,
        )

    def public_dict(self) -> dict[str, Any]:
        """给前端展示用的摘要；不暴露下载 URL。"""
        return {
            "id": self.id,
            "book_key": self.book_key,
            "stage": self.stage,
            "system": self.system,
            "subject": self.subject,
            "subject_raw": self.subject_raw,
            "edition": self.edition,
            "edition_raw": self.edition_raw,
            "grade": self.grade,
            "semester": self.semester,
            "book_type": self.book_type,
            "high_type": self.high_type,
            "volume": self.volume,
            "title": self.title,
            "target_path": self.target_path,
            "split": self.split,
            "chunk_count": self.chunk_count,
            "total_size": self.total_size,
        }


class TextbookCatalog:
    """加载后的教材清单；提供按学段/学科/版本/年级/学期的匹配。"""

    def __init__(self, data: dict[str, Any]):
        source = data.get("source") or {}
        if not isinstance(source, dict) or not source.get("commit"):
            raise TextbookCatalogError("教材清单缺少 source.commit")
        policy = data.get("download_policy") or {}
        if not isinstance(policy, dict):
            policy = {}
        self.data = data
        self.source = source
        self.download_policy = policy
        self.merge_policy = data.get("merge") or {}
        raw_books = data.get("books")
        if not isinstance(raw_books, list):
            raise TextbookCatalogError("教材清单缺少 books 数组")
        self.books = [CatalogBook.from_dict(item) for item in raw_books]
        self._by_key = {book.book_key: book for book in self.books}
        self._by_id = {book.id: book for book in self.books}
        if len(self._by_key) != len(self.books):
            raise TextbookCatalogError("教材清单存在重复 book_key")
        self._flat_names = self._build_flat_names()

    def _build_flat_names(self) -> dict[str, str]:
        """平铺到工作区「教材/」时的文件名；重名依次加版本、年级前缀。"""
        names: dict[str, str] = {}
        used: set[str] = set()
        for book in self.books:
            orig = book.target_path.replace("\\", "/").rsplit("/", 1)[-1]
            candidates = [orig]
            if book.edition and not orig.startswith(book.edition):
                candidates.append(f"{book.edition}-{orig}")
            if book.grade:
                candidates.append(f"{book.edition}-{book.grade}-{orig}".lstrip("-"))
            candidates.append(f"{book.book_key}-{orig}")
            chosen = candidates[-1]
            for candidate in candidates:
                if candidate not in used:
                    chosen = candidate
                    break
            names[book.book_key] = chosen
            used.add(chosen)
        return names

    def flat_filename(self, book: CatalogBook) -> str:
        """工作区「教材/」目录下的平铺文件名。"""
        fallback = book.target_path.replace("\\", "/").rsplit("/", 1)[-1]
        return self._flat_names.get(book.book_key) or fallback

    @property
    def source_commit(self) -> str:
        return str(self.source.get("commit", ""))

    @property
    def raw_base(self) -> str:
        return str(self.source.get("raw_base", ""))

    @property
    def default_book_types(self) -> tuple[str, ...]:
        types = self.download_policy.get("default_book_types") or list(DEFAULT_BOOK_TYPES)
        return tuple(types)

    def editions(
        self,
        *,
        stage: str = "",
        system: str = "",
        subject: str = "",
        grade: str = "",
        book_types: tuple[str, ...] = DEFAULT_BOOK_TYPES,
    ) -> list[dict[str, Any]]:
        """列出可选教材版本；按版本名称排序（近似拼音序），可按学制/年级过滤。

        年级过滤规则：版本下任一本书的年级标注覆盖所选年级即保留；合并册
        （如「1至2年级」）按 ``GRADE_COVERAGE`` 展开；无年级标注（全一册、
        高中等）视为覆盖所有年级。
        """
        grouped: dict[str, dict[str, Any]] = {}
        subject = normalize_subject(subject)
        for book in self.books:
            if stage and book.stage != stage:
                continue
            if system and book.system != system:
                continue
            if subject and book.subject != subject:
                continue
            if grade and book.grade and book.grade != grade and grade not in GRADE_COVERAGE.get(book.grade, ()):
                continue
            if book_types and book.book_type not in book_types:
                continue
            item = grouped.setdefault(book.edition_raw, {
                "edition": book.edition,
                "edition_raw": book.edition_raw,
                "count": 0,
                "total_size": 0,
            })
            item["count"] += 1
            item["total_size"] += book.total_size
        return sorted(
            grouped.values(),
            key=lambda item: edition_sort_key(item["edition_raw"]),
        )

    def subjects(
        self,
        *,
        stage: str = "",
        system: str = "",
        book_types: tuple[str, ...] = DEFAULT_BOOK_TYPES,
    ) -> list[dict[str, Any]]:
        """列出可选学科；按书数量排序。"""
        grouped: dict[str, dict[str, Any]] = {}
        for book in self.books:
            if stage and book.stage != stage:
                continue
            if system and book.system != system:
                continue
            if book_types and book.book_type not in book_types:
                continue
            item = grouped.setdefault(book.subject, {
                "subject": book.subject,
                "subject_raw": book.subject_raw,
                "count": 0,
                "total_size": 0,
            })
            item["count"] += 1
            item["total_size"] += book.total_size
        return sorted(
            grouped.values(),
            key=lambda item: (-item["count"], item["subject"]),
        )

    def find_books(
        self,
        *,
        stage: str = "",
        system: str = "",
        subject: str = "",
        edition: str = "",
        grade: str = "",
        semester: str = "",
        high_scope: str = "all_textbook_volumes",
        book_types: tuple[str, ...] = DEFAULT_BOOK_TYPES,
        book_keys: tuple[str, ...] = (),
    ) -> list[CatalogBook]:
        """匹配教材。

        ``edition`` 优先精确匹配 ``edition_raw``；为空时返回该组合全部版本。
        高中 ``high_scope`` 支持 ``all_textbook_volumes`` / ``required_only`` /
        ``manual``；``manual`` 不做必修/选择性必修过滤，由用户逐本勾选。
        """
        book_types = tuple(book_types or ())
        subject = normalize_subject(subject)
        results: list[CatalogBook] = []
        for book in self.books:
            if book_keys:
                if book.book_key not in book_keys:
                    continue
            else:
                if stage and book.stage != stage:
                    continue
                if system and book.system != system:
                    continue
                if subject and book.subject != subject:
                    continue
                if edition and edition not in (book.edition, book.edition_raw):
                    continue
                if grade and book.grade != grade:
                    continue
                if semester and book.semester != semester:
                    continue
                if book_types and book.book_type not in book_types:
                    continue
                if book.stage == HIGH_SCHOOL and high_scope == "required_only":
                    if book.high_type != "必修":
                        continue
            results.append(book)
        return results


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime_ns: int, size: int) -> TextbookCatalog:
    del mtime_ns, size  # 仅作为缓存失效指纹
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise TextbookCatalogError(f"教材清单不存在: {path}") from e
    except (json.JSONDecodeError, OSError) as e:
        raise TextbookCatalogError(f"教材清单读取失败: {e}") from e
    return TextbookCatalog(data)


def get_catalog(path: Path | str = CATALOG_PATH) -> TextbookCatalog:
    """加载教材清单；按文件 mtime/size 缓存。"""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError as e:
        raise TextbookCatalogError(f"教材清单不存在: {path}") from e
    return _load_cached(str(path), stat.st_mtime_ns, stat.st_size)
