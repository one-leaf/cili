"""国家中小学智慧教育平台教材目录（官方 JSON 抓取 + 分阶段缓存）。

数据源是平台前端自用的静态 JSON（无需登录）：
- 标签树：``zxx/ndrs/tags/tch_material_tag.json``
- 版本清单：``zxx/ndrs/resources/tch_material/version/data_version.json``
  返回逗号分隔的分片 URL，逐片拉取得到课本数组。

教材 PDF 位于私有 CDN（``r1/r2/r3-ndr-private.ykt.cbern.com.cn``），下载需要
平台登录凭据（access_token + mac_key）并按每条 URL 现算 ``X-ND-AUTH`` MAC 签名。

缓存策略：内存 + 磁盘（``data/cili/cache/smartedu_catalog.json``），TTL 24 小时；
过期时先用旧缓存并在后台线程刷新，抓取失败继续沿用旧数据。首次无缓存时同步抓取。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

import requests

from core.config import PROJECT_ROOT
from core.fs_utils import atomic_write_json

logger = logging.getLogger(__name__)

TAGS_URL = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/tags/tch_material_tag.json"
DATA_VERSION_URL = (
    "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/resources/tch_material/version/data_version.json"
)
DETAIL_URL = (
    "https://s-file-1.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{content_id}.json"
)
DETAIL_PAGE = (
    "https://basic.smartedu.cn/tchMaterial/detail?contentType=assets_document"
    "&contentId={content_id}&catalogType=tchMaterial&subCatalog=tchMaterial"
)
PRIVATE_CDN_HOSTS = tuple(f"r{i}-ndr-private.ykt.cbern.com.cn" for i in range(1, 4))
CDN_PLACEHOLDER = "cs_path:${ref-path}"
REQUEST_TIMEOUT = (10, 60)

CACHE_PATH = PROJECT_ROOT / "data" / "cili" / "cache" / "smartedu_catalog.json"
CACHE_TTL_SECONDS = 24 * 3600

STAGE_ORDER = (
    "小学", "小学（五•四学制）", "初中", "初中（五•四学制）", "高中", "特殊教育", "幼儿",
)
GRADE_ORDER = (
    "一年级", "二年级", "三年级", "四年级", "五年级", "六年级",
    "七年级", "八年级", "九年级", "高一", "高二", "高三",
)
# 官方标签的年级别名（高中多数教材只标「高中年级」）
GRADE_ALIASES = {
    "高一年级": "高一",
    "高二年级": "高二",
    "高中年级": "高中",
}
# 合并册标签覆盖的标准年级
GRADE_COVERAGE = {
    "一至二年级": ("一年级", "二年级"),
    "三至四年级": ("三年级", "四年级"),
    "五至六年级": ("五年级", "六年级"),
    "七至九年级": ("七年级", "八年级", "九年级"),
}
VOLUME_ORDER = ("上册", "下册", "全一册", "上册、下册")

# 学科模板文件名使用的别名（官方学科名 → 模板命名）
SUBJECT_ALIASES = {
    "生物学": "生物",
    "体育与健康": "体育",
    "人文地理": "地理",
    "语文·书法练习指导": "语文",
}


def normalize_subject(subject: str) -> str:
    """把官方学科名映射为学科模板使用的规范名。"""
    return SUBJECT_ALIASES.get(subject, subject)

BASE_HEADERS = {
    "Origin": "https://basic.smartedu.cn",
    "Referer": "https://basic.smartedu.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "X-ND-AUTH": 'MAC id="0",nonce="0",mac="0"',
}


class SmartEduError(RuntimeError):
    """平台目录抓取或凭据缺失。"""


def _name_sort_key(name: str) -> tuple[int, bytes]:
    try:
        return (0, name.encode("gb2312"))
    except UnicodeEncodeError:
        return (1, name.encode("utf-8"))


def _order_index(value: str, order: Iterable[str]) -> int:
    try:
        return tuple(order).index(value)
    except ValueError:
        return len(tuple(order))


# ── 登录凭据与 MAC 签名 ────────────────────────────────────────────────────────

NONCE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class SmartEduCredentials:
    """平台登录凭据；mac_key 为空时只能发占位头（私有资源会 400/401）。"""

    access_token: str = ""
    mac_key: str = ""
    diff: int = 0

    @property
    def complete(self) -> bool:
        return bool(self.access_token and self.mac_key)


AUTH_CACHE_PATH = PROJECT_ROOT / "data" / "cili" / "cache" / "smartedu_auth.json"
_auth_memory: SmartEduCredentials | None = None


def load_credentials() -> SmartEduCredentials:
    """读取浏览器登录后写入缓存目录的平台凭据（长期有效，不进 setting.json）。

    登录一次后凭据写入 ``data/cili/cache/smartedu_auth.json`` 并跨重启复用；
    仅当平台返回认证失效时才清除并重新走浏览器登录。
    """
    global _auth_memory
    if _auth_memory is not None:
        return _auth_memory
    try:
        data = json.loads(AUTH_CACHE_PATH.read_text(encoding="utf-8"))
        creds = SmartEduCredentials(
            access_token=str(data.get("access_token") or ""),
            mac_key=str(data.get("mac_key") or ""),
            diff=int(data.get("diff") or 0),
        )
    except (OSError, ValueError, TypeError):
        creds = SmartEduCredentials()
    _auth_memory = creds
    return creds


def save_credentials(creds: SmartEduCredentials) -> None:
    """保存浏览器登录得到的凭据（本地运行时缓存，权限尽量收紧）。"""
    global _auth_memory
    _auth_memory = creds
    try:
        AUTH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        AUTH_CACHE_PATH.write_text(json.dumps({
            "access_token": creds.access_token,
            "mac_key": creds.mac_key,
            "diff": creds.diff,
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
        try:
            AUTH_CACHE_PATH.chmod(0o600)
        except OSError:
            pass
    except OSError as e:
        logger.warning(f"[smartedu] 写登录凭据失败: {e}")


def clear_credentials() -> None:
    global _auth_memory
    _auth_memory = None
    try:
        AUTH_CACHE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def credentials() -> SmartEduCredentials:
    return load_credentials()


def is_auth_expired(status_code: int, error_code: str = "") -> bool:
    """判断平台响应是否表示登录凭据失效（401/403 或 400 InvalidArgument）。"""
    return status_code in (401, 403) or (
        status_code == 400 and str(error_code).strip() == "InvalidArgument"
    )


def generate_nonce(diff: int = 0) -> str:
    """按平台前端逻辑生成 nonce：毫秒时间戳(含时差) + 冒号 + 8 位随机字符。"""
    suffix = "".join(
        NONCE_ALPHABET[math.ceil(35 * random.random())] for _ in range(8)
    )
    return f"{int(time.time() * 1000) + int(diff)}:{suffix}"


def signature_string(url: str, method: str, nonce: str) -> str:
    """签名原文：nonce\\nMETHOD\\n解码后的 path(+query)\\nhostname\\n（末尾空行）。"""
    parts = urlsplit(url)
    relative = unquote(parts.path) + (f"?{parts.query}" if parts.query else "")
    return f"{nonce}\n{method.upper()}\n{relative}\n{parts.hostname or ''}\n"


def sign_mac(text: str, mac_key: str) -> str:
    digest = hmac.new(
        mac_key.encode("utf-8"), text.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def build_nd_auth(
    url: str,
    method: str = "GET",
    creds: SmartEduCredentials | None = None,
) -> str:
    creds = creds or credentials()
    token_id = creds.access_token or "0"
    if not creds.mac_key:
        return f'MAC id="{token_id}",nonce="0",mac="0"'
    nonce = generate_nonce(creds.diff)
    mac = sign_mac(signature_string(url, method, nonce), creds.mac_key)
    return f'MAC id="{token_id}",nonce="{nonce}",mac="{mac}"'


def cdn_url(storage: str) -> str:
    """把平台存储地址换成 r1 私有 CDN；已是 https 的原样返回。"""
    if not storage:
        return ""
    if storage.startswith(CDN_PLACEHOLDER):
        return storage.replace(CDN_PLACEHOLDER, "https://r1-ndr-private.ykt.cbern.com.cn")
    return storage


# ── 目录数据模型 ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OfficialBook:
    """平台教材条目；下载所需文件在下载时按 content_id 查详情接口获得。"""

    content_id: str
    title: str
    stage: str
    subject: str          # 官方学科原名
    edition: str          # 版本（可能为空）
    grade: str            # 年级（可能为空）
    volume: str           # 册次
    total_size: int
    url: str              # 平台详情页

    @property
    def book_key(self) -> str:
        return self.content_id

    @property
    def system(self) -> str:
        return "五四制" if "五•四学制" in self.stage else "六三制"

    @property
    def subject_raw(self) -> str:
        return self.subject

    @property
    def edition_raw(self) -> str:
        return self.edition

    @property
    def semester(self) -> str:
        return self.volume

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.content_id,
            "book_key": self.content_id,
            "stage": self.stage,
            "system": self.system,
            "subject": self.subject,
            "subject_raw": self.subject,
            "edition": self.edition,
            "edition_raw": self.edition,
            "grade": self.grade,
            "semester": self.volume,
            "book_type": "textbook",
            "high_type": "",
            "volume": self.volume,
            "title": self.title,
            "target_path": f"教材/{self.title}.pdf",
            "split": False,
            "chunk_count": 1,
            "total_size": self.total_size,
        }


class OfficialCatalog:
    """官方教材目录；提供与旧清单一致的查询接口。"""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.fetched_at = str(data.get("fetched_at") or "")
        raw_books = data.get("books")
        if not isinstance(raw_books, list):
            raise SmartEduError("平台目录缺少 books 数组")
        books: list[OfficialBook] = []
        for item in raw_books:
            try:
                books.append(OfficialBook(
                    content_id=str(item["content_id"]),
                    title=str(item["title"]),
                    stage=str(item["stage"]),
                    subject=str(item["subject"]),
                    edition=str(item.get("edition") or ""),
                    grade=str(item.get("grade") or ""),
                    volume=str(item.get("volume") or ""),
                    total_size=int(item.get("total_size") or 0),
                    url=str(item.get("url") or DETAIL_PAGE.format(content_id=item["content_id"])),
                ))
            except (KeyError, TypeError, ValueError) as e:
                raise SmartEduError(f"平台目录条目非法: {e}") from e
        self.books = books
        self._by_key = {book.content_id: book for book in books}
        self._flat_names = self._build_flat_names()

    @property
    def source_commit(self) -> str:
        return self.fetched_at

    @property
    def raw_base(self) -> str:
        return ""

    @property
    def default_book_types(self) -> tuple[str, ...]:
        return ("textbook",)

    def _build_flat_names(self) -> dict[str, str]:
        names: dict[str, str] = {}
        used: set[str] = set()
        for book in self.books:
            base = _sanitize_filename(f"{book.title}.pdf")
            candidates = [base]
            if book.edition:
                candidates.append(_sanitize_filename(f"{book.edition}-{base}"))
            if book.grade:
                candidates.append(_sanitize_filename(f"{book.edition}-{book.grade}-{base}".lstrip("-")))
            candidates.append(f"{book.content_id[:8]}-{base}")
            chosen = candidates[-1]
            for candidate in candidates:
                if candidate not in used:
                    chosen = candidate
                    break
            names[book.content_id] = chosen
            used.add(chosen)
        return names

    def flat_filename(self, book: OfficialBook) -> str:
        return self._flat_names.get(book.content_id) or _sanitize_filename(f"{book.title}.pdf")

    # ── 查询接口（与旧教材清单保持同名签名，便于路由复用） ──

    def stage_options(self) -> list[str]:
        stages = {book.stage for book in self.books}
        return sorted(stages, key=lambda s: (_order_index(s, STAGE_ORDER), s))

    def grades(self, stage: str = "") -> list[str]:
        grades = {
            book.grade for book in self.books
            if book.grade and (not stage or book.stage == stage)
        }
        return sorted(grades, key=lambda g: (_order_index(g, GRADE_ORDER), g))

    def subjects(self, *, stage: str = "", grade: str = "", system: str = "",
                 book_types: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        del system, book_types  # 兼容旧调用签名
        grouped: dict[str, dict[str, Any]] = {}
        for book in self.books:
            if stage and book.stage != stage:
                continue
            if grade and book.grade and book.grade != grade:
                continue
            item = grouped.setdefault(book.subject, {
                "subject": book.subject,
                "subject_raw": book.subject,
                "count": 0,
                "total_size": 0,
            })
            item["count"] += 1
            item["total_size"] += book.total_size
        return sorted(
            grouped.values(),
            key=lambda item: (_order_index(item["subject"], ()), _name_sort_key(item["subject"])),
        )

    def editions(self, *, stage: str = "", subject: str = "", grade: str = "",
                 system: str = "", book_types: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        del system, book_types
        grouped: dict[str, dict[str, Any]] = {}
        for book in self.books:
            if stage and book.stage != stage:
                continue
            if subject and book.subject != subject:
                continue
            if grade and book.grade and book.grade != grade:
                continue
            if not book.edition:
                continue
            item = grouped.setdefault(book.edition, {
                "edition": book.edition,
                "edition_raw": book.edition,
                "count": 0,
                "total_size": 0,
                "grades": set(),
            })
            item["count"] += 1
            item["total_size"] += book.total_size
            item["grades"].add(book.grade or "")

        stage_grades = {
            g for g in self.grades(stage)
            if g in GRADE_ORDER
        } if stage else set()

        def display_grades(raw: set[str]) -> list[str]:
            """把官方年级标签归一化为覆盖的标准年级（供前端按年级过滤版本）。"""
            covered: set[str] = set()
            for g in raw:
                alias = GRADE_ALIASES.get(g, g)
                if alias in GRADE_ORDER:
                    covered.add(alias)
                covered.update(GRADE_COVERAGE.get(g, ()))
                if g in ("", "高中年级"):
                    covered.update(("高一", "高二", "高三"))
            if not covered:
                covered = set(stage_grades)
            return sorted(covered, key=lambda g: (_order_index(g, GRADE_ORDER), g))

        return [
            {**item, "grades": display_grades(item["grades"])}
            for item in sorted(grouped.values(), key=lambda i: _name_sort_key(i["edition"]))
        ]

    def find_books(self, *, stage: str = "", system: str = "", subject: str = "",
                   edition: str = "", grade: str = "", semester: str = "",
                   high_scope: str = "", book_types: tuple[str, ...] = (),
                   book_keys: tuple[str, ...] = ()) -> list[OfficialBook]:
        del system, high_scope, book_types
        keys = set(book_keys or ())
        result: list[OfficialBook] = []
        for book in self.books:
            if keys:
                if book.content_id not in keys:
                    continue
            else:
                if stage and book.stage != stage:
                    continue
                if subject and book.subject != subject:
                    continue
                if edition and edition not in (book.edition,):
                    continue
                if grade and not self.grade_covers(book.grade, grade):
                    continue
                if semester and book.volume != semester:
                    continue
            result.append(book)
        return result

    def grade_covers(self, book_grade: str, selected: str) -> bool:
        """判断教材的年级标签是否覆盖用户选择的年级（别名、合并册、高中通用）。"""
        if not selected:
            return True
        if book_grade == selected:
            return True
        if GRADE_ALIASES.get(book_grade) == selected:
            return True
        if selected in GRADE_COVERAGE.get(book_grade, ()):
            return True
        if book_grade in ("", "高中年级") and selected in ("高一", "高二", "高三"):
            return True
        return False

    def get_by_key(self, content_id: str) -> OfficialBook | None:
        return self._by_key.get(content_id)


def _sanitize_filename(value: str) -> str:
    table = str.maketrans({
        "<": "＜", ">": "＞", ":": "：", '"': "＂", "/": "／",
        "\\": "＼", "|": "｜", "?": "？", "*": "＊",
    })
    cleaned = "".join("_" if ord(ch) < 32 else ch for ch in value.translate(table))
    cleaned = cleaned.rstrip(" .")
    return cleaned or "教材"


# ── 抓取 ──────────────────────────────────────────────────────────────────────

def _pick_source_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    def usable(item: dict[str, Any]) -> bool:
        fmt = str(item.get("ti_format") or "").lower()
        return fmt != "folder" and bool(item.get("ti_storage") or item.get("ti_storages"))

    for item in items:
        if item.get("ti_is_source_file") and usable(item):
            return item
    for item in items:
        if item.get("ti_file_flag") == "source" and usable(item):
            return item
    return None


def _extract_total_size(item: dict[str, Any]) -> int:
    """列表分片通常不含 ti_items；尽量从常见字段取大小，取不到返回 0。

    下载时会查询详情接口（``fetch_book_source``）获得准确的源文件大小。
    """
    candidates: list[Any] = [
        item.get("total_size"), item.get("file_size"), item.get("size"),
    ]
    custom = item.get("custom_properties")
    if isinstance(custom, dict):
        candidates += [
            custom.get("total_size"), custom.get("file_size"), custom.get("size"),
        ]
    for value in candidates:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0


def fetch_catalog() -> dict[str, Any]:
    """从平台抓取全量教材目录（同步，可能耗时数秒）。"""
    session = requests.Session()
    session.trust_env = False
    session.headers.update(BASE_HEADERS)

    version = session.get(DATA_VERSION_URL, timeout=REQUEST_TIMEOUT).json()
    urls = version.get("urls") or []
    if isinstance(urls, str):
        urls = urls.split(",")
    urls = [u.strip() for u in urls if str(u).strip()]
    if not urls:
        raise SmartEduError("平台教材版本清单为空")

    raw: list[dict[str, Any]] = []
    for url in urls:
        chunk = session.get(url, timeout=REQUEST_TIMEOUT).json()
        if isinstance(chunk, list):
            raw.extend(chunk)

    books: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for item in raw:
        content_id = str(item.get("id") or "")
        if not content_id or content_id in seen:
            duplicates += 1
            continue
        tags: dict[str, str] = {}
        for tag in item.get("tag_list") or []:
            dimension = tag.get("tag_dimension_id")
            if dimension and dimension not in tags:
                tags[dimension] = str(tag.get("tag_name") or "")
        stage, subject = tags.get("zxxxd", ""), tags.get("zxxxk", "")
        if not stage or not subject:   # 逐课课件/教学指南等非整本教材
            continue
        seen.add(content_id)
        # 列表分片通常不带 ti_items（0/1000），大小先记 0；下载时查详情接口取准确值
        books.append({
            "content_id": content_id,
            "title": str(item.get("title") or content_id),
            "stage": stage,
            "subject": subject,
            "edition": tags.get("zxxbb", ""),
            "grade": tags.get("zxxnj", ""),
            "volume": tags.get("zxxcc", ""),
            "total_size": _extract_total_size(item),
            "url": DETAIL_PAGE.format(content_id=content_id),
        })

    return {
        "schema_version": 1,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "platform": "https://basic.smartedu.cn/tchMaterial",
            "tags_api": TAGS_URL,
            "list_api": DATA_VERSION_URL,
            "duplicates_skipped": duplicates,
        },
        "books": books,
    }


# ── 缓存 ──────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_fetch_lock = threading.Lock()
_memory: OfficialCatalog | None = None
_memory_at = 0.0
_refresh_thread: threading.Thread | None = None


def _read_disk() -> tuple[dict[str, Any] | None, float]:
    try:
        stat = CACHE_PATH.stat()
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, 0.0
    return (data if isinstance(data, dict) else None), stat.st_mtime


def _write_disk(data: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(CACHE_PATH, data)
    except OSError as e:
        logger.warning(f"[smartedu] 写缓存失败: {e}")


def _set_memory(catalog: OfficialCatalog, at: float) -> None:
    global _memory, _memory_at
    with _lock:
        _memory, _memory_at = catalog, at


def _is_fresh(at: float) -> bool:
    return at > 0 and (time.time() - at) < CACHE_TTL_SECONDS


def _refresh_worker() -> None:
    try:
        data = fetch_catalog()
    except Exception as e:  # noqa: BLE001 —— 刷新失败沿用旧缓存
        logger.warning(f"[smartedu] 后台刷新失败，继续使用旧缓存: {e}")
        return
    _write_disk(data)
    _set_memory(OfficialCatalog(data), time.time())
    logger.info(f"[smartedu] 后台刷新完成，共 {len(data.get('books') or [])} 本教材")


def _spawn_refresh() -> None:
    global _refresh_thread
    if _refresh_thread and _refresh_thread.is_alive():
        return
    _refresh_thread = threading.Thread(target=_refresh_worker, daemon=True,
                                       name="smartedu-catalog-refresh")
    _refresh_thread.start()


def get_catalog(*, force_refresh: bool = False) -> OfficialCatalog:
    """返回官方教材目录；内存 → 磁盘 → 平台抓取，过期时后台刷新。"""
    with _lock:
        cached, cached_at = _memory, _memory_at
    if cached is not None and not force_refresh and _is_fresh(cached_at):
        return cached

    data, disk_at = _read_disk()
    if data is not None and not (data.get("books") or []):
        data = None  # 空目录缓存视为无效（抓取失败或格式变化）
    if data is not None and not force_refresh and _is_fresh(disk_at):
        catalog = OfficialCatalog(data)
        _set_memory(catalog, disk_at)
        return catalog

    if data is not None and not force_refresh:  # 磁盘缓存过期：先用旧数据，后台刷新
        catalog = OfficialCatalog(data)
        _set_memory(catalog, disk_at)
        _spawn_refresh()
        return catalog

    with _fetch_lock:
        with _lock:
            if not force_refresh and _memory is not None and _is_fresh(_memory_at):
                return _memory
        data = fetch_catalog()
        _write_disk(data)
        catalog = OfficialCatalog(data)
        _set_memory(catalog, time.time())
        return catalog


def warmup() -> None:
    """启动时预热：后台抓取一次，不阻塞启动流程。"""
    try:
        get_catalog()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[smartedu] 预热失败: {e}")


def fetch_book_source(content_id: str) -> dict[str, Any]:
    """查详情接口，返回源文件信息 ``{url, format, size}``。"""
    if not content_id or "/" in content_id or ".." in content_id:
        raise SmartEduError(f"非法 content_id: {content_id}")
    session = requests.Session()
    session.trust_env = False
    session.headers.update(BASE_HEADERS)
    response = session.get(DETAIL_URL.format(content_id=content_id),
                           timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    source = _pick_source_item(data.get("ti_items") or [])
    if not source:
        raise SmartEduError(f"教材没有可下载的源文件: {content_id}")
    url = cdn_url(str(source.get("ti_storage") or ""))
    if not url:
        url = next((u for u in source.get("ti_storages") or [] if u), "")
    if not url:
        raise SmartEduError(f"教材源文件地址缺失: {content_id}")
    return {
        "url": url,
        "format": str(source.get("ti_format") or "pdf").lower(),
        "size": int(source.get("ti_size") or 0),
    }


def reset_cache_for_tests() -> None:
    """测试辅助：清空内存缓存。"""
    global _memory, _memory_at
    with _lock:
        _memory, _memory_at = None, 0.0
