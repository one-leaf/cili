"""Agent 执行统计的唯一值对象。

同一个 exec_id 的 iterations / message_count / tool_call_count 只在这里定义，
其他层通过 to_dict/from_dict 传递嵌套 stats，避免字段名和默认值各自演化。
"""

from __future__ import annotations

from dataclasses import dataclass


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class AgentStats:
    iterations: int = 0
    message_count: int = 0
    tool_call_count: int = 0

    def __add__(self, other: "AgentStats") -> "AgentStats":
        if not isinstance(other, AgentStats):
            return NotImplemented
        return AgentStats(
            iterations=self.iterations + other.iterations,
            message_count=self.message_count + other.message_count,
            tool_call_count=self.tool_call_count + other.tool_call_count,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "iterations": self.iterations,
            "message_count": self.message_count,
            "tool_call_count": self.tool_call_count,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "AgentStats":
        if not isinstance(data, dict):
            return cls()
        return cls(
            iterations=_non_negative_int(data.get("iterations")),
            message_count=_non_negative_int(data.get("message_count")),
            tool_call_count=_non_negative_int(data.get("tool_call_count")),
        )
