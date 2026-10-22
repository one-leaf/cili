"""教材清单、分片合并与下载任务测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from core.textbooks import (
    CatalogBook,
    TextbookCatalog,
    TextbookTaskManager,
    get_catalog,
    merge_textbook_chunks,
)
from core.textbooks.merge import TextbookMergeError

PDF_HEAD = b"%PDF-1.4\n"
PDF_TAIL = b"\n%%EOF\n"


def _pdf_body(size: int, fill: bytes = b"a") -> bytes:
    assert size >= len(PDF_HEAD) + len(PDF_TAIL) + 1
    return PDF_HEAD + fill * (size - len(PDF_HEAD) - len(PDF_TAIL)) + PDF_TAIL


class TestCatalog:
    """真实清单的基础校验和匹配。"""

    def test_loads_real_catalog(self):
        catalog = get_catalog()
        assert catalog.books
        assert catalog.source_commit
        assert catalog.default_book_types == ("textbook",)

    def test_edition_publishers_cover_catalog(self):
        from core.textbooks.catalog import EDITION_PUBLISHERS

        catalog = get_catalog()
        editions = {
            book.edition
            for book in catalog.books
            if book.book_type == "textbook" and book.stage in ("小学", "初中", "高中") and book.edition
        }
        assert editions <= set(EDITION_PUBLISHERS), sorted(editions - set(EDITION_PUBLISHERS))

    def test_edition_sort_key_pinyin_order(self):
        from core.textbooks.catalog import edition_sort_key

        names = ["人教版", "冀教版", "北京版", "北师大版", "𝐀稀有字版"]
        ordered = sorted(names, key=edition_sort_key)
        assert ordered[:4] == ["北京版", "北师大版", "冀教版", "人教版"]
        assert "𝐀稀有字版" in ordered

    def test_finds_primary_math_pep(self):
        catalog = get_catalog()
        books = catalog.find_books(
            stage="小学", system="六三制", subject="数学",
            edition="人教版", book_types=("textbook",),
        )
        assert len(books) == 12
        grades = {book.grade for book in books}
        assert "一年级" in grades
        assert all(book.split is False or book.chunk_count > 0 for book in books)

    def test_high_school_default_contains_required_and_selective(self):
        catalog = get_catalog()
        books = catalog.find_books(
            stage="高中", subject="数学", edition="人教版（A版）（主编：章建跃&李增沪）-人民教育出版社",
        )
        assert {book.high_type for book in books} >= {"必修", "选择性必修"}
        required_only = catalog.find_books(
            stage="高中", subject="数学",
            edition="人教版（A版）（主编：章建跃&李增沪）-人民教育出版社",
            high_scope="required_only",
        )
        assert {book.high_type for book in required_only} == {"必修"}

    def test_information_science_has_no_match(self):
        catalog = get_catalog()
        assert catalog.find_books(stage="初中", subject="信息学") == []

    def test_editions_are_sorted_by_name(self):
        from core.textbooks.catalog import edition_sort_key

        catalog = get_catalog()
        editions = catalog.editions(stage="小学", system="六三制", subject="数学")
        assert editions
        keys = [edition_sort_key(item["edition_raw"]) for item in editions]
        assert keys == sorted(keys)

    def test_editions_filter_by_grade(self):
        from dataclasses import replace

        base = CatalogBook(
            id="x", book_key="0123456789ab", stage="小学", system="六三制",
            subject="数学", subject_raw="数学", edition="甲版", edition_raw="甲版",
            grade="一年级", semester="上册", book_type="textbook", high_type="",
            volume="", title="书", source_path="小学/数学/甲版/书.pdf",
            target_path="教材/原始资料/小学/数学/甲版/一年级/上册/书.pdf",
            split=False, chunk_count=1, total_size=100,
            files=({"index": 1, "path": "x.pdf", "url": "https://raw.githubusercontent.com/one-leaf/ChinaTextbook/" + "0" * 40 + "/x.pdf", "size": 100},),
        )
        specs = [("甲版", "一年级"), ("乙版", "3至4年级"), ("丙版", "")]
        books = [
            replace(
                base, id=f"id{i}", book_key=f"key{i:012d}",
                edition=edition, edition_raw=edition, grade=grade,
                target_path=f"教材/原始资料/小学/数学/{edition}/{grade}/书.pdf",
            )
            for i, (edition, grade) in enumerate(specs)
        ]
        catalog = TextbookCatalog({
            "source": {"commit": "0" * 40, "raw_base": "https://raw.githubusercontent.com/one-leaf/ChinaTextbook/" + "0" * 40 + "/"},
            "download_policy": {}, "merge": {},
            "books": [book.__dict__ for book in books],
        })

        def names(grade: str) -> set[str]:
            return {item["edition"] for item in catalog.editions(stage="小学", subject="数学", grade=grade)}

        assert names("") == {"甲版", "乙版", "丙版"}
        assert names("一年级") == {"甲版", "丙版"}
        assert names("三年级") == {"乙版", "丙版"}
        assert names("九年级") == {"丙版"}

    def test_invalid_catalog_raises(self):
        with pytest.raises(Exception):
            TextbookCatalog({})


class TestMerge:
    """分片合并与 PDF 校验。"""

    def test_merges_chunks_in_given_order(self, tmp_path):
        chunks = []
        expected = bytearray()
        for index, fill in enumerate((b"1", b"2", b"3"), 1):
            path = tmp_path / f"{index:04d}.done"
            body = _pdf_body(64 if index < 3 else 80, fill)
            path.write_bytes(body)
            expected.extend(body)
            chunks.append(path)
        target = tmp_path / "out" / "book.pdf"
        result = merge_textbook_chunks(chunks, target, expected_size=len(expected))
        assert result.size == len(expected)
        assert target.read_bytes() == bytes(expected)
        assert target.read_bytes().startswith(PDF_HEAD)
        assert b"%%EOF" in target.read_bytes()[-16:]

    def test_rejects_size_mismatch(self, tmp_path):
        chunk = tmp_path / "0001.done"
        chunk.write_bytes(_pdf_body(64))
        with pytest.raises(TextbookMergeError, match="大小不符"):
            merge_textbook_chunks([chunk], tmp_path / "book.pdf", expected_size=65)

    def test_rejects_bad_pdf(self, tmp_path):
        chunk = tmp_path / "0001.done"
        chunk.write_bytes(b"not a pdf")
        with pytest.raises(TextbookMergeError, match="PDF"):
            merge_textbook_chunks([chunk], tmp_path / "book.pdf")

    def test_does_not_overwrite_by_default(self, tmp_path):
        chunk = tmp_path / "0001.done"
        chunk.write_bytes(_pdf_body(64))
        target = tmp_path / "book.pdf"
        target.write_bytes(_pdf_body(64, b"old"))
        with pytest.raises(TextbookMergeError, match="已存在"):
            merge_textbook_chunks([chunk], target)


def _book(tmp_path: Path, chunk_count: int = 2) -> CatalogBook:
    bodies = [_pdf_body(48, bytes([65 + i])) for i in range(chunk_count)]
    files = [
        {
            "index": index,
            "path": f"test/book.pdf.{index}",
            "url": f"https://raw.githubusercontent.com/one-leaf/ChinaTextbook/{'0' * 40}/test/book.pdf.{index}",
            "size": len(body),
        }
        for index, body in enumerate(bodies, 1)
    ]
    return CatalogBook(
        id="test/book.pdf", book_key="0123456789ab", stage="小学", system="六三制",
        subject="数学", subject_raw="数学", edition="人教版", edition_raw="人教版",
        grade="一年级", semester="上册", book_type="textbook", high_type="",
        volume="", title="测试教材", source_path="test/book.pdf",
        target_path="教材/原始资料/小学/数学/人教版/一年级/上册/测试教材.pdf",
        split=chunk_count > 1, chunk_count=chunk_count, total_size=sum(map(len, bodies)),
        files=tuple(files),
    )


def _fake_catalog(book: CatalogBook) -> TextbookCatalog:
    data = {
        "source": {
            "commit": "0" * 40,
            "raw_base": "https://raw.githubusercontent.com/one-leaf/ChinaTextbook/" + "0" * 40 + "/",
        },
        "download_policy": {"default_book_types": ["textbook"]},
        "merge": {},
        "books": [book.__dict__],
    }
    return TextbookCatalog(data)


class TestDownloader:
    """分片下载、合并、进度与 manifest。"""

    def test_downloads_and_merges_split_pdf(self, tmp_path):
        book = _book(tmp_path, 2)
        catalog = _fake_catalog(book)
        bodies = [_pdf_body(48, bytes([65 + i])) for i in range(2)]
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            index = int(str(request.url).rsplit(".", 1)[-1])
            return httpx.Response(200, content=bodies[index - 1])

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def run():
            workspace = tmp_path / "ws"
            workspace.mkdir()
            manager = TextbookTaskManager()
            task = manager.start(
                workspace_uuid="test-ws", workspace_dir=workspace,
                data_dir=tmp_path / "data", catalog=catalog, books=[book],
                client_factory=factory,
            )
            await task.async_task
            return task, manager, workspace

        task, manager, workspace = asyncio.run(run())
        assert task.status == "done", task.public_dict()
        progress = task.books[book.book_key]
        assert progress.status == "done"
        assert progress.downloaded_bytes == book.total_size
        target = workspace / "教材" / "测试教材.pdf"
        assert target.is_file()
        assert target.stat().st_size == book.total_size
        assert len(requests) == 2
        manifest_path = workspace / "教材" / ".manifest.json"
        assert manifest_path.is_file()

    def test_falls_back_to_mirror_when_direct_fails(self, tmp_path):
        book = _book(tmp_path, 1)
        catalog = _fake_catalog(book)
        body = _pdf_body(48)
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if str(request.url).startswith("https://raw.githubusercontent.com/"):
                return httpx.Response(500)
            return httpx.Response(200, content=body)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def run():
            workspace = tmp_path / "ws"
            workspace.mkdir()
            manager = TextbookTaskManager()
            task = manager.start(
                workspace_uuid="test-ws", workspace_dir=workspace,
                data_dir=tmp_path / "data", catalog=catalog, books=[book],
                client_factory=factory,
            )
            await task.async_task
            return task, workspace

        task, workspace = asyncio.run(run())
        assert task.status == "done", task.public_dict()
        assert requested[0].startswith("https://raw.githubusercontent.com/")
        assert requested[1] == "https://ghproxy.net/" + requested[0]
        assert (workspace / "教材" / "测试教材.pdf").is_file()

    def test_skips_existing_target_without_overwrite(self, tmp_path):
        book = _book(tmp_path, 1)
        catalog = _fake_catalog(book)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        target = workspace / "教材" / "测试教材.pdf"
        target.parent.mkdir(parents=True)
        target.write_bytes(_pdf_body(48))

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not download")

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def run():
            manager = TextbookTaskManager()
            task = manager.start(
                workspace_uuid="test-ws", workspace_dir=workspace,
                data_dir=tmp_path / "data", catalog=catalog, books=[book],
                client_factory=factory,
            )
            await task.async_task

        asyncio.run(run())
        assert target.read_bytes() == _pdf_body(48)

    def test_rejects_non_catalog_host(self, tmp_path):
        book = _book(tmp_path, 1)
        catalog = _fake_catalog(book)
        bad = catalog.books[0].__dict__["files"][0]
        bad = dict(bad)
        bad["url"] = "https://example.com/evil.pdf"
        evil = CatalogBook(**{**catalog.books[0].__dict__, "files": (bad,)})
        catalog = _fake_catalog(evil)
        workspace = tmp_path / "ws"
        workspace.mkdir()

        def factory() -> httpx.AsyncClient:
            raise AssertionError("should not create client")

        async def run():
            manager = TextbookTaskManager()
            task = manager.start(
                workspace_uuid="test-ws", workspace_dir=workspace,
                data_dir=tmp_path / "data", catalog=catalog, books=[evil],
                client_factory=factory,
            )
            await task.async_task

        asyncio.run(run())
        assert not list(workspace.rglob("*.pdf"))
