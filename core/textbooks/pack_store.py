"""教材数据包访问层：索引、章节 zip、FTS 检索库。

数据布局（``data/cili/textbooks/``，由 cili-data 同步线程写入）::

    chapters.zip     精炼章节（<cid>/index.md、<cid>/chapters/NNN.md）
    catalog.json     书目索引（学段/学科/版本/年级/册次/章数/字数）
    catalog.db       SQLite 检索库（books + chapters + FTS trigram）
    pack_meta.json   数据集版本与各包 SHA256
    originals.zip    可选：清洗版原文（仅本地，分发包不含）

内置索引兜底：``core/templates/textbooks/dataset_index.json`` 随 cili 发布，
首次启动、尚未同步 cili-data 时仍可提供书目与分类（下载不受影响）。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT
from core.textbooks.smartedu import (
    DETAIL_PAGE,
    GRADE_ALIASES,
    GRADE_COVERAGE,
    OfficialCatalog,
)

logger = logging.getLogger(__name__)

PACK_DIR = PROJECT_ROOT / "data" / "cili" / "textbooks"
BUNDLED_INDEX = PROJECT_ROOT / "core" / "templates" / "textbooks" / "dataset_index.json"

FRONTMATTER_RE = re.compile(r"^---\r?\n.*?\r?\n---\r?\n", re.S)
INDEX_LINE_RE = re.compile(r"^- \[chapters/(\d+)\.md\]\(.*?\) (.*)（(\d+) 字）")

_index_cache: dict[str, Any] | None = None
_index_key: tuple[str, float] = ("", 0.0)


class PackUnavailable(RuntimeError):
    """数据包尚不可用（未同步或正在同步）。"""


# ── 索引 ─────────────────────────────────────────────────────────────────────

def index_path() -> Path:
    """运行时索引优先：同步来的 catalog.json > 内置 dataset_index.json。"""
    packed = PACK_DIR / "catalog.json"
    if packed.is_file():
        return packed
    return BUNDLED_INDEX


def load_index() -> dict[str, Any]:
    """读取书目索引（带 mtime 缓存）。返回 {generated_at, books: [...]}。"""
    global _index_cache, _index_key
    path = index_path()
    try:
        mtime = path.stat().st_mtime
    except OSError as e:
        raise PackUnavailable(f"教材索引不存在: {path}") from e
    key = (str(path), mtime)
    if _index_cache is not None and key == _index_key:
        return _index_cache
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("books"), list):
        raise PackUnavailable(f"教材索引格式非法: {path}")
    _index_cache, _index_key = data, key
    return data


def dataset_catalog() -> OfficialCatalog:
    """把数据包索引包装成 OfficialCatalog（供下载页/向导复用其查询接口）。

    只依赖本地索引，不访问官方平台缓存。
    """
    data = load_index()
    books = [{
        "content_id": b.get("cid", ""),
        "title": b.get("title", ""),
        "stage": b.get("stage", ""),
        "subject": b.get("subject", ""),
        "edition": b.get("edition", ""),
        "grade": b.get("grade", ""),
        "volume": b.get("volume", ""),
        "total_size": 0,
        "url": DETAIL_PAGE.format(content_id=b.get("cid", "")),
    } for b in data["books"] if b.get("cid")]
    return OfficialCatalog({
        "fetched_at": data.get("generated_at", ""),
        "books": books,
    })


def grade_covers(book_grade: str, selected: str) -> bool:
    """教材年级标签是否覆盖用户选择的年级（别名/合并册/高中通用）。"""
    if not selected:
        return True
    if book_grade == selected:
        return True
    if GRADE_ALIASES.get(book_grade) == selected:
        return True
    if selected in GRADE_COVERAGE.get(book_grade, ()):
        return True
    # 高中教材统一标「高中年级」，视为覆盖高一/高二/高三
    return book_grade in ("", "高中年级") and selected in ("高一", "高二", "高三")


def find_books(stage: str = "", subject: str = "", grade: str = "",
               edition: str = "", volume: str = "", keyword: str = "",
               limit: int = 10) -> list[dict[str, Any]]:
    books = load_index()["books"]
    kw = keyword.strip()
    result = []
    for b in books:
        if stage and b.get("stage") != stage:
            continue
        if subject and b.get("subject") != subject:
            continue
        if grade and not grade_covers(b.get("grade", ""), grade):
            continue
        if edition and b.get("edition") != edition:
            continue
        if volume and b.get("volume") != volume:
            continue
        if kw and kw not in b.get("title", ""):
            continue
        result.append(b)
        if len(result) >= limit:
            break
    return result


def stage_matrix() -> list[tuple[str, int, int]]:
    """(学段, 书数, 万字) 汇总，供无条件查询时的概览。"""
    from collections import defaultdict
    acc: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for b in load_index()["books"]:
        acc[b.get("stage", "")][0] += 1
        acc[b.get("stage", "")][1] += int(b.get("chars") or 0)
    return sorted((s, n, c // 10000) for s, (n, c) in acc.items())


def format_book_line(b: dict[str, Any]) -> str:
    meta = "·".join(x for x in (b.get("stage"), b.get("subject"),
                                b.get("edition"), b.get("grade"),
                                b.get("volume")) if x)
    return (f"{b.get('cid', '')[:8]} | {b.get('title', '')} | {meta} | "
            f"{b.get('chapters', 0)} 章 | {int(b.get('chars') or 0) / 1e4:.1f} 万字")


# ── 数据包访问 ────────────────────────────────────────────────────────────────

class PackStore:
    """章节/原文读取与全文检索（只读；同步线程是唯一写入方）。"""

    def __init__(self, pack_dir: Path | None = None):
        self.pack_dir = Path(pack_dir) if pack_dir else PACK_DIR

    @property
    def available(self) -> bool:
        return (self.pack_dir / "chapters.zip").is_file()

    def _missing_message(self) -> str:
        try:
            from core.textbooks import pack_sync
            state = pack_sync.state()
        except Exception:  # noqa: BLE001
            state = {"status": "idle", "message": ""}
        status = state.get("status")
        if status in ("checking", "downloading", "installing", "building"):
            return (f"教材数据正在后台同步（{state.get('message') or status}），"
                    f"完成后可重试。")
        return ("教材数据尚未就绪。请检查网络后重启 cili 触发后台同步，"
                "或手动运行 cili-data 仓库的 build_textbook_pack.py --install。")

    def resolve_book(self, book_ref: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """按 cid（全量或前缀）或书名关键词定位；多义时返回候选。"""
        ref = (book_ref or "").strip()
        if not ref:
            return None, []
        books = load_index()["books"]
        exact = [b for b in books if b.get("cid") == ref]
        if exact:
            return exact[0], []
        prefix = [b for b in books if b.get("cid", "").startswith(ref)]
        if len(prefix) == 1:
            return prefix[0], []
        if prefix:
            return None, prefix[:10]
        titled = [b for b in books if ref in b.get("title", "")]
        if len(titled) == 1:
            return titled[0], []
        return None, titled[:10]

    def outline(self, cid: str) -> str:
        if not self.available:
            raise PackUnavailable(self._missing_message())
        with zipfile.ZipFile(self.pack_dir / "chapters.zip") as z:
            member = f"{cid}/index.md"
            try:
                return z.read(member).decode("utf-8")
            except KeyError as e:
                raise PackUnavailable(f"数据包中找不到目录: {member}") from e

    def _chapter_index_map(self, z: zipfile.ZipFile, cid: str) -> dict[int, tuple[str, int]]:
        mapping: dict[int, tuple[str, int]] = {}
        try:
            text = z.read(f"{cid}/index.md").decode("utf-8")
        except KeyError:
            return mapping
        for line in text.splitlines():
            m = INDEX_LINE_RE.match(line)
            if m:
                mapping[int(m.group(1))] = (m.group(2), int(m.group(3)))
        return mapping

    def read_chapter(self, cid: str, chapter: str, max_chars: int = 20000) -> tuple[str, str]:
        """读取单章。chapter 可为序号或标题关键词。返回 (标题, 正文)。"""
        if not self.available:
            raise PackUnavailable(self._missing_message())
        with zipfile.ZipFile(self.pack_dir / "chapters.zip") as z:
            mapping = self._chapter_index_map(z, cid)
            if not mapping:
                raise PackUnavailable(f"数据包中找不到该书目录: {cid}")
            ref = (chapter or "").strip()
            idx: int | None = None
            if ref.isdigit():
                idx = int(ref)
            else:
                for k, (title, _) in mapping.items():
                    if ref in title:
                        idx = k
                        break
            if idx is None or idx not in mapping:
                listing = "\n".join(f"  {k}. {t}" for k, (t, _) in sorted(mapping.items()))
                raise ValueError(f"找不到章节「{chapter}」，现有章节：\n{listing}")
            title = mapping[idx][0]
            try:
                raw = z.read(f"{cid}/chapters/{idx:03d}.md").decode("utf-8")
            except KeyError as e:
                raise PackUnavailable(f"数据包中缺章节文件: {idx:03d}") from e
        body = FRONTMATTER_RE.sub("", raw, count=1).strip()
        if max_chars and len(body) > max_chars:
            body = (body[:max_chars]
                    + f"\n\n……（本章共 {len(body)} 字，已截断；可调大 max_chars 继续读取）")
        return title, body

    def chapter_headings(self, cid: str, chapter: str) -> tuple[str, list[str]]:
        """列出某一章内部的标题（课文/篇目），返回 (章节标题, 标题列表)。"""
        title, body = self.read_chapter(cid, chapter, max_chars=0)
        headings: list[str] = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                text = stripped.lstrip("#").strip()
                if text:
                    headings.append(text)
        return title, headings

    def original(self, cid: str, offset: int = 0, limit: int = 20000) -> str:
        """读取清洗版原文（仅本地安装的 originals.zip 提供）。"""
        path = self.pack_dir / "originals.zip"
        if not path.is_file():
            raise PackUnavailable(
                "本地无原文数据（分发包只含提炼章节）。"
                "如需原文，请在 cili-data 工作区运行 "
                "build_textbook_pack.py --with-originals --install。")
        with zipfile.ZipFile(path) as z:
            try:
                text = z.read(f"{cid}.md").decode("utf-8")
            except KeyError as e:
                raise PackUnavailable(f"原文包中找不到: {cid}") from e
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 50000))
        seg = text[offset:offset + limit]
        return f"[原文 {offset}-{offset + len(seg)} / 共 {len(text)} 字]\n\n{seg}"

    def _connect(self) -> sqlite3.Connection:
        db = self.pack_dir / "catalog.db"
        if not db.is_file():
            raise PackUnavailable(
                "检索索引（catalog.db）尚未构建。" + self._missing_message())
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        return con

    def search(self, query: str, stage: str = "", subject: str = "",
               grade: str = "", edition: str = "", book: str = "",
               limit: int = 20) -> list[dict[str, Any]]:
        """全文检索；≥3 字走 FTS trigram，2 字走 LIKE 回退。"""
        query = (query or "").strip()
        if not query:
            raise ValueError("query 不能为空")
        limit = max(1, min(int(limit), 50))
        where, params = [], []
        if stage:
            where.append("b.stage = ?"); params.append(stage)
        if subject:
            where.append("b.subject = ?"); params.append(subject)
        if grade:
            where.append("b.grade = ?"); params.append(grade)
        if edition:
            where.append("b.edition = ?"); params.append(edition)
        if book:
            where.append("(b.title LIKE ? OR b.cid LIKE ?)")
            params += [f"%{book}%", f"{book}%"]
        cond = (" AND " + " AND ".join(where)) if where else ""
        con = self._connect()
        try:
            if len(query) >= 3:
                sql = (
                    "SELECT b.cid, b.title, b.stage, b.subject, b.edition, "
                    "b.grade, b.volume, c.idx, c.title AS ch_title, "
                    "snippet(chapters_fts, 1, '【', '】', '…', 40) AS snip, "
                    "bm25(chapters_fts) AS score "
                    "FROM chapters_fts f JOIN chapters c ON c.rowid = f.rowid "
                    "JOIN books b ON b.cid = c.cid "
                    "WHERE chapters_fts MATCH ?" + cond +
                    " ORDER BY score LIMIT ?")
                rows = con.execute(sql, [query, *params, limit]).fetchall()
            else:
                sql = (
                    "SELECT b.cid, b.title, b.stage, b.subject, b.edition, "
                    "b.grade, b.volume, c.idx, c.title AS ch_title, "
                    "substr(c.content, max(1, instr(c.content, ?) - 40), 120) AS snip, "
                    "0 AS score "
                    "FROM chapters c JOIN books b ON b.cid = c.cid "
                    "WHERE instr(c.content, ?) > 0" + cond +
                    " LIMIT ?")
                rows = con.execute(sql, [query, query, *params, limit]).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]


def build_catalog_db(install_dir: Path | None = None) -> Path:
    """由 chapters.zip + catalog.json 构建 catalog.db（同步线程调用，约 85s）。"""
    install_dir = Path(install_dir) if install_dir else PACK_DIR
    db_path = install_dir / "catalog.db"
    db_path.unlink(missing_ok=True)
    catalog = json.loads((install_dir / "catalog.json").read_text(encoding="utf-8"))
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("""CREATE TABLE books(
          cid TEXT PRIMARY KEY, title TEXT NOT NULL,
          stage TEXT, subject TEXT, edition TEXT, grade TEXT, volume TEXT,
          chapters INTEGER NOT NULL, chars INTEGER NOT NULL)""")
        con.execute("""CREATE TABLE chapters(
          cid TEXT NOT NULL, idx INTEGER NOT NULL, title TEXT NOT NULL,
          chars INTEGER NOT NULL, content TEXT NOT NULL, PRIMARY KEY (cid, idx))""")
        with zipfile.ZipFile(install_dir / "chapters.zip") as z:
            names = set(z.namelist())
            for b in catalog["books"]:
                cid = b["cid"]
                con.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?)",
                            (cid, b["title"], b.get("stage", ""), b.get("subject", ""),
                             b.get("edition", ""), b.get("grade", ""),
                             b.get("volume", ""), b.get("chapters", 0),
                             b.get("chars", 0)))
                meta: dict[int, tuple[str, int]] = {}
                im = f"{cid}/index.md"
                if im in names:
                    for line in z.read(im).decode("utf-8").splitlines():
                        m = INDEX_LINE_RE.match(line)
                        if m:
                            meta[int(m.group(1))] = (m.group(2), int(m.group(3)))
                for k in range(1, int(b.get("chapters", 0)) + 1):
                    member = f"{cid}/chapters/{k:03d}.md"
                    if member not in names:
                        continue
                    text = FRONTMATTER_RE.sub("", z.read(member).decode("utf-8"), 1)
                    title, chars = meta.get(k, ("", len(text)))
                    con.execute("INSERT INTO chapters VALUES (?,?,?,?,?)",
                                (cid, k, title, chars, text))
        con.commit()
        con.execute("""CREATE VIRTUAL TABLE chapters_fts USING fts5(
          title, content, content='chapters', content_rowid='rowid',
          tokenize='trigram')""")
        con.execute("INSERT INTO chapters_fts(chapters_fts) VALUES('rebuild')")
        con.commit()
    finally:
        con.close()
    return db_path
