"""教材数据包访问层测试（索引 / 章节读取 / 检索 / 降级）。"""
import json
import zipfile

import pytest

from core.textbooks import pack_store


@pytest.fixture()
def mini_pack(tmp_path, monkeypatch):
    pack = tmp_path / "textbooks"
    pack.mkdir()
    books = [
        {"cid": "aaaa1111-0000-0000", "title": "数学 七年级 上册", "stage": "初中",
         "subject": "数学", "edition": "人教版", "grade": "七年级", "volume": "上册",
         "chapters": 2, "chars": 30},
        {"cid": "bbbb2222-0000-0000", "title": "语文 七年级 上册", "stage": "初中",
         "subject": "语文", "edition": "统编版", "grade": "七年级", "volume": "上册",
         "chapters": 1, "chars": 10},
    ]
    (pack / "catalog.json").write_text(
        json.dumps({"generated_at": "2026-01-01", "books": books}), encoding="utf-8")
    with zipfile.ZipFile(pack / "chapters.zip", "w") as z:
        z.writestr("aaaa1111-0000-0000/index.md",
                   "# 数学\n- [chapters/001.md](chapters/001.md) 有理数（10 字）\n"
                   "- [chapters/002.md](chapters/002.md) 整式（20 字）\n")
        z.writestr("aaaa1111-0000-0000/chapters/001.md",
                   "---\r\n书名: 数学\r\n---\r\n# 有理数\r\n"
                   "## 正数与负数\r\n有理数的定义与勾股定理无关的示例内容。\r\n")
        z.writestr("aaaa1111-0000-0000/chapters/002.md",
                   "---\n书名: 数学\n---\n整式的加减内容。\n")
        z.writestr("bbbb2222-0000-0000/index.md",
                   "# 语文\n- [chapters/001.md](chapters/001.md) 春（10 字）\n")
        z.writestr("bbbb2222-0000-0000/chapters/001.md",
                   "---\n书名: 语文\n---\n春的内容。\n")
    pack_store.build_catalog_db(pack)
    monkeypatch.setattr(pack_store, "PACK_DIR", pack)
    return pack


def test_find_books(mini_pack):
    books = pack_store.find_books(subject="数学")
    assert len(books) == 1
    assert books[0]["cid"].startswith("aaaa")
    assert pack_store.find_books(keyword="语文")[0]["subject"] == "语文"
    assert pack_store.find_books(subject="物理") == []


def test_resolve_book(mini_pack):
    store = pack_store.PackStore()
    book, candidates = store.resolve_book("aaaa1111")
    assert book["title"] == "数学 七年级 上册" and candidates == []
    book, candidates = store.resolve_book("xxxx")
    assert book is None and candidates == []
    # 书名多义：两本都含「七年级」
    book, candidates = store.resolve_book("七年级")
    assert book is None and len(candidates) == 2


def test_outline_and_read(mini_pack):
    store = pack_store.PackStore()
    outline = store.outline("aaaa1111-0000-0000")
    assert "有理数" in outline
    title, body = store.read_chapter("aaaa1111-0000-0000", "2")
    assert title == "整式" and "整式的加减" in body
    title, body = store.read_chapter("aaaa1111-0000-0000", "有理数")
    assert "勾股定理" in body
    with pytest.raises(ValueError):
        store.read_chapter("aaaa1111-0000-0000", "不存在")


def test_search_fts_and_like(mini_pack):
    store = pack_store.PackStore()
    hits = store.search("勾股定理")          # ≥3 字 → FTS
    assert len(hits) == 1 and hits[0]["cid"].startswith("aaaa")
    hits = store.search("整式")              # 2 字 → LIKE 回退
    assert len(hits) == 1 and hits[0]["idx"] == 2
    hits = store.search("勾股定理", subject="语文")
    assert hits == []


def test_original_missing(mini_pack):
    store = pack_store.PackStore()
    with pytest.raises(pack_store.PackUnavailable):
        store.original("aaaa1111-0000-0000")


def test_unavailable_message(tmp_path, monkeypatch):
    monkeypatch.setattr(pack_store, "PACK_DIR", tmp_path / "nope")
    store = pack_store.PackStore(tmp_path / "nope")
    assert not store.available
    with pytest.raises(pack_store.PackUnavailable) as exc:
        store.outline("x")
    assert "教材数据" in str(exc.value)


def test_bundled_index_catalog():
    """内置索引可独立提供目录（不依赖数据包与官方 cache）。"""
    catalog = pack_store.dataset_catalog()
    assert len(catalog.books) > 1000
    assert "初中" in catalog.stage_options()


def test_grade_covers_alias(mini_pack):
    # 高中年级覆盖高一/高二/高三；合并册覆盖各年级
    assert pack_store.grade_covers("高中年级", "高二")
    assert not pack_store.grade_covers("高中年级", "七年级")
    assert pack_store.grade_covers("一至二年级", "二年级")
    assert pack_store.grade_covers("七年级", "七年级")


def test_chapter_headings(mini_pack):
    store = pack_store.PackStore()
    title, headings = store.chapter_headings("aaaa1111-0000-0000", "1")
    assert title == "有理数"
    assert headings == ["有理数", "正数与负数"]


def test_read_strips_crlf_frontmatter(mini_pack):
    store = pack_store.PackStore()
    _, body = store.read_chapter("aaaa1111-0000-0000", "1")
    assert body.startswith("# 有理数")
    assert "书名" not in body.splitlines()[0]
