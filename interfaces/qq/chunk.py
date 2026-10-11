"""markdown 感知的长文本切分。

纯函数，无状态，可直接测。

为什么不能硬切
--------------
- 切断代码围栏会让**后续内容全渲染成代码块**（围栏没闭合）
- 切断 GFM 表格会让表格渲染崩掉

上限为什么是 4000
-----------------
``qqbot-agent-sdk`` 的 ``build_text_body`` 会按 ``MAX_MESSAGE_LENGTH = 4000``
**静默截断**。若我们按官方 TS 插件的 5000 切，SDK 会把每个分片再切一刀，
直接丢内容。所以上限必须 ≤ 4000。

算法：先按「表格整块 / 单行」切成语义单元，再贪心装箱；跨片的代码围栏在片尾
补闭合、下片开头重新打开。单个单元自身超限时硬切（罕见：超长单行或超大表格）。
"""

from __future__ import annotations

import re

# 与 SDK 的 MAX_MESSAGE_LENGTH 对齐；不要调大（见模块 docstring）
MAX_CHUNK_CHARS = 4000

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _fence_marker(line: str) -> str | None:
    """行首是代码围栏则返回其标记（``` 或 ~~~），否则 None。"""
    m = _FENCE_RE.match(line)
    return m.group(1) if m else None


def _is_table_line(line: str) -> bool:
    """GFM 表格行：去空白后以 ``|`` 开头（覆盖数据行与 ``|---|---|`` 分隔行）。"""
    return line.strip().startswith("|")


def _to_units(text: str) -> list[str]:
    """切成语义单元：连续表格行合成一个单元，其余每行一个单元。"""
    units: list[str] = []
    table_buf: list[str] = []
    for line in text.split("\n"):
        if _is_table_line(line):
            table_buf.append(line)
            continue
        if table_buf:
            units.append("\n".join(table_buf))
            table_buf = []
        units.append(line)
    if table_buf:
        units.append("\n".join(table_buf))
    return units


def _split_oversized(unit: str, limit: int) -> list[str]:
    """超限单元切分：优先按**行边界**（保住表格行的完整），单行仍超限才硬切。

    为什么不能只按字符硬切：一个 >limit 的表格按字符切会把某一行劈成两半，
    整张表渲染崩掉。按行切至少每片仍是合法表格片段。而超限又必须切——SDK 的
    ``build_text_body`` 会按 4000 静默截断，不切就丢内容。
    """
    if len(unit) <= limit:
        return [unit]
    if "\n" not in unit:
        return [unit[i:i + limit] for i in range(0, len(unit), limit)]

    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in unit.split("\n"):
        if len(line) > limit:
            # 单行自身超限：先把已累积的落盘，再硬切这一行
            if cur:
                out.append("\n".join(cur))
                cur, cur_len = [], 0
            out.extend(line[i:i + limit] for i in range(0, len(line), limit))
            continue
        extra = len(line) + (1 if cur else 0)
        if cur and cur_len + extra > limit:
            out.append("\n".join(cur))
            cur, cur_len = [line], len(line)
        else:
            cur.append(line)
            cur_len += extra
    if cur:
        out.append("\n".join(cur))
    return out


def chunk_markdown(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """把文本切成每片 ≤ ``limit`` 字符的片段。

    返回的片段可直接逐条发送；拼接回去（去掉补的围栏）与原文语义一致。
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    # 单元自身就超限 → 先按行边界切，避免装箱时无法处理
    units: list[str] = []
    for unit in _to_units(text):
        if len(unit) <= limit:
            units.append(unit)
        else:
            units.extend(_split_oversized(unit, limit))

    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    fence_open: str | None = None  # 当前片末尾未闭合的围栏标记

    def flush() -> None:
        """把当前片落盘；若围栏未闭合则补闭合，并让下一片重新打开。"""
        nonlocal cur, cur_len
        if not cur:
            return
        body = "\n".join(cur)
        if fence_open:
            body += "\n" + fence_open
        chunks.append(body)
        cur = [fence_open] if fence_open else []
        cur_len = (len(fence_open) + 1) if fence_open else 0

    for unit in units:
        extra = len(unit) + (1 if cur else 0)
        if cur and cur_len + extra > limit:
            flush()
            extra = len(unit) + (1 if cur else 0)
        cur.append(unit)
        cur_len += extra

        marker = _fence_marker(unit)
        if marker:
            fence_open = None if fence_open else marker

    if cur:
        chunks.append("\n".join(cur))

    return chunks or [text]
