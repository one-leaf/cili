"""敏感词替换：上行请求防泄密（工作区级）。

目标不是内容审查，而是**防止工作区里的关键词随请求上传到模型服务商**。
替换点在上行边界（``LLMClient.chat`` / ``chat_stream`` 调 adapter.serialize 前），
会话落盘、本地展示、导出都保留原文——只有发给模型的副本被替换。

规则文件：``{workspace}/.cili/sensitive_words.json``

    {"enabled": true,
     "fixed": {"phone": true, "id_card": true, "bank_card": true},
     "rules": [{"word": "朱雀计划", "replacement": "某项目"}]}

固定规则（手机号 / 身份证号 / 银行卡号）走业界通用判定，减少误杀：
- 手机号：中国大陆 11 位 ``1[3-9]`` + 8 位，前后不能紧邻其他数字
- 身份证号：GB 11643 18 位（出生日期 + 校验码）与旧版 15 位（出生月日）
- 银行卡号：16–19 位连续数字 + Luhn 校验

字面词与固定规则合并成一次正则扫描（固定规则在前、字面词长词优先），避免
``A→B`` 之后再被 ``B→C`` 二次改写。单向替换：模型看不到原文，工具链以替换词为准。

读取按 (path, mtime_ns, size) 指纹缓存，规则文件改动即时生效。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, replace as dc_replace
from datetime import date
from pathlib import Path
from typing import Any

from core.fs_utils import atomic_write_json

logger = logging.getLogger(__name__)

CONFIG_NAME = "sensitive_words.json"
DIR_NAME = "sensitive_words"
SETTINGS_NAME = "settings.json"
RULES_DIR = "rules"

# 规则条数上限：防止正则 alternation 过长拖慢每次请求
MAX_RULES = 500

# ─── 固定规则（业界通用格式 + 校验位） ──────────────────────────

# 中国大陆手机号：1[3-9] 开头 11 位，两侧不能是数字（避免长数字串内误命中）
_PHONE_RE = r"(?<!\d)1[3-9]\d{9}(?!\d)"
# 身份证号：18 位（GB 11643，含出生日期与校验码）优先，其次旧版 15 位
_ID_CARD_RE = (
    r"(?<!\d)(?:"
    r"[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]"
    r"|[1-9]\d{5}\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}"
    r")(?!\d)"
)
# 银行卡号：16–19 位连续数字，命中后按 Luhn 校验（空格/短横分隔的卡号不处理）
_BANK_CARD_RE = r"(?<!\d)\d{16,19}(?!\d)"

# 顺序即 alternation 顺序：身份证 → 银行卡 → 手机号（长格式先试）
_FIXED_SPECS: tuple[tuple[str, str], ...] = (
    ("id_card", _ID_CARD_RE),
    ("bank_card", _BANK_CARD_RE),
    ("phone", _PHONE_RE),
)
_FIXED_KINDS = tuple(kind for kind, _ in _FIXED_SPECS)
_FIXED_LABELS = {"phone": "[手机号]", "id_card": "[身份证号]", "bank_card": "[银行卡号]"}

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"


def _valid_date(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
        return True
    except ValueError:
        return False


def _id_card_valid(text: str) -> bool:
    """18 位：出生日期 + GB 11643 校验码；15 位旧版：只校验出生月日。"""
    if len(text) == 18:
        if not _valid_date(int(text[6:10]), int(text[10:12]), int(text[12:14])):
            return False
        total = sum(int(c) * w for c, w in zip(text[:17], _ID_WEIGHTS))
        return _ID_CHECK_CHARS[total % 11] == text[17].upper()
    return _valid_date(1900 + int(text[6:8]), int(text[8:10]), int(text[10:12]))


def _bank_card_valid(text: str) -> bool:
    """Luhn 校验；位数 16–19。"""
    digits = [int(c) for c in text if c.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return 16 <= len(digits) <= 19 and total % 10 == 0


@dataclass(frozen=True)
class SensitiveRules:
    """编译后的规则集；未启用或无规则时 pattern 为 None（快路径）。"""

    enabled: bool = False
    pattern: re.Pattern[str] | None = None
    mapping: dict[str, str] | None = None

    @property
    def active(self) -> bool:
        return self.enabled and self.pattern is not None

    def _replace_match(self, match: re.Match) -> str:
        kind = match.lastgroup
        if kind is not None:
            text = match.group(0)
            # 校验位不合法视为普通数字，不替换（避免误杀工具输出里的编号）
            if kind == "id_card" and not _id_card_valid(text):
                return text
            if kind == "bank_card" and not _bank_card_valid(text):
                return text
            return _FIXED_LABELS[kind]
        return (self.mapping or {}).get(match.group(0), match.group(0))

    def replace_text(self, text: str) -> str:
        if not self.pattern or not text:
            return text
        return self.pattern.sub(self._replace_match, text)


_EMPTY = SensitiveRules()

# path -> (fingerprint, rules)；指纹变了才重读
_cache: dict[str, tuple[tuple[int, int] | None, SensitiveRules]] = {}
_cache_lock = threading.Lock()


def config_path(sensitive_dir: str | Path) -> Path:
    """返回配置位置：优先已存在的目录，否则旧单文件。"""
    base = Path(sensitive_dir)
    for candidate in (base / DIR_NAME, base / "config" / DIR_NAME):
        if candidate.is_dir():
            return candidate
    return base / CONFIG_NAME


def _read_dir_config(path: Path) -> dict:
    settings, _ = _read_json(path / SETTINGS_NAME)
    if not isinstance(settings, dict):
        settings = {}
    rules: list[dict] = []
    rules_dir = path / RULES_DIR
    if rules_dir.is_dir():
        for file in sorted(rules_dir.glob("*.json")):
            item, _ = _read_json(file)
            if isinstance(item, dict) and item.get("word"):
                rules.append(item)
    return {
        "enabled": settings.get("enabled", False),
        "fixed": settings.get("fixed", {}),
        "rules": rules,
    }


def _read_config_at(path: Path) -> tuple[dict, bool]:
    if path.is_dir():
        return _read_dir_config(path), True
    data, ok = _read_json(path)
    return (data if isinstance(data, dict) else {}), ok


def _config_sig(path: Path) -> tuple | None:
    try:
        if path.is_dir():
            items = []
            for file in sorted(path.rglob("*.json")):
                st = file.stat()
                items.append((str(file.relative_to(path)), st.st_mtime_ns, st.st_size))
            return tuple(items)
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _normalize_config(data: Any) -> dict:
    """把文件/请求内容归一化为 enabled / fixed / rules 三段。

    ``enabled`` 与 ``fixed`` 均缺省关闭：由用户在设置页逐项勾选。
    """
    if not isinstance(data, dict):
        data = {}
    enabled = bool(data.get("enabled", False))

    fixed_raw = data.get("fixed")
    if not isinstance(fixed_raw, dict):
        fixed_raw = {}
    fixed = {kind: bool(fixed_raw.get(kind, False)) for kind in _FIXED_KINDS}

    rules: list[dict] = []
    seen: set[str] = set()
    for item in data.get("rules") or []:
        if not isinstance(item, dict):
            continue
        word = item.get("word")
        replacement = item.get("replacement", "")
        if not isinstance(word, str) or not isinstance(replacement, str):
            continue
        word = word.strip()
        if not word or word in seen:
            continue
        seen.add(word)
        rules.append({"word": word, "replacement": replacement})
        if len(rules) >= MAX_RULES:
            break
    return {"enabled": enabled, "fixed": fixed, "rules": rules}


def _build_rules(config: dict) -> SensitiveRules:
    config = _normalize_config(config)
    mapping = {r["word"]: r["replacement"] for r in config["rules"]}

    alternatives = [
        f"(?P<{kind}>{pattern})"
        for kind, pattern in _FIXED_SPECS
        if config["fixed"].get(kind)
    ]
    if mapping:
        # 长词优先：保证 "敏感词长" 先于 "敏感词" 匹配
        words = sorted(mapping, key=len, reverse=True)
        alternatives.append("|".join(re.escape(w) for w in words))
    if not config["enabled"] or not alternatives:
        return _EMPTY
    return SensitiveRules(enabled=True, pattern=re.compile("|".join(alternatives)), mapping=mapping)


def _read_json(path: Path) -> tuple[Any, bool]:
    """读取规则文件；失败时不改名、不丢文件（规则是用户秘密清单，不能静默清掉）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), True
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError) as e:
        logger.warning("[sensitive] 读取规则失败，本次按未配置处理: %s (%s)", path, e)
        return {}, False


def load_rules(sensitive_dir: str | Path) -> SensitiveRules:
    """读取并缓存工作区规则；文件/目录不存在视为未启用。"""
    path = config_path(sensitive_dir)
    key = os.path.normcase(str(path))
    sig = _config_sig(path)

    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == sig:
            return cached[1]
        if sig is None:
            rules = _EMPTY
        else:
            data, ok = _read_config_at(path)
            if not ok:
                # 读取失败不落缓存：下次请求重试，避免一次瞬时错误让过滤静默停用
                return cached[1] if cached is not None else _EMPTY
            rules = _build_rules(data)
        _cache[key] = (sig, rules)
        return rules


def read_config(sensitive_dir: str | Path) -> dict:
    """供设置页读取的归一化配置（不编译）。"""
    path = config_path(sensitive_dir)
    if not path.exists():
        return _normalize_config({})
    data, _ok = _read_config_at(path)
    return _normalize_config(data)


def write_config(sensitive_dir: str | Path, data: Any) -> dict:
    """写入归一化配置并失效缓存；已迁移目录时写 settings + 一词一文件。"""
    path = config_path(sensitive_dir)
    normalized = _normalize_config(data)
    if path.is_dir():
        rules_dir = path / RULES_DIR
        rules_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            path / SETTINGS_NAME,
            {"enabled": normalized["enabled"], "fixed": normalized["fixed"]},
        )
        names = set()
        for rule in normalized["rules"]:
            name = hashlib.sha1(rule["word"].encode("utf-8")).hexdigest()[:16] + ".json"
            names.add(name)
            atomic_write_json(rules_dir / name, rule)
        for file in rules_dir.glob("*.json"):
            if file.name not in names:
                file.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, normalized)
    with _cache_lock:
        _cache.pop(os.path.normcase(str(path)), None)
    return normalized


# ─── Message / system 脱敏 ───────────────────────────────────────


def _redact_content_list(items: list, rules: SensitiveRules) -> tuple[list, bool]:
    """tool_result 的多模态列表：只替换文本块的 text 字段。"""
    out: list = []
    changed = False
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            new_text = rules.replace_text(item["text"])
            if new_text != item["text"]:
                item = {**item, "text": new_text}
                changed = True
        out.append(item)
    return out, changed


def _redact_message(msg: Any, rules: SensitiveRules) -> Any:
    # 延迟导入：core.llm 包导入 client，client 又导入本模块，顶层导入会成环
    from core.llm.types import TextBlock, ToolResultBlock

    content = getattr(msg, "content", None)
    if isinstance(content, str):
        new_text = rules.replace_text(content)
        return msg if new_text == content else dc_replace(msg, content=new_text)
    if not isinstance(content, list):
        return msg

    out: list = []
    changed = False
    for block in content:
        if isinstance(block, TextBlock):
            new_text = rules.replace_text(block.text)
            new_block = block if new_text == block.text else TextBlock(text=new_text)
        elif isinstance(block, ToolResultBlock):
            if isinstance(block.content, str):
                new_text = rules.replace_text(block.content)
                new_block = block if new_text == block.content else dc_replace(block, content=new_text)
            elif isinstance(block.content, list):
                new_content, hit = _redact_content_list(block.content, rules)
                new_block = dc_replace(block, content=new_content) if hit else block
            else:
                new_block = block
        else:
            # ToolCallBlock / ReasoningBlock / ImageBlock / ServerBlock 原样保留：
            # - 模型输出（tool_use 参数 / thinking）会被回放，改文本会破坏 JSON 或
            #   thinking 签名（官方 Anthropic 端点按签名绑定原文，改后整轮 400）
            # - 二进制内容无法做词面替换
            new_block = block
        out.append(new_block)
        changed = changed or new_block is not block
    return dc_replace(msg, content=out) if changed else msg


def _redact_system(system: Any, rules: SensitiveRules) -> Any:
    if isinstance(system, str):
        return rules.replace_text(system)
    if isinstance(system, list):
        return [rules.replace_text(s) if isinstance(s, str) else s for s in system]
    return system


def redact_messages(
    messages: list,
    system: Any,
    sensitive_dir: str | Path | None,
) -> tuple[list, Any]:
    """返回替换后的 (messages, system)；无规则时原样返回同一对象。

    不修改入参：只对发生替换的 Message/块创建副本，会话存储里的原文不受影响。
    """
    if not sensitive_dir:
        return messages, system
    rules = load_rules(sensitive_dir)
    if not rules.active:
        return messages, system
    return [_redact_message(m, rules) for m in messages], _redact_system(system, rules)
