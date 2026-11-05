"""多模态入参组装测试：纯图片消息不能被丢成空回合。"""

from types import SimpleNamespace

from core.media_fetch import InboundMedia
from core.multimodal import build_user_input


def _runner(supports_image=True):
    return SimpleNamespace(model=SimpleNamespace(
        input=["text", "image"] if supports_image else ["text"]
    ))


def _image(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    return InboundMedia(kind="image", path=str(path), rel_path="a.png",
                        filename="a.png", content_type="image/png")


def _voice():
    return InboundMedia(kind="voice", path="", rel_path="v.ogg",
                        filename="v.ogg", transcript="你好")


def test_pure_image_message_keeps_image_block(tmp_path):
    blocks = build_user_input("", [_image(tmp_path)], _runner())
    assert isinstance(blocks, list)
    assert blocks and blocks[0]["type"] == "image"


def test_image_plus_voice_keeps_both(tmp_path):
    blocks = build_user_input("", [_image(tmp_path), _voice()], _runner())
    assert isinstance(blocks, list)
    assert any(b["type"] == "image" for b in blocks)
    assert blocks[0]["type"] == "text"
    assert "你好" in blocks[0]["text"]


def test_text_and_image_updates_text_block(tmp_path):
    blocks = build_user_input("看图", [_image(tmp_path)], _runner())
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "text"
    assert "看图" in blocks[0]["text"]
    assert any(b["type"] == "image" for b in blocks)


def test_no_image_falls_back_to_text():
    result = build_user_input("", [_voice()], _runner())
    assert isinstance(result, str)
    assert "[语音转写] 你好" in result


def test_model_without_image_capability_falls_back_to_path(tmp_path):
    result = build_user_input("", [_image(tmp_path)], _runner(supports_image=False))
    assert isinstance(result, str)
    assert "[附件] a.png" in result
