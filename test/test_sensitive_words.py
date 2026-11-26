"""敏感词上行替换：规则读写、文本替换、Message/system 脱敏、LLMClient 接线。"""

import asyncio
import json
from types import SimpleNamespace

from core.llm.client import LLMClient
from core.llm.types import (
    ImageBlock,
    Message,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from core.sensitive_words import (
    load_rules,
    read_config,
    redact_messages,
    write_config,
)


def _rules(sensitive_dir, pairs, enabled=True, fixed=None):
    write_config(sensitive_dir, {
        "enabled": enabled,
        "fixed": fixed if fixed is not None else {"phone": True, "id_card": True, "bank_card": True},
        "rules": [{"word": w, "replacement": r} for w, r in pairs],
    })
    return load_rules(sensitive_dir)


# ─── 规则读写 ────────────────────────────────────────────────────

def test_write_read_roundtrip(tmp_path):
    write_config(tmp_path, {
        "enabled": True,
        "rules": [{"word": " 朱雀计划 ", "replacement": "某项目"}, {"word": "朱雀计划"}],
    })
    config = read_config(tmp_path)
    assert config == {
        "enabled": True,
        # 未显式给出的固定规则默认关闭
        "fixed": {"phone": False, "id_card": False, "bank_card": False},
        "rules": [{"word": "朱雀计划", "replacement": "某项目"}],
    }

    rules = load_rules(tmp_path)
    assert rules.active
    assert rules.replace_text("朱雀计划二期") == "某项目二期"


def test_missing_or_disabled_is_inactive(tmp_path):
    assert load_rules(tmp_path).active is False
    _rules(tmp_path, [("朱雀计划", "某项目")], enabled=False)
    assert load_rules(tmp_path).active is False


# ─── 文本替换 ────────────────────────────────────────────────────

def test_longest_first_and_no_cascade(tmp_path):
    rules = _rules(tmp_path, [("AB", "X"), ("X", "Y"), ("A", "Z")])
    # 长词优先：AB 整体命中，而不是先被 A 拆掉
    assert rules.replace_text("AB") == "X"
    # 单次扫描：替换产物 X 不会被 X→Y 二次改写；只有原文里的 X 才会
    assert rules.replace_text("AB X A") == "X Y Z"


def test_replace_empty_replacement(tmp_path):
    rules = _rules(tmp_path, [("机密", "")])
    assert rules.replace_text("这是机密内容") == "这是内容"


# ─── 固定规则（手机号 / 身份证号 / 银行卡号） ───────────────────

def test_fixed_patterns(tmp_path):
    rules = _rules(tmp_path, [])
    assert rules.replace_text("电话 13812345678 结束") == "电话 [手机号] 结束"
    assert rules.replace_text("身份证 11010519491231002X") == "身份证 [身份证号]"
    assert rules.replace_text("旧版 130503670401001") == "旧版 [身份证号]"
    assert rules.replace_text("卡号 6222021234567894") == "卡号 [银行卡号]"


def test_fixed_patterns_require_valid_checksum(tmp_path):
    rules = _rules(tmp_path, [])
    # 身份证校验码错误 / 银行卡 Luhn 不通过 → 视为普通数字，不替换
    assert rules.replace_text("110105194912310021") == "110105194912310021"
    assert rules.replace_text("6222021234567890") == "6222021234567890"


def test_fixed_phone_needs_digit_boundary(tmp_path):
    rules = _rules(tmp_path, [])
    # 长数字串内部的 11 位片段不当作手机号
    assert rules.replace_text("138123456789") == "138123456789"
    assert rules.replace_text("013812345678") == "013812345678"


def test_fixed_toggles(tmp_path):
    write_config(tmp_path, {
        "enabled": True,
        "fixed": {"phone": False, "id_card": False, "bank_card": False},
        "rules": [],
    })
    assert load_rules(tmp_path).active is False

    write_config(tmp_path, {
        "enabled": True,
        "fixed": {"phone": False, "id_card": True, "bank_card": False},
        "rules": [{"word": "机密", "replacement": "内容"}],
    })
    rules = load_rules(tmp_path)
    assert rules.replace_text("机密 13812345678") == "内容 13812345678"
    assert rules.replace_text("11010519491231002X") == "[身份证号]"


# ─── Message / system 脱敏 ──────────────────────────────────────

def test_redact_messages_blocks_and_no_mutation(tmp_path):
    _rules(tmp_path, [("朱雀计划", "某项目")])
    text = TextBlock(text="朱雀计划的预算")
    thinking = ReasoningBlock(text="朱雀计划很敏感", signature="sig")
    tool_str = ToolResultBlock(tool_use_id="t1", content="文件里写着朱雀计划")
    tool_list = ToolResultBlock(tool_use_id="t2", content=[
        {"type": "text", "text": "朱雀计划清单"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
    ])
    tool_call = ToolCallBlock(id="c1", name="write", arguments=json.dumps({"content": "朱雀计划"}))
    image = ImageBlock(data="BBB", mime_type="image/png")

    msg = Message(role="user", content=[text, thinking, tool_str, tool_list, tool_call, image])
    messages, system = redact_messages([msg], "系统：朱雀计划", tmp_path)

    out = messages[0].content
    assert out[0].text == "某项目的预算"
    # thinking 与 tool_use 参数是模型输出，改文本会破坏签名/JSON，原样保留
    assert out[1] is thinking
    assert out[1].text == "朱雀计划很敏感"
    assert out[1].signature == "sig"
    assert out[2].content == "文件里写着某项目"
    assert out[3].content[0]["text"] == "某项目清单"
    assert out[3].content[1]["type"] == "image"
    assert out[4] is tool_call
    assert out[5] is image
    assert system == "系统：某项目"

    # 入参不被修改：会话存储里的原文保持不变
    assert text.text == "朱雀计划的预算"
    assert thinking.text == "朱雀计划很敏感"
    assert tool_str.content == "文件里写着朱雀计划"
    assert tool_list.content[0]["text"] == "朱雀计划清单"
    assert msg.content[0] is text


def test_redact_system_list_and_string_content(tmp_path):
    _rules(tmp_path, [("朱雀计划", "某项目")])
    messages, system = redact_messages(
        [Message(role="user", content="朱雀计划说明")],
        ["朱雀计划", "___DYNAMIC_BOUNDARY___", "规则"],
        tmp_path,
    )
    assert messages[0].content == "某项目说明"
    assert system == ["某项目", "___DYNAMIC_BOUNDARY___", "规则"]


def test_no_rules_returns_same_objects(tmp_path):
    messages = [Message(role="user", content="朱雀计划")]
    out, system = redact_messages(messages, "朱雀计划", tmp_path)
    assert out is messages
    assert system == "朱雀计划"


def test_corrupt_file_kept_and_recovers(tmp_path):
    """损坏文件不能被静默改名删掉；修好后规则立即恢复。"""
    path = tmp_path / "sensitive_words.json"
    path.write_text("{bad json", encoding="utf-8")

    assert load_rules(tmp_path).active is False
    assert path.exists()

    write_config(tmp_path, {
        "enabled": True,
        "fixed": {"phone": True, "id_card": True, "bank_card": True},
        "rules": [{"word": "机密", "replacement": "内容"}],
    })
    assert load_rules(tmp_path).replace_text("机密") == "内容"


# ─── LLMClient 接线：serialize 前替换 ────────────────────────────

class _RecordingAdapter:
    requires_streaming = False
    api_url = "http://stub"

    def __init__(self):
        self.sent = None

    def serialize(self, **kwargs):
        self.sent = kwargs
        return {}

    def build_request_headers(self, session_id=""):
        return {}

    def parse_response(self, data):
        return [], "end_turn", None


class _StubTransport:
    def post(self, url, headers, body, timeout=None):
        return 200, {}, {"model": "stub"}

    def with_retry(self, fn, total_timeout=None):
        return fn()

    def close(self):
        pass


def _client(sensitive_dir):
    config = SimpleNamespace(
        name="stub", max_output_tokens=64, base_url="",
        auth_type="", credential="",
    )
    adapter = _RecordingAdapter()
    client = LLMClient(
        adapter=adapter, transport=_StubTransport(), config=config,
        sensitive_dir=sensitive_dir,
    )
    return client, adapter


def test_client_redacts_before_serialize(tmp_path):
    _rules(tmp_path, [("朱雀计划", "某项目")])
    client, adapter = _client(tmp_path)
    original = Message(role="user", content=[TextBlock(text="朱雀计划预算")])

    client.chat([original], system="系统：朱雀计划")

    sent = adapter.sent
    assert sent["messages"][0].content[0].text == "某项目预算"
    assert sent["system"] == "系统：某项目"
    # 原 Message 不受影响（会话存储保留原文）
    assert original.content[0].text == "朱雀计划预算"


def test_client_without_rules_passes_through(tmp_path):
    client, adapter = _client(tmp_path)
    original = Message(role="user", content=[TextBlock(text="朱雀计划预算")])

    client.chat([original], system="系统")

    assert adapter.sent["messages"][0] is original
    assert adapter.sent["system"] == "系统"


# ─── 路由端点 ────────────────────────────────────────────────────

def test_api_get_put(tmp_path):
    from web import routes_workspace

    request = routes_workspace.SensitiveWordsRequest(
        enabled=True,
        fixed={"phone": False, "id_card": True, "bank_card": True},
        rules=[{"word": "朱雀计划", "replacement": "某项目"}],
    )
    saved = asyncio.run(routes_workspace.save_sensitive_words(request, ws_dir=tmp_path))
    assert saved["rules"] == [{"word": "朱雀计划", "replacement": "某项目"}]
    assert saved["fixed"] == {"phone": False, "id_card": True, "bank_card": True}

    loaded = asyncio.run(routes_workspace.get_sensitive_words(ws_dir=tmp_path))
    assert loaded == saved

    # 缺文件时返回默认配置（全部关闭，等待用户勾选）
    empty = asyncio.run(routes_workspace.get_sensitive_words(ws_dir=tmp_path / "none"))
    assert empty == {
        "enabled": False,
        "fixed": {"phone": False, "id_card": False, "bank_card": False},
        "rules": [],
    }
