"""路径目标模型：OP_* 与 PathTarget（解析层/策略层共用的数据类）。"""

from __future__ import annotations

from dataclasses import dataclass

OP_WRITE = "write"
OP_DELETE = "delete"
OP_READ = "read"

@dataclass
class PathTarget:
    """一个待判定的路径目标。

    static / prefixed / opaque 三态：
    - static: resolved 非空，路径完全可静态确认；
    - prefixed: resolved 为空但 static_prefix 非空，动态部分位于稳定目录下；
    - opaque: 两者都为空，只能按代码 + 目标序号做会话级批准。
    """

    op: str  # OP_READ / OP_WRITE / OP_DELETE
    raw: str
    op_name: str = ""
    resolved: str | None = None
    static_prefix: str | None = None
    display: str = ""
    source: str = ""
    code_hash: str = ""
    target_index: int = 0
    suggested_rule: dict | None = None

    @property
    def dynamic(self) -> bool:
        """路径不可静态解析（prefixed / opaque）——由 resolved 推导，不单独存字段。"""
        return self.resolved is None

    @property
    def dynamic_kind(self) -> str:
        if self.resolved:
            return "static"
        if self.static_prefix:
            return "prefixed"
        return "opaque"
