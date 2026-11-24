# 消息压缩设计

> 按当前实现重写（2026-11-25）。代码：`core/compression.py`、
> `core/session_runner_runtime/executor.py`、`core/session_runner_runtime/conversation.py`。

## 一、三层压缩

| 层 | 触发 | 行为 | 实现 |
|---|---|---|---|
| Microcompact | 每轮执行前 | 标记孤立工具结果/旧错误结果失效、大结果外置 | `compression.microcompact_mark_orphans_and_errors` |
| Full compact | 上下文达到 `max_context_tokens * 0.80` | 保留最近对话 + 摘要化更早内容（写 summary commit） | `TurnExecutor._perform_full_compact` |
| Emergency | 请求体仍超限/413 | 逐级降级（历史图片降级、工具结果清空） | `executor` 的图片预算与 `_apply_model_content_rules` |

## 二、常量

```
KEEP_USER_MESSAGES = 3                 # full compact 保留的最近用户消息数
FULL_COMPACT_TOKEN_RATIO = 0.80        # 触发 full compact 的窗口占比
COMPACT_KEEP_TOKEN_RATIO = 0.25        # 压缩后保留的 token 比例（按窗口计算）
COMPACT_KEEP_MIN_TOKENS = 4000
COMPACT_KEEP_MAX_TOKENS = 32000
```

`_compact_keep_tokens()` = clamp(窗口 × 0.25, 4k, 32k)。

## 三、切分点

1. `find_split_by_user_messages(messages, keep_user_count)`：按用户消息数切。
2. `find_split_by_token_budget(messages, keep_tokens)`：按 token 预算切。
3. `adjust_split_for_tool_rounds`：把切分点对齐到完整工具回合（不切断 tool_use/tool_result）；
   错误提示消息（error_notice）两侧也做对齐。

## 四、摘要

- `ConversationStore._summarize_messages(split_idx)`：调用模型生成摘要，写 **summary commit**
  （内嵌文本，不进 jsonl）；被摘要化区间在 commits 中标记（`compacted`）。
- 失败重试：摘要失败时保留原视图并记录告警，不丢消息。
- Handoff：`summarize_for_handoff(next_steps)` 用于 fork 超限与跨会话交接。

## 五、失效与投影

- `invalidate_all_messages()`：`valid=False`（消息级），保留 jsonl。
- `_mark_old_images_invalid` / `_demote_current_turn_images`：按图片预算把旧图片降级为引用。
- 模型视图重建（`build_projected_messages`）时：已回答 tool_result 清空内联内容，
  大结果改 `output_path` 引用；图片按 `images.history_mode` 投影。

## 六、token 估算

`core/compression`：

- `approx_token_count`：中文约 2.5 字符/token，其他约 4 字符/token。
- `count_tokens_approx` / `count_messages_tokens`：按内容块累加（图片按基准 750 token +
  base64 体积折算）。
- 工具输出截断：`truncate_middle`（token 预算）/ `truncate_result`（字符上限，
  上限集中在 `core/tool_limits`）。

## 七、配置

- 窗口：`ResolvedModel.max_context_tokens`。
- 图片历史：`images.history_mode`（full / current_turn）与图片预算（`images.max_*`）。
- 请求体估算为 system prompt 与工具定义预留保守开销（`estimate_request_body_size`）。
