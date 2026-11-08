"""回合级图片投影测试：当前回合嵌入、历史引用、工具图、非视觉降级。"""

from types import SimpleNamespace

from core.config import Config, ImageConfig
from core.session_runner_runtime.runner import Runner


def _image(turn_id: str, path: str) -> dict:
    return {
        "type": "image",
        "id": f"img_{turn_id}",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
        "_meta": {"turn_id": turn_id, "path": path, "media_type": "image/png"},
    }


def _make_handler(messages: list[dict], mode: str = "current_turn") -> Runner:
    cfg = Config(images=ImageConfig(history_mode=mode))
    fake = SimpleNamespace(
        config=cfg,
        messages=messages,
        current_turn_id="turn_current",
        cwd=".",
        session_dir=None,
        _demoted_image_paths=set(),
        model=SimpleNamespace(input=["text", "image"]),
    )
    fake._get_messages_with_header = lambda: messages
    fake._invalidate_message_cache = lambda: None
    return Runner(fake)


def test_current_turn_image_stays_embedded_history_becomes_ref():
    messages = [{
        "role": "user",
        "content": [_image("turn_current", "uploads/current.png"),
                    _image("turn_old", "uploads/old.png")],
    }]
    projected = _make_handler(messages).build_projected_messages()
    assert projected[0]["content"][0]["type"] == "image"
    assert projected[0]["content"][1]["type"] == "text"
    assert "uploads/old.png" in projected[0]["content"][1]["text"]


def test_full_mode_keeps_images_embedded():
    messages = [{"role": "user", "content": [_image("turn_old", "uploads/old.png")]}]
    projected = _make_handler(messages, mode="full").build_projected_messages()
    assert projected[0]["content"][0]["type"] == "image"


def test_tool_result_images_follow_parent_turn():
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
    projected = _make_handler(messages).build_projected_messages()
    subs = projected[0]["content"][0]["content"]
    assert [b["type"] for b in subs] == ["text", "text"]


def test_demote_current_turn_images_marks_paths():
    messages = [{"role": "user", "content": [_image("turn_current", "uploads/current.png")]}]
    handler = _make_handler(messages)
    demoted = handler._demote_current_turn_images(max_body_size=1, keep_recent=0)
    assert demoted >= 1
    assert "uploads/current.png" in handler.runner._demoted_image_paths
    projected = handler.build_projected_messages()
    assert projected[0]["content"][0]["type"] == "text"


def test_non_vision_strip_handles_top_level_and_tool_result():
    messages = [{"role": "user", "content": [
        _image("turn_current", "uploads/current.png"),
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "BBBB"}}],
        },
    ]}]
    handler = _make_handler(messages)
    stripped = handler._strip_images_from_messages(messages)
    assert stripped[0]["content"][0]["type"] == "text"
    assert stripped[0]["content"][1]["content"][0]["type"] == "text"
