"""ARIA snapshot YAML 解析：为可交互元素分配 ref。"""

from __future__ import annotations

import re

# aria_snapshot YAML 行解析（ref 定位模型）
# 行形态：
#   - banner:                 容器（无 name）
#   - button "Sign in"        带名元素（可带内联属性如 [level=1] 或子节点冒号）
#   - paragraph: some text    文本形式（role: content）
#   - /url: /home             属性行（保留显示，不分配 ref）
_SNAP_EL_RE = re.compile(
    r'^(\s*)-\s+(\w+)(?:\s+"((?:[^"\\]|\\.)*)")?(?:\s+\[([^\]]*)\])?:?\s*$'
)
_SNAP_TEXT_RE = re.compile(r"^(\s*)-\s+(\w+):\s+(.+?)\s*$")

# 无「可访问名」时仍分配 ref 的角色。快照里无名元素照样会显示，若不给 ref，模型就
# 看得见却寻址不了（典型：JS 注入的 <div role="button"> 没有文本标签）。只对交互类
# 角色放宽，避免把 ref 预算耗在 - generic: 这类结构性节点上（SNAPSHOT_MAX_REFS 有限）。
INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "checkbox", "radio", "switch",
    "combobox", "listbox", "option", "slider", "spinbutton", "menuitem",
    "menuitemcheckbox", "menuitemradio", "tab", "treeitem", "gridcell",
})

# 单次 snapshot 最多分配的 ref 数（超出的元素保留显示但不给 ref）
SNAPSHOT_MAX_REFS = 400


def unescape_quoted(s: str) -> str:
    """反转义 aria_snapshot 引号 name 中的 \" 和 \\。"""
    return s.replace('\\"', '"').replace("\\\\", "\\")


def parse_aria_snapshot(yaml_text: str, max_refs: int = SNAPSHOT_MAX_REFS):
    """解析 aria_snapshot YAML，为可交互元素分配 ref。

    Returns:
        lines: list[tuple[str, str]] — (indent, 带 ref 标注的重建行)
        ref_map: dict[str, dict] — ref → {"role", "name", "index", "mode"}
            index: 同 (role, name) 元素出现次序（0 起），用于 get_by_role().nth()
            mode: "role" 用 get_by_role 定位，"text" 用 get_by_text 定位
    """
    ref_map: dict[str, dict] = {}
    lines_out: list[tuple[str, str]] = []
    seen: dict[tuple, int] = {}
    ref_counter = 0
    for line in yaml_text.splitlines():
        if not line.strip():
            continue
        m = _SNAP_EL_RE.match(line)
        if m:
            indent, role, name, _attrs = m.groups()
            if name or role in INTERACTIVE_ROLES:
                key = (role, name or "")
                idx = seen.get(key, 0)
                seen[key] = idx + 1
                if ref_counter < max_refs:
                    ref_counter += 1
                    ref = f"r{ref_counter}"
                    ref_map[ref] = {
                        "role": role,
                        "name": unescape_quoted(name) if name else "",
                        "index": idx,
                        "mode": "role",
                    }
                    line = line.rstrip() + f" [ref={ref}]"
            lines_out.append((indent, line))
            continue
        m = _SNAP_TEXT_RE.match(line)
        if m:
            indent, role, text = m.groups()
            key = (role, text)
            idx = seen.get(key, 0)
            seen[key] = idx + 1
            if ref_counter < max_refs:
                ref_counter += 1
                ref = f"r{ref_counter}"
                ref_map[ref] = {
                    "role": role,
                    "name": text,
                    "index": idx,
                    "mode": "text",
                }
                line = line.rstrip() + f" [ref={ref}]"
            lines_out.append((indent, line))
            continue
        # 属性行（/url 等）原样保留，不分配 ref
        lines_out.append(("", line))
    return lines_out, ref_map
