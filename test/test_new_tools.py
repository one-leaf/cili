"""新增工具测试：read_image / clock / session_search。"""

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
        """clock sleep 至少等待指定秒数。"""
        t0 = time.monotonic()
        r = _tool(tools, "clock").execute(action="sleep", seconds=0.2)
        assert not r.error
        assert "Slept" in r.output
        assert time.monotonic() - t0 >= 0.2

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


class TestReadImage:
    def test_missing_file(self, tools):
        r = _tool(tools, "read_image").execute(file_path="nope.png")
        assert r.error
        assert "file not found" in r.output

    def test_unsupported_extension(self, tools, test_workspace):
        p = os.path.join(test_workspace, "a.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("hi")
        r = _tool(tools, "read_image").execute(file_path="a.txt")
        assert r.error
        assert "not a supported image type" in r.output

    def test_png_returns_multimodal(self, tools, test_workspace):
        """read_image 返回 base64 图片内容块。"""
        pytest.importorskip("PIL")
        from PIL import Image

        p = os.path.join(test_workspace, "img.png")
        Image.new("RGB", (16, 16), (255, 0, 0)).save(p)
        r = _tool(tools, "read_image").execute(file_path="img.png")
        assert not r.error
        from core.llm.types import ImageBlock

        img = [b for b in r.blocks if isinstance(b, ImageBlock)]
        assert len(img) == 1
        assert img[0].mime_type == "image/png"
        assert img[0].data

    def test_shared_with_read(self, tools, test_workspace):
        """read_image 与 read 对同一图片返回一致的 multimodal 结果。"""
        pytest.importorskip("PIL")
        from PIL import Image

        p = os.path.join(test_workspace, "shared.png")
        Image.new("RGB", (8, 8), (0, 0, 255)).save(p)
        ri = _tool(tools, "read_image").execute(file_path="shared.png")
        rd = _tool(tools, "read").execute(file_path="shared.png")
        assert not ri.error and not rd.error
        from core.llm.types import ImageBlock

        ri_img = [b for b in ri.blocks if isinstance(b, ImageBlock)]
        rd_img = [b for b in rd.blocks if isinstance(b, ImageBlock)]
        assert len(ri_img) == 1 and len(rd_img) == 1
        assert ri_img[0].data == rd_img[0].data


class TestSessionSearch:
    """跨会话历史消息搜索。

    使用 tools fixture 的 workspace_uuid="test-workspace"，
    会话文件写入 data/agents/test-workspace/sessions/（fixture 自动清理）。
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
