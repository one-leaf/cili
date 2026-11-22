"""图片存储/回合边界回归：读图方向、jsonl 引用化、工具图 turn 过滤、sidecar。"""

import base64
import io
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("PIL")

from PIL import Image

from core.config import Config, ImageConfig
from core.image_pipeline import normalize_image_block, write_tool_image_sidecar
from core.image_read import read_image_file
from core.llm.types import ImageBlock, TextBlock
from core.session import SessionStore
from core.session_runner_runtime.executor import TurnExecutor


def _oriented_jpeg(path, orientation: int) -> None:
    img = Image.new("RGB", (2, 1))
    img.putpixel((0, 0), (255, 0, 0))
    img.putpixel((1, 0), (0, 0, 255))
    exif = Image.Exif()
    exif[0x0112] = orientation
    img.save(path, format="JPEG", exif=exif, quality=100)


def _png_bytes() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_read_image_applies_exif_orientation(tmp_path):
    path = tmp_path / "oriented.jpg"
    _oriented_jpeg(path, 6)
    result = read_image_file(str(path))
    images = [b for b in result.blocks if isinstance(b, ImageBlock)]
    assert len(images) == 1
    out = Image.open(io.BytesIO(base64.b64decode(images[0].data)))
    assert out.size == (1, 2)


def test_session_jsonl_sanitizes_image_blocks(tmp_path):
    store = SessionStore("sess", tmp_path / "sessions")
    store.image_history_mode = "current_turn"
    block = {
        "type": "image",
        "id": "img_1",
        "source": {"type": "base64", "media_type": "image/png", "data": _png_bytes()},
        "_meta": {"turn_id": "turn_1", "path": "uploads/a.png", "media_type": "image/png"},
    }
    store.add_message("user", [block], meta={"turn_id": "turn_1"})
    store.save(force=True)

    jsonl = (store.session_dir / "messages.jsonl").read_text(encoding="utf-8")
    assert '"type": "image"' not in jsonl
    assert "image ref: uploads/a.png" in jsonl
    index = json.loads((store.session_dir / "index.json").read_text(encoding="utf-8"))
    assert "uploads/a.png" in json.dumps(index, ensure_ascii=False)


def test_resolve_tool_results_filters_historical_images(tmp_path):
    session_dir = tmp_path / "sessions" / "sess"
    session_dir.mkdir(parents=True)
    payload = {
        "type": "multimodal",
        "blocks": [
            {"type": "text", "text": "shot"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        ],
    }
    (session_dir / "t1.json").write_text(json.dumps(payload), encoding="utf-8")

    messages = [
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "t1", "content": None,
            "_meta": {"output_path": "t1.json", "turn_id": "turn_current"},
        }]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "t2", "content": None,
            "_meta": {
                "output_path": "t1.json", "turn_id": "turn_old",
                "image_refs": [{"path": "sessions/sess/attachments/t2-0.png", "media_type": "image/png"}],
            },
        }]},
    ]
    fake = SimpleNamespace(
        config=Config(images=ImageConfig(history_mode="current_turn")),
        messages=messages,
        current_turn_id="turn_current",
        session_dir=session_dir,
        cwd=str(tmp_path),
        _demoted_image_paths=set(),
        model=SimpleNamespace(input=["text", "image"]),
    )
    fake._get_messages_with_header = lambda: messages
    fake._invalidate_message_cache = lambda: None

    resolved = TurnExecutor(fake)._resolved_messages()
    current_subs = resolved[0]["content"][0]["content"]
    old_subs = resolved[1]["content"][0]["content"]
    assert [b["type"] for b in current_subs] == ["text", "image"]
    assert [b["type"] for b in old_subs] == ["text", "text"]
    assert "sessions/sess/attachments/t2-0.png" in old_subs[1]["text"]


def test_tool_image_limit_replaces_excess_without_crashing(tmp_path):
    """工具返回多图且 output 非空时，超限图片转引用不能崩。

    回归：_execute_tool_impl 里一度局部 import TextBlock（遮蔽模块级导入），
    output 非空时该分支不执行，后面的限量分支引用 TextBlock 抛
    UnboundLocalError；紧随其后的 notes 又赋值只读 property result.output。
    """
    from core.tools.base import Tool, ToolResult

    def _png(color) -> str:
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), color).save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    class _DummyImageTool(Tool):
        name = "dummy_img"
        description = "returns two images"
        parameters = {"type": "object", "properties": {}}

        def execute(self, **kwargs):
            return ToolResult(blocks=[
                TextBlock(text="two shots"),
                ImageBlock(data=_png((255, 0, 0)), mime_type="image/png"),
                ImageBlock(data=_png((0, 0, 255)), mime_type="image/png"),
            ])

    session_dir = tmp_path / "sessions" / "sess"
    session_dir.mkdir(parents=True)
    sink = SimpleNamespace(on_tool_call=lambda *a: None, on_tool_result=lambda *a: None)
    fake = SimpleNamespace(
        tools=[_DummyImageTool()],
        session_dir=session_dir,
        cwd=str(tmp_path),
        sink=sink,
        config=Config(images=ImageConfig(max_images_per_tool_result=1)),
        _retired_image_carriers=set(),
    )

    out = TurnExecutor(fake).execute_tool("dummy_img", {}, "t1")

    refs = (out.get("_meta") or {}).get("image_refs") or []
    assert len(refs) == 1  # 只内联 1 张，其余转引用
    payload = json.loads((session_dir / out["_meta"]["output_path"]).read_text(encoding="utf-8"))
    texts = [b.get("text", "") for b in payload["blocks"] if b.get("type") == "text"]
    assert any("image omitted" in t for t in texts)
    assert sum(1 for b in payload["blocks"] if b.get("type") == "image") == 1


def test_write_tool_image_sidecar(tmp_path):
    session_dir = tmp_path / "sessions" / "sess"
    block = ImageBlock(data=_png_bytes(), mime_type="image/png")
    ref = write_tool_image_sidecar(session_dir, tmp_path, "t1", 0, block)
    assert ref is not None
    assert ref["path"].startswith("sessions/sess/attachments/")
    assert (tmp_path / ref["path"]).exists()


def test_normalize_image_block_downscales_before_send():
    """工具图必须在进请求前归一化：原图 >4096px 会被服务端拒绝。"""
    buf = io.BytesIO()
    Image.new("RGB", (5000, 3500), (0, 200, 0)).save(buf, format="JPEG", quality=95)
    block = ImageBlock(
        data=base64.b64encode(buf.getvalue()).decode(), mime_type="image/jpeg"
    )
    info = normalize_image_block(block)
    assert info is not None
    payload, mime, width, height = info
    assert max(width, height) <= 4096
    # 就地改写后的 data 与 sidecar payload 一致
    assert base64.b64decode(block.data) == payload
    assert block.mime_type == mime
    out = Image.open(io.BytesIO(payload))
    assert max(out.size) <= 4096
