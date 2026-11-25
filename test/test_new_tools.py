"""新增工具测试：read 图片分支 / clock / session_search。"""

import json
import os
import time

import pytest

from core.config import get_workspace_data_dir
from core.tools import get_tool_by_name


def _tool(tools, name):
    return get_tool_by_name(tools, name)


class TestClock:
    def test_now(self, tools):
        """clock now 返回本地时间与 UTC 时间。"""
        r = _tool(tools, "clock").execute()
        assert not r.error
        assert "Local time:" in r.output
        assert "UTC time:" in r.output

    def test_sleep(self, tools):
        """clock sleep 至少等待指定秒数。

        容忍 Windows 定时器分辨率（~15.6ms tick）：time.sleep(0.2) 可能
        提前一个 tick 醒来，monotonic 实测 0.188s 属正常，故下界取 0.15。
        """
        t0 = time.monotonic()
        r = _tool(tools, "clock").execute(action="sleep", seconds=0.2)
        assert not r.error
        assert "Slept" in r.output
        assert time.monotonic() - t0 >= 0.15

    def test_sleep_capped(self, tools, monkeypatch):
        """超出上限的 sleep 被截断到 60s 而非报错。"""
        from core.tools import clock as clock_mod

        slept = []
        monkeypatch.setattr(clock_mod.time, "sleep", lambda s: slept.append(s))
        r = _tool(tools, "clock").execute(action="sleep", seconds=99999)
        assert not r.error
        assert slept == [60.0]

    def test_bad_action(self, tools):
        r = _tool(tools, "clock").execute(action="bogus")
        assert r.error

    def test_now_with_timezone(self, tools):
        """指定 IANA 时区时直接返回该时区时间，不依赖模型心算。"""
        pytest.importorskip("zoneinfo")
        r = _tool(tools, "clock").execute(action="now", timezone="Asia/Ho_Chi_Minh")
        assert not r.error
        assert "Asia/Ho_Chi_Minh time:" in r.output
        assert "(UTC+07" in r.output

    def test_now_invalid_timezone(self, tools):
        r = _tool(tools, "clock").execute(action="now", timezone="Mars/Olympus_Mons")
        assert r.error
        assert "unknown timezone" in r.output


class TestReadImage:
    def test_missing_file(self, tools):
        r = _tool(tools, "read").execute(file_path="nope.png")
        assert r.error
        assert "file not found" in r.output

    def test_png_returns_multimodal(self, tools, test_workspace):
        """read 的图片分支返回 base64 图片内容块。"""
        pytest.importorskip("PIL")
        from PIL import Image

        p = os.path.join(test_workspace, "img.png")
        Image.new("RGB", (16, 16), (255, 0, 0)).save(p)
        r = _tool(tools, "read").execute(file_path="img.png")
        assert not r.error
        from core.llm.types import ImageBlock

        img = [b for b in r.blocks if isinstance(b, ImageBlock)]
        assert len(img) == 1
        assert img[0].mime_type == "image/png"
        assert img[0].data

    def test_svg_reads_as_text_source(self, tools, test_workspace):
        """SVG 按文本读取源码，不做光栅化（接口层不收 image/svg+xml）。"""
        p = os.path.join(test_workspace, "chart.svg")
        source = ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
                  '<rect width="40" height="20" fill="red"/></svg>')
        with open(p, "w", encoding="utf-8") as f:
            f.write(source)
        r = _tool(tools, "read").execute(file_path="chart.svg")
        assert not r.error, r.output
        assert "<svg" in r.output and "fill=\"red\"" in r.output
        from core.llm.types import ImageBlock

        assert not [b for b in r.blocks if isinstance(b, ImageBlock)]

    def test_read_image_tool_removed(self):
        """图片查看已并入 read，不再单独注册 read_image。"""
        from core.tools.registry import TOOL_REGISTRY

        assert "read_image" not in TOOL_REGISTRY


class TestReadPdf:
    @pytest.fixture
    def pdf_factory(self, test_workspace):
        pymupdf = pytest.importorskip("pymupdf")

        def make(name: str, kinds: list[str]) -> str:
            path = os.path.join(test_workspace, name)
            doc = pymupdf.open()
            for kind in kinds:
                page = doc.new_page()
                if kind == "text":
                    page.insert_text((72, 72), "Hello PDF text layer", fontsize=14)
                else:
                    page.draw_rect(
                        pymupdf.Rect(50, 50, 250, 250),
                        color=(0, 0, 1), fill=(1, 0, 0),
                    )
            doc.save(path)
            doc.close()
            return path

        return make

    def test_text_pdf_returns_text(self, tools, pdf_factory):
        pytest.importorskip("pdfplumber")
        pdf_factory("text.pdf", ["text"])
        r = _tool(tools, "read").execute(file_path="text.pdf")
        assert not r.error, r.output
        assert "Hello PDF text layer" in r.output
        from core.llm.types import ImageBlock

        assert not [b for b in r.blocks if isinstance(b, ImageBlock)]

    def test_scanned_pdf_renders_pages(self, tools, pdf_factory):
        pytest.importorskip("pdfplumber")
        pdf_factory("scanned.pdf", ["blank", "blank"])
        r = _tool(tools, "read").execute(file_path="scanned.pdf", pages="1-2")
        assert not r.error, r.output
        from core.llm.types import ImageBlock

        imgs = [b for b in r.blocks if isinstance(b, ImageBlock)]
        assert len(imgs) == 2
        assert "no extractable text layer" in r.output

    def test_scanned_pdf_caps_rendered_pages(self, tools, pdf_factory):
        pytest.importorskip("pdfplumber")
        pdf_factory("many.pdf", ["blank"] * 6)
        r = _tool(tools, "read").execute(file_path="many.pdf", pages="1-6")
        assert not r.error, r.output
        from core.llm.types import ImageBlock

        imgs = [b for b in r.blocks if isinstance(b, ImageBlock)]
        assert len(imgs) == 4
        assert "first 4 of 6" in r.output


class TestSessionSearch:
    """跨会话历史消息搜索。

    使用 tools fixture 的 workspace_uuid="test-workspace"，
    会话文件写入 test_workspace/.cili/sessions/（fixture 自动清理）。
    """

    def _write_session(self, sid, name, messages, updated):
        sdir = get_workspace_data_dir("test-workspace") / "sessions" / sid
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "meta.json").write_text(json.dumps({
            "session_id": sid,
            "name": name,
            "metadata": {"updated_at": updated},
        }), encoding="utf-8")
        (sdir / "messages.jsonl").write_text(
            "\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n",
            encoding="utf-8",
        )

    def test_find_across_sessions(self, tools):
        self._write_session("s1", "会话一", [
            {"seq": 0, "id": "a", "role": "user", "content": "今天讨论量子纠缠"},
        ], "2026-09-01 10:00:00")
        self._write_session("s2", "会话二", [
            {"seq": 0, "id": "b", "role": "user", "content": "帮我写个排序算法"},
        ], "2026-09-02 10:00:00")
        r = _tool(tools, "session_search").execute(query="量子")
        assert not r.error
        assert "会话一" in r.output
        assert "会话二" not in r.output

    def test_no_match(self, tools):
        self._write_session("s3", "会话三", [
            {"seq": 0, "id": "c", "role": "user", "content": "hello"},
        ], "2026-09-03 10:00:00")
        r = _tool(tools, "session_search").execute(query="nope")
        assert not r.error
        assert "No historical messages matched" in r.output

    def test_block_content_extraction(self, tools):
        """content 为 block 列表时也能提取文本与工具名。"""
        self._write_session("s4", "会话四", [
            {"seq": 0, "id": "d", "role": "user",
             "content": [{"type": "text", "text": "看看成绩单"}]},
            {"seq": 1, "id": "e", "role": "assistant",
             "content": [{"type": "tool_use", "name": "browser", "input": {}}]},
        ], "2026-09-04 10:00:00")
        r1 = _tool(tools, "session_search").execute(query="成绩")
        assert not r1.error
        assert "成绩单" in r1.output
        r2 = _tool(tools, "session_search").execute(query="browser")
        assert not r2.error
        assert "[tool:browser]" in r2.output

    def test_requires_query(self, tools):
        r = _tool(tools, "session_search").execute(query="   ")
        assert r.error
