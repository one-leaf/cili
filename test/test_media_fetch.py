"""core/media_fetch 测试：下载 URL 校验、分类规则、落盘转换。"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest

import core.media_fetch as mf
from core.media_fetch import check_download_url, classify, download_to_uploads


@pytest.fixture
def _public_dns(monkeypatch):
    monkeypatch.setattr(mf.socket, "getaddrinfo", lambda host, port, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
    ])


class TestCheckDownloadUrl:
    @pytest.mark.parametrize("url", ["", "http://example.com/a.png"])
    def test_rejects_empty_and_http(self, url):
        assert check_download_url(url)

    def test_metadata_endpoint_rejected(self):
        assert "元数据" in (check_download_url("https://metadata.google.internal/x") or "")

    @pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "192.168.1.5", "169.254.1.1"])
    def test_private_literal_ip_rejected(self, host):
        assert check_download_url(f"https://{host}/a")

    def test_public_host_allowed(self, _public_dns):
        assert check_download_url("https://cdn.example.com/a.png") is None

    def test_host_resolving_private_rejected(self, monkeypatch):
        monkeypatch.setattr(mf.socket, "getaddrinfo", lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 0)),
        ])
        assert check_download_url("https://internal.example.com/a")


class TestClassify:
    @pytest.mark.parametrize("content_type,filename,expected", [
        ("image/png", "", "image"),
        ("image/jpeg", "", "image"),
        ("audio/amr", "", "voice"),
        ("voice", "", "voice"),
        ("application/pdf", "a.pdf", "file"),
        ("application/octet-stream", "a.bin", "file"),
        ("", "a.zip", "file"),
    ])
    def test_classify(self, content_type, filename, expected):
        assert classify(content_type, filename) == expected


class TestDownload:
    def test_success_writes_uploads(self, tmp_path, _public_dns):
        payload = b"\x89PNG\r\n\x1a\n" + b"p" * 32
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload))
        )
        path = asyncio.run(download_to_uploads(
            "https://cdn.example.com/a.png",
            "a.png",
            client=client,
            uploads_dir=tmp_path / "uploads",
            max_bytes=1024,
        ))
        assert path is not None
        assert Path(path).read_bytes() == payload
        assert Path(path).parent == tmp_path / "uploads"

    def test_rejected_url_returns_none(self, tmp_path):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x"))
        )
        path = asyncio.run(download_to_uploads(
            "http://example.com/a.png",
            "a.png",
            client=client,
            uploads_dir=tmp_path / "uploads",
            max_bytes=1024,
        ))
        assert path is None

    def test_transform_applied_before_write(self, tmp_path, _public_dns):
        cipher = b"ciphertext"

        def transform(chunk: bytes, is_last: bool) -> bytes:
            return b"plain" + chunk

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=cipher))
        )
        path = asyncio.run(download_to_uploads(
            "https://cdn.example.com/a.bin",
            "a.bin",
            client=client,
            uploads_dir=tmp_path / "uploads",
            max_bytes=1024,
            transform=transform,
        ))
        assert Path(path).read_bytes() == b"plainciphertext"
