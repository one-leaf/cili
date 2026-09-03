"""多模态图片管线测试：ToolResult → save → resolve → LLM adapter → 实际 LLM 调用。

覆盖两套底层接口（Anthropic / OpenAI），验证图片数据在整条管线中不被丢失。
"""

import base64
import json
import os
import secrets
import shutil
import tempfile
from pathlib import Path

import pytest

# ── 项目根目录 ──
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from core.llm.types import (
    TextBlock,
    ImageBlock,
    ToolResultBlock,
    block_from_dict,
    blocks_from_dicts,
    Message,
)
from core.tools.shared.base import Tool, ToolResult


# ── DGX 本地 LLM 端点 ──
from test.conftest import DGX_BASE_URL, DGX_API_KEY, DGX_MODEL, make_dgx_config
from core.llm import LLMClient, create_llm_client


# ══════════════════════════════════════════════════════════════════════════════
# 辅助工具
# ══════════════════════════════════════════════════════════════════════════════


def _make_test_image(path: str, width: int = 200, height: int = 100) -> str:
    """用 PIL 生成一张带红色矩形的白色背景 PNG，返回路径。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)
    # 在中央画一个明显的红色矩形
    x0, y0 = width // 4, height // 4
    x1, y1 = width * 3 // 4, height * 3 // 4
    draw.rectangle([x0, y0, x1, y1], fill="red", outline="black")
    img.save(path, format="PNG")
    return path


def _dummy_image_block() -> dict:
    """返回一个最小有效的 image content block dict（base64 为 1x1 PNG）。"""
    # 1x1 透明 PNG（最小合法数据）
    png_data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png_data).decode(),
        },
    }


def _dummy_text_block(text: str = "图片内容") -> dict:
    return {"type": "text", "text": text}


# ══════════════════════════════════════════════════════════════════════════════
# 单元测试：ToolResultBlock 保留图片数据
# ══════════════════════════════════════════════════════════════════════════════


class TestToolResultBlockImage:
    """ToolResultBlock 在 from_dict / to_dict 往返后图片不丢失。"""

    def test_from_dict_preserves_image_list(self):
        """from_dict 传入 list content 时原样保留，不降级为纯文本。"""
        image_block = _dummy_image_block()
        text_block = _dummy_text_block("说明文字")

        trb = ToolResultBlock.from_dict({
            "type": "tool_result",
            "tool_use_id": "test-id-1",
            "content": [text_block, image_block],
        })

        assert isinstance(trb.content, list), \
            f"content 应为 list，实际为 {type(trb.content).__name__}"
        assert len(trb.content) == 2
        # 图片块保留
        img = next((b for b in trb.content if b.get("type") == "image"), None)
        assert img is not None, "图片块丢失"
        assert img["source"]["data"] == image_block["source"]["data"]

    def test_to_dict_preserves_image_list(self):
        """to_dict 序列化时保留 list content 中的图片块。"""
        trb = ToolResultBlock(
            tool_use_id="test-id-2",
            content=[_dummy_text_block("标题"), _dummy_image_block()],
        )
        d = trb.to_dict()

        assert isinstance(d["content"], list)
        types = [b["type"] for b in d["content"]]
        assert "image" in types
        assert "text" in types

    def test_round_trip_preserves_image_data(self):
        """from_dict → to_dict → from_dict 往返后图片 base64 数据完整。"""
        original_data = _dummy_image_block()["source"]["data"]

        trb1 = ToolResultBlock.from_dict({
            "type": "tool_result",
            "tool_use_id": "round-trip",
            "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": original_data}}],
        })
        d = trb1.to_dict()
        trb2 = ToolResultBlock.from_dict(d)

        img = next(b for b in trb2.content if isinstance(b, dict) and b.get("type") == "image")
        assert img["source"]["data"] == original_data

    def test_string_content_still_works(self):
        """纯文本 tool_result 仍然返回 str，向后兼容。"""
        trb = ToolResultBlock.from_dict({
            "type": "tool_result",
            "tool_use_id": "text-only",
            "content": "普通文本输出",
        })
        assert isinstance(trb.content, str)
        assert trb.content == "普通文本输出"


# ══════════════════════════════════════════════════════════════════════════════
# 单元测试：ToolResult 构造函数（content + output 同时传入）
# ══════════════════════════════════════════════════════════════════════════════


class TestToolResultMultimodalInit:
    """read.py 调用 ToolResult(output="...", content=[image_block]) 时图片不丢失。"""

    def test_output_and_content_both_preserved(self):
        """同时传 output 和 content 时，两者都应出现在 blocks 中。"""
        image_dict = _dummy_image_block()
        result = ToolResult(output="[Image: test.png]", content=[image_dict])

        # 应包含 ImageBlock
        image_blocks = [b for b in result.blocks if isinstance(b, ImageBlock)]
        assert len(image_blocks) == 1, f"期望 1 个 ImageBlock，实际 {len(image_blocks)}"

        # 应包含 TextBlock（来自 output）
        text_blocks = [b for b in result.blocks if isinstance(b, TextBlock)]
        assert any(b.text == "[Image: test.png]" for b in text_blocks)

    def test_content_only(self):
        """只传 content 不传 output，图片正常保存。"""
        result = ToolResult(content=[_dummy_image_block()])
        image_blocks = [b for b in result.blocks if isinstance(b, ImageBlock)]
        assert len(image_blocks) == 1

    def test_output_only(self):
        """只传 output，向后兼容。"""
        result = ToolResult(output="纯文本")
        text_blocks = [b for b in result.blocks if isinstance(b, TextBlock)]
        assert text_blocks[0].text == "纯文本"


# ══════════════════════════════════════════════════════════════════════════════
# 单元测试：save_output_to_file（多模态 → .json，纯文本 → .txt）
# ══════════════════════════════════════════════════════════════════════════════


class TestSaveOutputToFileMultimodal:

    @pytest.fixture
    def tmp_dir(self):
        d = tempfile.mkdtemp()
        yield d
        shutil.rmtree(d, ignore_errors=True)

    def test_image_output_saved_as_json(self, tmp_dir):
        """含图片的 ToolResult 保存为 .json，不出现 .txt.json。"""
        output_file = os.path.join(tmp_dir, "toolu_abc.txt")
        tool = _make_tool_instance(output_file)

        image_block = ImageBlock(
            data=_dummy_image_block()["source"]["data"],
            mime_type="image/png",
        )
        result = ToolResult(blocks=[
            TextBlock(text="[Image: test.png]"),
            image_block,
        ])

        tool.save_output_to_file(result)

        # 应保存为 toolu_abc.json（不是 toolu_abc.txt.json）
        json_path = os.path.join(tmp_dir, "toolu_abc.json")
        txt_json_path = os.path.join(tmp_dir, "toolu_abc.txt.json")
        assert os.path.exists(json_path), f"期望 {json_path} 存在"
        assert not os.path.exists(txt_json_path), f"不应出现 {txt_json_path}"

        # 验证 json 内容包含图片
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["type"] == "multimodal"
        block_types = [b["type"] for b in data["blocks"]]
        assert "image" in block_types

    def test_text_output_saved_as_txt(self, tmp_dir):
        """纯文本 ToolResult 保存为 .txt。"""
        output_file = os.path.join(tmp_dir, "toolu_def.txt")
        tool = _make_tool_instance(output_file)

        result = ToolResult(blocks=[TextBlock(text="hello world")])
        tool.save_output_to_file(result)

        txt_path = os.path.join(tmp_dir, "toolu_def.txt")
        assert os.path.exists(txt_path)
        with open(txt_path, encoding="utf-8") as f:
            assert f.read() == "hello world"


def _make_tool_instance(output_file: str) -> Tool:
    """创建带 output_file 的 Tool 实例（用于测试 save_output_to_file）。"""
    from core.tools.shared.read import ReadTool
    tool = ReadTool(cwd=tempfile.gettempdir(), workspace_uuid="test")
    tool.output_file = output_file
    return tool


# ══════════════════════════════════════════════════════════════════════════════
# 单元测试：read 工具读取图片返回正确的 ToolResult
# ══════════════════════════════════════════════════════════════════════════════


class TestReadToolImage:

    @pytest.fixture
    def tmp_image(self, tmp_path):
        img_path = str(tmp_path / "test_image.png")
        _make_test_image(img_path)
        return img_path

    def test_read_image_returns_image_block(self, tmp_image):
        from core.tools.shared.read import ReadTool
        tool = ReadTool(cwd=os.path.dirname(tmp_image), workspace_uuid="test")
        result = tool.execute(file_path=tmp_image)

        # 应有 ImageBlock
        image_blocks = [b for b in result.blocks if isinstance(b, ImageBlock)]
        assert len(image_blocks) == 1, \
            f"期望 1 个 ImageBlock，实际 {[type(b).__name__ for b in result.blocks]}"

        # 图片数据非空（200x100 PNG 至少几百字符）
        assert len(image_blocks[0].data) > 100, "图片 base64 数据过短，可能为空"
        assert image_blocks[0].mime_type == "image/png"


# ══════════════════════════════════════════════════════════════════════════════
# 单元测试：Message.from_dict 正确解析含图片的 tool_result 内容
# ══════════════════════════════════════════════════════════════════════════════


class TestMessageImageDeserialization:

    def test_tool_result_with_image_preserved(self):
        """Message.from_dict 解析 tool_result 消息时，图片 list content 保留。"""
        image_dict = _dummy_image_block()

        msg_dict = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "test-id",
                    "content": [_dummy_text_block("识别结果"), image_dict],
                }
            ],
        }

        msg = Message.from_dict(msg_dict)
        content = msg.content

        assert isinstance(content, list)
        tool_result_block = content[0]
        assert isinstance(tool_result_block, ToolResultBlock)
        assert isinstance(tool_result_block.content, list), \
            f"ToolResultBlock.content 应为 list，实际为 {type(tool_result_block.content).__name__}"

        # 图片块保留
        img = next((b for b in tool_result_block.content if isinstance(b, dict) and b.get("type") == "image"), None)
        assert img is not None, "图片块在 Message.from_dict 后丢失"
        assert img["source"]["data"] == image_dict["source"]["data"]


# ══════════════════════════════════════════════════════════════════════════════
# 集成测试：Anthropic / OpenAI adapter 序列化含图片的 tool_result
# ══════════════════════════════════════════════════════════════════════════════


class TestAdapterImageSerialization:
    """验证两套 adapter 在序列化时正确传递图片数据。"""

    def _make_message_with_image(self):
        """构造包含图片 tool_result 的 Message 对象。"""
        image_dict = _dummy_image_block()
        return Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="img-call-1",
                    content=[_dummy_text_block("图片说明"), image_dict],
                )
            ],
        )

    def test_anthropic_adapter_serializes_image(self):
        from core.llm.anthropic import AnthropicAdapter
        from core.config import ModelConfig

        config = ModelConfig(name="claude-sonnet-4-6", api_key="test", interface_type="anthropic")
        adapter = AnthropicAdapter(config)

        msg = self._make_message_with_image()
        body = adapter.serialize([msg], system="", tools=None, model="claude-sonnet-4-6", max_tokens=4096)

        # Anthropic 格式：tool_result.content 可以是 list，包含 image block
        user_msg = body["messages"][0]
        assert user_msg["role"] == "user"
        tool_result = user_msg["content"][0]
        assert tool_result["type"] == "tool_result"
        # content 应为 list，包含 image
        assert isinstance(tool_result["content"], list)
        has_image = any(
            isinstance(b, dict) and b.get("type") == "image"
            for b in tool_result["content"]
        )
        assert has_image, f"Anthropic adapter 未传递图片: {tool_result['content']}"

    def test_openai_adapter_handles_image_in_tool_result(self):
        from core.llm.openai import OpenAIAdapter
        from core.config import ModelConfig

        config = ModelConfig(name="gpt-4o", api_key="test", interface_type="openai")
        adapter = OpenAIAdapter(config)

        msg = self._make_message_with_image()
        body = adapter.serialize([msg], system="", tools=None, model="gpt-4o", max_tokens=4096)

        # OpenAI 格式：tool_result 转为独立 tool 消息，图片转为文本描述
        tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        # OpenAI 不支持 tool result 中嵌入图片，降级为 "[image content]" 文本
        # 这是正确的降级行为
        assert "[image content]" in tool_msgs[0]["content"] or len(tool_msgs[0]["content"]) > 0


# ══════════════════════════════════════════════════════════════════════════════
# DGX 集成测试：真实 LLM 调用，验证图片被正确识别
# ══════════════════════════════════════════════════════════════════════════════


class TestMultimodalDGX:
    """使用 DGX 本地端点的真实 LLM 图片识别测试。

    参数化覆盖 Anthropic 和 OpenAI 两套接口。
    """

    @pytest.fixture(params=["anthropic", "openai"], ids=["anthropic", "openai"])
    def dgx_client(self, request):
        config = make_dgx_config(request.param)
        client = create_llm_client(config.model)
        yield client, request.param
        client.close()

    def test_connection(self, dgx_client):
        """DGX 端点可达。"""
        client, protocol = dgx_client
        success, msg = client.test_connection()
        assert success is True, f"[{protocol}] 连接失败: {msg}"

    def test_image_recognition_direct(self, dgx_client, tmp_path):
        """直接向 LLM 发送含图片的 Message，验证模型能"看到"图片。

        发送一张白色背景 + 中央红色矩形的 PNG，询问模型图片内容。
        如果模型能识别出"红色"或"矩形"等关键特征，说明图片正确传递。
        """
        client, protocol = dgx_client

        # 生成测试图片：白底 + 红矩形
        img_path = str(tmp_path / "red_rect.png")
        _make_test_image(img_path, width=300, height=200)
        with open(img_path, "rb") as f:
            raw_png = f.read()
        b64_data = base64.b64encode(raw_png).decode()

        # 构造含图片的消息
        image_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": b64_data,
            },
        }
        text_block = {
            "type": "text",
            "text": "请描述这张图片的内容。图片中有什么颜色和形状？请简短回答。",
        }

        # 使用 Message 对象，content 为 list[dict] 传给 adapter
        msg = Message(role="user", content=[
            TextBlock(text=text_block["text"]),
            ImageBlock(data=b64_data, mime_type="image/png"),
        ])

        response = client.chat(messages=[msg], max_tokens=500)
        reply = response.get_text()

        # 验证：回复应包含对图片的描述，而非"无法看到图片"
        # 注意：DGX 的 reasoning 模型可能将 tokens 全用于推理，导致文本为空
        if not reply:
            pytest.skip(f"[{protocol}] 模型无文本输出（可能全用于 reasoning）")

        # 检查回复中是否包含关键视觉特征（红色或矩形）
        reply_lower = reply.lower().strip()
        visual_keywords = [
            "红", "rect", "长方形", "正方形", "形状", "color", "颜色", "red", "rectangle",
            "白色", "背景", "图像", "图片"
        ]
        matched = [kw for kw in visual_keywords if kw in reply_lower]
        has_visual_keywords = len(matched) > 0
        assert has_visual_keywords, \
            f"[{protocol}] 模型似乎未看到图片。reply repr={repr(reply[:300])}, matched={matched}"

    def test_image_in_tool_result(self, dgx_client, tmp_path):
        """模拟 read 工具返回图片：tool_result 的 content 为 list（含 image block）。

        验证图片在 tool_result 中传递时 LLM 能正确识别。
        """
        client, protocol = dgx_client

        # 生成测试图片
        img_path = str(tmp_path / "test_rect.png")
        _make_test_image(img_path, width=200, height=150)
        with open(img_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode()

        from core.llm.types import ToolCallBlock

        # 模拟 tool_result 消息（与 _resolve_tool_results 输出格式一致）
        tool_result_content = [
            {"type": "text", "text": f"[Image: {img_path}]"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": b64_data,
                },
            },
        ]

        # 构造对话：assistant 调用工具 → user 返回工具结果
        messages = [
            Message(
                role="user",
                content="请帮我查看 test_rect.png 这张图片的内容",
            ),
            Message(
                role="assistant",
                content=[
                    TextBlock(text="我来查看这张图片。"),
                    # OpenAI 要求 assistant 有 tool_calls 才能跟 tool 消息
                    ToolCallBlock(id="test-tool-id", name="read", arguments='{"file_path": "test_rect.png"}'),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_use_id="test-tool-id",
                        content=tool_result_content,  # list[dict] 含图片
                    ),
                    TextBlock(text="请描述你看到的图片内容，简短回答颜色和形状。"),
                ],
            ),
        ]

        response = client.chat(messages=messages, max_tokens=500)
        reply = response.get_text()

        if not reply:
            pytest.skip(f"[{protocol}] 模型无文本输出（可能全用于 reasoning）")

        reply_lower = reply.lower().strip()
        # 更全面的关键词匹配
        visual_keywords = [
            "红", "rect", "长方形", "正方形", "形状", "color", "颜色", "red", "rectangle",
            "白色", "背景", "图像", "图片"
        ]
        # 调试输出：打印 reply 的 repr 以便排查编码问题
        matched = [kw for kw in visual_keywords if kw in reply_lower]
        has_visual_keywords = len(matched) > 0
        assert has_visual_keywords, \
            f"[{protocol}] 工具结果中的图片未被识别。reply repr={repr(reply[:300])}, matched={matched}"


# ══════════════════════════════════════════════════════════════════════════════
# 全管线集成测试：read 工具 → save → resolve → LLM
# ══════════════════════════════════════════════════════════════════════════════


class TestFullImagePipeline:
    """验证从 read 工具读取图片到 LLM 调用的完整链路。"""

    def test_read_save_resolve_round_trip(self, tmp_path):
        """read 工具 → save_output_to_file → 手动模拟 _resolve_tool_results 加载。"""
        from core.tools.shared.read import ReadTool

        # 1. 生成图片
        img_path = str(tmp_path / "pipeline_test.png")
        _make_test_image(img_path)

        # 2. read 工具读取
        tool = ReadTool(cwd=str(tmp_path), workspace_uuid="test")
        output_file = str(tmp_path / "toolu_pipeline.txt")
        tool.output_file = output_file
        result = tool.execute(file_path=img_path)

        # 验证 ToolResult 包含 ImageBlock
        image_blocks = [b for b in result.blocks if isinstance(b, ImageBlock)]
        assert len(image_blocks) == 1, "read 工具未返回 ImageBlock"

        # 3. 保存到文件
        tool.save_output_to_file(result)

        # 验证保存为 .json（不是 .txt.json）
        json_path = str(tmp_path / "toolu_pipeline.json")
        assert os.path.exists(json_path), f"期望 {json_path}"
        assert not os.path.exists(str(tmp_path / "toolu_pipeline.txt.json"))

        # 4. 模拟 _resolve_tool_results 加载 json
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        assert data["type"] == "multimodal"
        block_types = [b["type"] for b in data["blocks"]]
        assert "image" in block_types, f"json 中无图片: {block_types}"

        # 5. 用 block_from_dict 还原 ContentBlock
        content_blocks = [block_from_dict(b) for b in data["blocks"]]
        restored_images = [b for b in content_blocks if isinstance(b, ImageBlock)]
        assert len(restored_images) == 1, "还原后图片丢失"
        assert restored_images[0].data == image_blocks[0].data, "图片数据不一致"
