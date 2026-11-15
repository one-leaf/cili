"""教材数据包下载源选择与重试策略。

背景：数据仓库的 release 可能没有资产（数据直接发布在 main 的
textbook/dist/）。此时 release 资产 URL 必然 404，不应排进下载源，
否则每个文件都要先浪费 4 个必然失败的尝试并打出 WARNING。
"""

from pathlib import Path

from core.textbooks import pack_sync as ps


def _cfg(**over):
    cfg = dict(ps.CFG_DEFAULTS)
    cfg.update(over)
    return cfg


class TestSourceList:
    def test_skips_release_when_no_assets(self, monkeypatch):
        monkeypatch.setattr(ps, "_latest_release", lambda cfg: ("v1", frozenset()))
        urls = ps._urls(_cfg(), "chapters.zip")
        assert not any("releases/latest/download" in u for u in urls)
        assert any("raw.githubusercontent.com" in u for u in urls)

    def test_includes_release_when_asset_present(self, monkeypatch):
        monkeypatch.setattr(
            ps, "_latest_release",
            lambda cfg: ("v1", frozenset({"chapters.zip"})),
        )
        urls = ps._urls(_cfg(), "chapters.zip")
        assert any("releases/latest/download/chapters.zip" in u for u in urls)

    def test_api_unavailable_keeps_legacy_attempt(self, monkeypatch):
        """API 拿不到 release 信息时退回旧行为（照常尝试），失败只记 DEBUG。"""
        monkeypatch.setattr(ps, "_latest_release", lambda cfg: None)
        urls = ps._urls(_cfg(), "chapters.zip")
        assert any("releases/latest/download/chapters.zip" in u for u in urls)

    def test_direct_mirror_returns_unproxied(self, monkeypatch):
        monkeypatch.setattr(ps, "_latest_release", lambda cfg: ("v1", frozenset()))
        urls = ps._urls(_cfg(mirror="direct"), "chapters.zip")
        assert urls == [
            "https://raw.githubusercontent.com/one-leaf/cili-data/main/textbook/dist/chapters.zip"
        ]


class TestReleaseInfo:
    def test_parses_assets_and_caches(self, monkeypatch):
        calls = []

        class _Resp:
            status_code = 200

            def json(self):
                return {"tag_name": "v9", "assets": [
                    {"name": "chapters.zip"}, {"name": "catalog.json"}, {},
                ]}

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Resp()

        monkeypatch.setattr(ps.httpx, "get", fake_get)
        monkeypatch.setattr(ps, "_release_cache", {})
        cfg = _cfg()

        first = ps._latest_release(cfg)
        second = ps._latest_release(cfg)

        assert first == ("v9", frozenset({"chapters.zip", "catalog.json"}))
        assert second == first
        assert len(calls) == 1  # TTL 内复用缓存，不重复打 API


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {}

    def iter_bytes(self, block):
        return iter(())


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *exc):
        return False


class _FakeClient:
    status_code = 404

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, headers=None):
        return _FakeStream(_FakeResponse(self.status_code))


class TestRetryPolicy:
    def test_404_fails_fast_without_sleeping(self, monkeypatch, tmp_path: Path):
        """源明确没有文件（404）：换源不等待，全部失败后只留一条 WARNING。"""
        sleeps = []
        monkeypatch.setattr(ps.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(ps.httpx, "Client", _FakeClient)
        monkeypatch.setattr(ps, "_urls", lambda cfg, name: ["https://a/1", "https://a/2"])

        ok = ps._download(_cfg(), "chapters.zip", tmp_path / "chapters.zip.dl")

        assert ok is False
        assert sleeps == []

    def test_5xx_retries_with_backoff(self, monkeypatch, tmp_path: Path):
        """网络/5xx 类失败才短暂等待后再换源。"""
        sleeps = []

        class _Client500(_FakeClient):
            status_code = 500

        monkeypatch.setattr(ps.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(ps.httpx, "Client", _Client500)
        monkeypatch.setattr(ps, "_urls", lambda cfg, name: ["https://a/1", "https://a/2"])

        ok = ps._download(_cfg(), "chapters.zip", tmp_path / "chapters.zip.dl")

        assert ok is False
        assert len(sleeps) == 2
