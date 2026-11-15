"""用量统计的纯 dict 累加工具。

单一职责：把一份 usage 快照并入另一份（计数器 + per-model 分桶）。
速度滚动平均（prefill_speed / generation_speed 及权重）与本次累加无关，
不参与合并 —— 子执行的速度不能代表父级调用速度。

使用方：
- ``ConversationStore.merge_usage``：子代理用量并回父 runner（epoch 续任播种）
- ``core.session.usage_total_for_session_dir``：会话级用量聚合（读取时）
"""

from __future__ import annotations

from typing import Any

COUNTER_KEYS = (
    "input_tokens",
    "output_tokens",
    "api_calls",
    "cache_read_tokens",
    "cache_creation_tokens",
)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def add_usage(target: dict[str, Any], extra: dict[str, Any] | None) -> dict[str, Any]:
    """把 ``extra`` 并入 ``target``（原地），返回 target。"""
    if not isinstance(extra, dict):
        return target
    for key in COUNTER_KEYS:
        target[key] = _int(target.get(key)) + _int(extra.get(key))
    models = extra.get("models")
    if isinstance(models, dict):
        target_models = target.setdefault("models", {})
        for name, bucket in models.items():
            if not isinstance(bucket, dict):
                continue
            entry = target_models.setdefault(str(name), {key: 0 for key in COUNTER_KEYS})
            for key in COUNTER_KEYS:
                entry[key] = _int(entry.get(key)) + _int(bucket.get(key))
    return target
