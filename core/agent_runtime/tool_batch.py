"""工具批执行器：并发安全工具并行执行（仿 nanobot concurrency_safe 分批）。

Cili 是同步/线程模型，故用 ThreadPoolExecutor 而非 asyncio.gather。
- partition_tool_batches：连续 concurrency_safe 调用合批，其余各成单批。
- execute_tool_calls：先批前预激活 deferred 工具（消除并行激活竞态），
  再逐批执行并保证结果按输入顺序返回。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from core.tools.base import Tool


def is_concurrency_safe(tools: list[Tool], name: str) -> bool:
    """按工具名查注册表判断是否允许并行执行。未知工具视为不安全。"""
    for tool in tools:
        if tool.name == name:
            return bool(tool.concurrency_safe)
    return False


def partition_tool_batches(tools: list[Tool], tool_calls: list) -> list[list]:
    """将 tool_calls 划分为批：连续 concurrency_safe 调用合批，非安全各成单批。

    与 nanobot _partition_tool_batches 同构：安全调用在批内并行，
    非安全调用之间及与安全调用之间保持顺序。
    """
    batches: list[list] = []
    current: list = []
    for tc in tool_calls:
        if is_concurrency_safe(tools, tc.name):
            current.append(tc)
        else:
            if current:
                batches.append(current)
                current = []
            batches.append([tc])
    if current:
        batches.append(current)
    return batches


def execute_tool_calls(
    agent,
    tool_calls: list,
    *,
    parallel: bool = True,
    on_execute: Callable[[str], None] | None = None,
) -> list[dict]:
    """执行一批 tool_calls，返回按输入顺序排列的 tool_result dict 列表。

    - parallel=False（配置关闭）时全顺序执行。
    - 批前统一预激活本批 deferred 工具名（幂等），避免并行激活改共享状态。
    - 长度 >1 的并发安全批用 ThreadPoolExecutor 并发；结果经 pool.map 保序。
    - on_execute(tc.name) 每工具执行前调用（批级进度回调，非逐工具落盘）。
    """
    if not tool_calls:
        return []

    # 批前预激活 deferred 工具（消除并行激活 tool_schemas 竞态）
    deferred = [tc.name for tc in tool_calls
                if tc.name in getattr(agent, "_deferred_names", set())]
    if deferred:
        agent._activate_tools(deferred)

    batches = partition_tool_batches(agent.tools, tool_calls)
    if not parallel:
        batches = [[tc] for batch in batches for tc in batch]

    results: list[dict] = []
    for batch in batches:
        if on_execute is not None:
            on_execute(",".join(tc.name for tc in batch))
        if len(batch) > 1:
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                results.extend(
                    pool.map(
                        lambda tc: agent._execute_tool(tc.name, tc.parse_arguments(), tc.id),
                        batch,
                    )
                )
        else:
            tc = batch[0]
            results.append(agent._execute_tool(tc.name, tc.parse_arguments(), tc.id))
    return results
