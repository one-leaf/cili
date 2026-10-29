"""官方教材目录、登录签名、缓存与下载任务测试。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

import httpx
import pytest

from core.textbooks import TextbookTaskManager
from core.textbooks import downloader as downloader_mod
from core.textbooks import smartedu as smartedu_mod
from core.textbooks.smartedu import (
    OfficialCatalog,
    SmartEduCredentials,
    SmartEduError,
    _extract_total_size,
    build_nd_auth,
    is_auth_expired,
    signature_string,
)
from core.textbooks.smartedu_login import _capture_from_output

PDF_HEAD = b"%PDF-1.4\n"
PDF_TAIL = b"\n%%EOF\n"


def _pdf_body(size: int, fill: bytes = b"a") -> bytes:
    assert size >= len(PDF_HEAD) + len(PDF_TAIL) + 1
    return PDF_HEAD + fill * (size - len(PDF_HEAD) - len(PDF_TAIL)) + PDF_TAIL


def _book(
    content_id: str = "c3e06fe4-c6b3-49cb-8727-4f8ff69bbfbc",
    title: str = "义务教育教科书·数学一年级上册",
    *,
    stage: str = "小学",
    subject: str = "数学",
    edition: str = "人教版",
    grade: str = "一年级",
    volume: str = "上册",
    size: int = 100,
) -> dict:
    return {
        "content_id": content_id,
        "title": title,
        "stage": stage,
        "subject": subject,
        "edition": edition,
        "grade": grade,
        "volume": volume,
        "total_size": size,
        "url": f"https://basic.smartedu.cn/tchMaterial/detail?contentId={content_id}",
    }


def _catalog(*books: dict) -> OfficialCatalog:
    return OfficialCatalog({"fetched_at": "2026-10-28 00:00:00", "books": list(books)})


@pytest.fixture(autouse=True)
def _clean_smartedu_memory_cache():
    """隔离 smartedu 模块级内存缓存，避免跨测试文件互相污染。"""
    smartedu_mod.reset_cache_for_tests()
    yield
    smartedu_mod.reset_cache_for_tests()


class TestOfficialCatalog:
    """目录查询：学段/年级/学科/版本、年级覆盖与文件命名。"""

    def test_stage_and_grade_options(self):
        catalog = _catalog(
            _book(stage="高中", grade="高中年级"),
            _book(content_id="a0000001-0000-0000-0000-000000000001", stage="小学", grade="一年级"),
            _book(content_id="a0000002-0000-0000-0000-000000000002", stage="初中", grade="八年级"),
            _book(content_id="a0000003-0000-0000-0000-000000000003", stage="小学（五•四学制）", grade="三年级"),
        )
        assert catalog.stage_options() == ["小学", "小学（五•四学制）", "初中", "高中"]
        assert catalog.grades("小学") == ["一年级"]
        assert catalog.grades("小学（五•四学制）") == ["三年级"]

    def test_grade_coverage_handles_aliases_and_merged_books(self):
        merged = _book(content_id="b0000001-0000-0000-0000-000000000001",
                       grade="一至二年级", stage="小学")
        high = _book(content_id="b0000002-0000-0000-0000-000000000002",
                     stage="高中", grade="高中年级")
        catalog = _catalog(merged, high)
        assert [b.content_id for b in catalog.find_books(stage="小学", grade="二年级")] \
            == [merged["content_id"]]
        for grade in ("高一", "高二", "高三"):
            assert [b.content_id for b in catalog.find_books(stage="高中", grade=grade)] \
                == [high["content_id"]]

    def test_editions_expand_covered_grades(self):
        catalog = _catalog(
            _book(content_id="c0000001-0000-0000-0000-000000000001", grade="一至二年级"),
            _book(content_id="c0000002-0000-0000-0000-000000000002", grade="三年级",
                  title="义务教育教科书·数学三年级上册"),
        )
        editions = catalog.editions(stage="小学", subject="数学")
        assert len(editions) == 1
        assert editions[0]["grades"] == ["一年级", "二年级", "三年级"]

    def test_flat_filename_dedupes_same_title_across_editions(self):
        catalog = _catalog(
            _book(content_id="d0000001-0000-0000-0000-000000000001", edition="人教版"),
            _book(content_id="d0000002-0000-0000-0000-000000000002", edition="北师大版"),
        )
        first = catalog.get_by_key("d0000001-0000-0000-0000-000000000001")
        second = catalog.get_by_key("d0000002-0000-0000-0000-000000000002")
        assert catalog.flat_filename(first) == "义务教育教科书·数学一年级上册.pdf"
        assert catalog.flat_filename(second) == "北师大版-义务教育教科书·数学一年级上册.pdf"

    def test_invalid_catalog_raises(self):
        with pytest.raises(SmartEduError):
            OfficialCatalog({})


class TestSmartEduAuth:
    """X-ND-AUTH MAC 签名与凭据失效判断。"""

    def test_signature_string_uses_decoded_path_and_query(self):
        nonce = "1700000000000:ABCDEFGH"
        text = signature_string(
            "https://r1-ndr-private.ykt.cbern.com.cn/edu/%E6%95%B0%E5%AD%A6.pdf?x=1",
            "GET", nonce,
        )
        assert text == (
            "1700000000000:ABCDEFGH\nGET\n"
            "/edu/数学.pdf?x=1\nr1-ndr-private.ykt.cbern.com.cn\n"
        )

    def test_build_nd_auth_matches_hmac(self, monkeypatch):
        creds = SmartEduCredentials(access_token="tok", mac_key="key", diff=0)
        url = "https://r1-ndr-private.ykt.cbern.com.cn/book.pdf"
        nonce = "1700000000000:ABCDEFGH"
        monkeypatch.setattr(smartedu_mod, "generate_nonce", lambda diff=0: nonce)
        mac = base64.b64encode(hmac.new(
            b"key", signature_string(url, "GET", nonce).encode("utf-8"), hashlib.sha256
        ).digest()).decode("ascii")
        assert build_nd_auth(url, "GET", creds) == (
            f'MAC id="tok",nonce="{nonce}",mac="{mac}"'
        )

    def test_placeholder_without_mac_key(self):
        header = build_nd_auth("https://example.com/x.pdf", "GET",
                               SmartEduCredentials(access_token="tok"))
        assert header == 'MAC id="tok",nonce="0",mac="0"'

    def test_is_auth_expired(self):
        assert is_auth_expired(401)
        assert is_auth_expired(403)
        assert is_auth_expired(400, "InvalidArgument")
        assert not is_auth_expired(400, "")
        assert not is_auth_expired(200)

    def test_extract_total_size(self):
        assert _extract_total_size({"total_size": 123}) == 123
        assert _extract_total_size({"custom_properties": {"size": 45}}) == 45
        assert _extract_total_size({}) == 0


class TestSmartEduCache:
    """内存 + 磁盘缓存、强制刷新与空缓存失效。"""

    def test_cache_reuse_and_force_refresh(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "smartedu_catalog.json"
        monkeypatch.setattr(smartedu_mod, "CACHE_PATH", cache_path)
        calls = {"n": 0}

        def fake_fetch():
            calls["n"] += 1
            return {"fetched_at": "2026-10-28 00:00:00", "books": [_book()]}

        monkeypatch.setattr(smartedu_mod, "fetch_catalog", fake_fetch)
        smartedu_mod.reset_cache_for_tests()

        first = smartedu_mod.get_catalog()
        second = smartedu_mod.get_catalog()
        assert len(first.books) == 1
        assert first is second
        assert calls["n"] == 1

        refreshed = smartedu_mod.get_catalog(force_refresh=True)
        assert len(refreshed.books) == 1
        assert calls["n"] == 2

    def test_empty_disk_cache_is_treated_as_invalid(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "smartedu_catalog.json"
        cache_path.write_text(json.dumps({
            "fetched_at": "2026-10-28 00:00:00", "books": [],
        }), encoding="utf-8")
        monkeypatch.setattr(smartedu_mod, "CACHE_PATH", cache_path)

        def fake_fetch():
            return {"fetched_at": "2026-10-28 00:00:00", "books": [_book()]}

        monkeypatch.setattr(smartedu_mod, "fetch_catalog", fake_fetch)
        smartedu_mod.reset_cache_for_tests()
        catalog = smartedu_mod.get_catalog()
        assert len(catalog.books) == 1


class TestLoginCapture:
    def test_parses_execute_script_output(self):
        output = (
            'JavaScript result:\n[{"access_token": "tok", "mac_key": "key", "diff": 0}]'
        )
        assert _capture_from_output(output) == {
            "access_token": "tok", "mac_key": "key", "diff": 0,
        }

    def test_ignores_non_json_output(self):
        assert _capture_from_output("Navigated to ...") is None


class TestOfficialDownloader:
    """官方源文件下载：签名头、断点续传、跳过已存在、登录失效。"""

    @staticmethod
    def _run(tmp_path, book: dict, handler, monkeypatch, *, catalog=None):
        size = int(book["total_size"])
        monkeypatch.setattr(
            downloader_mod, "fetch_book_source",
            lambda content_id: {
                "url": f"https://r1-ndr-private.ykt.cbern.com.cn/{content_id}.pdf",
                "format": "pdf",
                "size": size,
            },
        )
        cat = catalog or _catalog(book)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def run():
            workspace = tmp_path / "ws"
            workspace.mkdir(exist_ok=True)
            manager = TextbookTaskManager()
            task = manager.start(
                workspace_uuid="test-ws",
                workspace_dir=workspace,
                data_dir=tmp_path / "data",
                catalog=cat,
                books=list(cat.books),
                client_factory=factory,
            )
            await task.async_task
            return task, workspace

        return asyncio.run(run())

    def test_downloads_with_signed_header(self, tmp_path, monkeypatch):
        body = _pdf_body(80)
        book = _book(size=len(body))
        seen_headers: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.append(dict(request.headers))
            return httpx.Response(200, content=body)

        task, workspace = self._run(tmp_path, book, handler, monkeypatch)
        assert task.status == "done", task.public_dict()
        files = list((workspace / "教材").glob("*.pdf"))
        assert len(files) == 1
        assert files[0].read_bytes() == body
        assert (workspace / "教材" / ".manifest.json").is_file()
        assert any("x-nd-auth" in {k.lower() for k in h} for h in seen_headers)

    def test_skips_existing_target_without_request(self, tmp_path, monkeypatch):
        body = _pdf_body(80)
        book = _book(size=len(body))
        workspace = tmp_path / "ws"
        workspace.mkdir()
        catalog = _catalog(book)
        target = workspace / "教材" / catalog.flat_filename(catalog.books[0])
        target.parent.mkdir(parents=True)
        target.write_bytes(body)

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("已有完整文件时不应发起请求")

        task, _ = self._run(tmp_path, book, handler, monkeypatch, catalog=catalog)
        assert task.books[book["content_id"]].status == "skipped"

    def test_auth_expired_clears_cached_credentials(self, tmp_path, monkeypatch):
        body = _pdf_body(80)
        book = _book(size=len(body))
        cleared: list[bool] = []
        monkeypatch.setattr(downloader_mod, "clear_credentials",
                            lambda: cleared.append(True))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b"unauthorized")

        task, _ = self._run(tmp_path, book, handler, monkeypatch)
        progress = task.books[book["content_id"]]
        assert progress.status == "failed"
        assert "登录" in progress.error
        assert cleared

    def test_resumes_from_partial_file(self, tmp_path, monkeypatch):
        body = _pdf_body(96)
        book = _book(size=len(body))
        content_id = book["content_id"]
        part_dir = tmp_path / "data" / "tmp" / "textbooks" / content_id
        part_dir.mkdir(parents=True)
        split_at = 40
        (part_dir / "source.part").write_bytes(body[:split_at])
        ranges: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            ranges.append(request.headers.get("range", ""))
            return httpx.Response(206, content=body[split_at:])

        task, workspace = self._run(tmp_path, book, handler, monkeypatch)
        assert task.status == "done", task.public_dict()
        assert ranges and ranges[0].startswith("bytes=")
        files = list((workspace / "教材").glob("*.pdf"))
        assert files and files[0].read_bytes() == body


class TestMarkdownStore:
    """教材 Markdown 复制：可读文件名、缺失源跳过。"""

    def test_copies_markdown_with_readable_name(self, tmp_path, monkeypatch):
        from core.textbooks import markdown_store

        md_dir = tmp_path / "md"
        md_dir.mkdir()
        monkeypatch.setattr(markdown_store, "MD_DIR", md_dir)
        content_id = "00000001-0000-0000-0000-000000000001"
        (md_dir / f"{content_id}.md").write_text("# 数学", encoding="utf-8")
        catalog = _catalog(_book(content_id=content_id))
        workspace = tmp_path / "ws"
        workspace.mkdir()

        copied = markdown_store.copy_textbook_markdown(workspace, catalog, catalog.books)
        target = workspace / "教材" / "义务教育教科书·数学一年级上册.md"
        assert copied == [str(target)]
        assert target.read_text(encoding="utf-8") == "# 数学"

    def test_skips_when_source_missing(self, tmp_path, monkeypatch):
        from core.textbooks import markdown_store

        monkeypatch.setattr(markdown_store, "MD_DIR", tmp_path / "empty")
        catalog = _catalog(_book())
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert markdown_store.copy_textbook_markdown(workspace, catalog, catalog.books) == []
