"""context 估算的外置工具结果解析（多模态 JSON / 文本截断）。"""

import json

from core.tool_output import resolve_tool_results_for_context


def _msg(output_path, content=None, truncated=False):
    return {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": content,
            "_meta": {
                "output_path": output_path,
                "truncated": truncated,
                "file_size": 0,
            },
        }],
    }


def test_multimodal_json_parsed_to_blocks(tmp_path):
    (tmp_path / "t1.json").write_text(json.dumps({
        "type": "multimodal",
        "blocks": [
            {"type": "text", "text": "hello"},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": "AAAA",
            }},
        ],
    }), encoding="utf-8")
    messages = [_msg("t1.json")]

    resolve_tool_results_for_context(messages, tmp_path)

    blocks = messages[0]["content"][0]["content"]
    assert isinstance(blocks, list)
    assert [b["type"] for b in blocks] == ["text", "image"]


def test_text_result_truncated_like_runner(tmp_path):
    (tmp_path / "t1.txt").write_text("x" * 50_000, encoding="utf-8")
    messages = [_msg("t1.txt", truncated=True)]

    resolve_tool_results_for_context(messages, tmp_path)

    content = messages[0]["content"][0]["content"]
    assert isinstance(content, str)
    assert len(content) < 40_000


def test_rejects_path_outside_session(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    messages = [_msg("../outside.txt")]

    resolve_tool_results_for_context(messages, tmp_path)

    # 路径穿越时不注入内容，保持原样
    assert messages[0]["content"][0]["content"] is None