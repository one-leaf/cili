"""图片管线测试：EXIF 方向、请求投影、持久化 sanitize、引用文本。"""

import io

import pytest

pytest.importorskip("PIL")

from PIL import Image

from core.image_pipeline import (
    image_ref_text,
    normalize_image_bytes,
    project_images_for_request,
    sanitize_images_for_persistence,
)


def _oriented_jpeg(tmp_path, orientation: int) -> bytes:
    path = tmp_path / f"oriented-{orientation}.jpg"
    img = Image.new("RGB", (2, 1))
    img.putpixel((0, 0), (255, 0, 0))
    img.putpixel((1, 0), (0, 0, 255))
    exif = Image.Exif()
    exif[0x0112] = orientation
    img.save(path, format="JPEG", exif=exif, quality=100)
    return path.read_bytes()


@pytest.mark.parametrize("orientation,expected", [
    (1, (2, 1)),
    (3, (2, 1)),
    (6, (1, 2)),
    (8, (1, 2)),
])
def test_normalize_applies_exif_orientation(tmp_path, orientation, expected):
    raw = _oriented_jpeg(tmp_path, orientation)
    normalized = normalize_image_bytes(raw, "image/jpeg")
    assert normalized is not None
    payload, mime, width, height = normalized
    assert (width, height) == expected
    out = Image.open(io.BytesIO(payload))
    assert out.size == expected
    assert out.getexif().get(0x0112) in (None, 1)


def _image_block(turn_id: str, path: str) -> dict:
    return {
        "type": "image",
        "id": f"img_{turn_id}",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
        "_meta": {"turn_id": turn_id, "path": path, "media_type": "image/png", "width": 10, "height": 20},
    }


def test_project_images_keeps_current_and_references_history():
    current = "turn_current"
    messages = [{
        "role": "user",
        "content": [
            _image_block(current, "uploads/current.png"),
            _image_block("turn_old", "uploads/old.png"),
        ],
    }]
    projected = project_images_for_request(messages, current)
    types = [b["type"] for b in projected[0]["content"]]
    assert types == ["image", "text"]
    assert "uploads/old.png" in projected[0]["content"][1]["text"]


def test_project_nested_tool_result_images():
    current = "turn_current"
    tool_result = {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": [
            {"type": "text", "text": "shot"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "BBBB"}},
        ],
        "_meta": {
            "turn_id": "turn_old",
            "image_refs": [{"path": "sessions/s/attachments/t1-0.png", "media_type": "image/png"}],
        },
    }
    messages = [{"role": "user", "content": [tool_result]}]
    projected = project_images_for_request(messages, current)
    subs = projected[0]["content"][0]["content"]
    assert [b["type"] for b in subs] == ["text", "text"]
    assert "sessions/s/attachments/t1-0.png" in subs[1]["text"]


def test_sanitize_images_for_persistence_replaces_top_and_nested():
    top = _image_block("turn_current", "uploads/a.png")
    tool_result = {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": [
            {"type": "text", "text": "shot"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "BBBB"}},
        ],
        "_meta": {
            "turn_id": "turn_current",
            "image_refs": [{"path": "sessions/s/attachments/t1-0.png", "media_type": "image/png"}],
        },
    }
    sanitized = sanitize_images_for_persistence([top, tool_result])
    assert sanitized[0]["type"] == "text"
    assert sanitized[0]["id"] == top["id"]
    assert [b["type"] for b in sanitized[1]["content"]] == ["text", "text"]
    assert "sessions/s/attachments/t1-0.png" in sanitized[1]["content"][1]["text"]


def test_image_ref_text_without_path_is_explicit():
    text = image_ref_text({"type": "image", "_meta": {}})
    assert "unavailable" in text
