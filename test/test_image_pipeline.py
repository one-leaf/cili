"""图片管线测试：EXIF 方向、请求投影、持久化 sanitize、引用文本。"""

import io

import pytest

pytest.importorskip("PIL")

from PIL import Image

from core.image_pipeline import (
    apply_image_budget,
    ensure_user_image_meta,
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


def test_project_nested_tool_result_images_keeps_current_turn():
    """当前回合的 tool_result 图片必须保留 base64：回合归属挂在父块上，
    image_refs 条目里没有 turn_id，不继承会被误判成历史图投影成引用。"""
    current = "turn_current"
    tool_result = {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": [
            {"type": "text", "text": "shot"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "BBBB"}},
        ],
        "_meta": {
            "turn_id": current,
            "image_refs": [{"path": "sessions/s/attachments/t1-0.png", "media_type": "image/png"}],
        },
    }
    messages = [{"role": "user", "content": [tool_result]}]
    projected = project_images_for_request(messages, current)
    subs = projected[0]["content"][0]["content"]
    assert [b["type"] for b in subs] == ["text", "image"]
    assert subs[1]["source"]["data"] == "BBBB"


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


def _tool_result_block(turn_id: str, tool_use_id: str, *, width: int = 100,
                       height: int = 100, data: str = "AAAA") -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": [{
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": data},
        }],
        "_meta": {
            "turn_id": turn_id,
            "image_refs": [{
                "path": f"att/{tool_use_id}.png",
                "media_type": "image/png",
                "width": width,
                "height": height,
            }],
        },
    }


def test_apply_image_budget_retires_oldest_carriers_keeps_reserved():
    current = "turn_current"
    messages = [
        {"role": "user", "content": [
            _image_block(current, "uploads/u1.png"),
            _image_block(current, "uploads/u2.png"),
        ]},
        {"role": "user", "content": [
            _tool_result_block(current, f"c{i}") for i in range(6)
        ]},
    ]
    result = apply_image_budget(
        messages,
        max_images=6, hard_limit=100, max_bytes=10**9,
        eviction_batch=4, eviction_floor=3,
    )
    assert result.removed_images == 2
    assert set(result.newly_retired_ids) == {"c0", "c1"}
    subs = messages[1]["content"]
    assert subs[0]["content"][0]["type"] == "text"
    assert subs[1]["content"][0]["type"] == "text"
    assert subs[2]["content"][0]["type"] == "image"
    # 用户上传图（reserved）永不退役
    assert all(block["type"] == "image" for block in messages[0]["content"])


def test_apply_image_budget_latched_carriers_not_counted_twice():
    current = "turn_current"
    messages = [{"role": "user", "content": [
        _tool_result_block(current, f"c{i}") for i in range(6)
    ]}]
    kwargs = dict(
        max_images=3, hard_limit=100, max_bytes=10**9,
        eviction_batch=4, eviction_floor=3,
    )
    first = apply_image_budget(messages, **kwargs)
    assert first.removed_images > 0
    latched = set(first.newly_retired_ids)

    second = apply_image_budget(messages, retired_ids=latched, **kwargs)
    assert second.removed_images == 0
    assert second.newly_retired_ids == []
    # 已 latched 的载体仍然是引用文本（静默替换）
    assert messages[0]["content"][0]["content"][0]["type"] == "text"


def test_apply_image_budget_conditional_limit_uses_hard_limit_when_all_small():
    current = "turn_current"
    messages = [{"role": "user", "content": [
        _tool_result_block(current, f"c{i}") for i in range(25)
    ]}]
    result = apply_image_budget(
        messages,
        max_images=100, hard_limit=100, max_bytes=10**9,
        eviction_batch=4, eviction_floor=3,
    )
    assert result.removed_images == 0

    # 任一张 >4096px → 严格上限 20，按 4 个载体步进退到 17 个
    oversized = [{"role": "user", "content": [
        _tool_result_block(current, f"d{i}", width=5000 if i == 0 else 100)
        for i in range(25)
    ]}]
    result2 = apply_image_budget(
        oversized,
        max_images=100, hard_limit=100, max_bytes=10**9,
        eviction_batch=4, eviction_floor=3,
    )
    assert result2.removed_images == 8


def test_apply_image_budget_byte_budget_retires_carriers():
    current = "turn_current"
    messages = [{"role": "user", "content": [
        _tool_result_block(current, f"c{i}", data="A" * 100) for i in range(4)
    ]}]
    result = apply_image_budget(
        messages,
        max_images=100, hard_limit=100, max_bytes=250,
        eviction_batch=4, eviction_floor=0,
    )
    assert result.removed_images == 2


def test_project_demotes_oversized_inline_image_even_in_current_turn():
    current = "turn_current"
    small = _image_block(current, "uploads/small.png")
    big = _image_block(current, "uploads/big.png")
    big["source"]["data"] = "A" * (5 * 1024 * 1024 + 1)
    messages = [{"role": "user", "content": [small, big]}]
    projected = project_images_for_request(messages, current)
    types = [b["type"] for b in projected[0]["content"]]
    assert types == ["image", "text"]
    assert "uploads/big.png" in projected[0]["content"][1]["text"]


def test_normalize_keeps_small_png_unchanged():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 128, 255)).save(buf, format="PNG")
    raw = buf.getvalue()
    normalized = normalize_image_bytes(raw, "image/png")
    assert normalized is not None
    payload, mime, width, height = normalized
    assert mime == "image/png"
    assert (width, height) == (16, 16)
    assert payload == raw  # 未超限不重编码


def test_normalize_downscales_large_image_to_4096px():
    raw_buf = io.BytesIO()
    Image.new("RGB", (5000, 3500), (200, 30, 30)).save(raw_buf, format="JPEG", quality=95)
    normalized = normalize_image_bytes(raw_buf.getvalue(), "image/jpeg")
    assert normalized is not None
    payload, mime, width, height = normalized
    assert max(width, height) <= 4096
    assert mime in ("image/jpeg", "image/png")
    assert Image.open(io.BytesIO(payload)).size == (width, height)


def test_ensure_user_image_meta_rewrites_source_with_normalized_data(tmp_path):
    import base64

    buf = io.BytesIO()
    Image.new("RGB", (5000, 3500), (10, 200, 10)).save(buf, format="JPEG", quality=95)
    block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.b64encode(buf.getvalue()).decode(),
        },
    }
    ensure_user_image_meta(block, cwd=tmp_path)
    # 发给模型的 source 必须已被归一化（而不是原图）
    out = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))
    assert max(out.size) <= 4096
    meta = block["_meta"]
    assert meta.get("normalized") is True
    assert meta.get("path")
    assert (tmp_path / "uploads").is_dir()
