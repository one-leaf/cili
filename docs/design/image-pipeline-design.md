# 图片管线设计

> 按当前实现整理（2026-11-28）。代码：`core/image_pipeline.py`、`core/multimodal.py`、
> `core/image_read.py`、`core/config.py`（`ImageConfig`）、`web/routes_chat.py`、
> `core/tools/read.py`（图片/PDF 页回退）。

## 一、目标

统一处理图片的**归一化、内联/引用、持久化与预算**，保证：

- 发给模型 API 的图片不会因尺寸/体积/格式被拒（400）；
- 历史图片不重复携带 base64，控制请求体与 token；
- 会话落盘不保存巨大的 base64，只保存路径/引用；
- Web / QQ / 微信 / 工具读图走同一套语义。

## 二、归一化

`normalize_image_bytes / normalize_base64_image / normalize_image_block`：

- 处理 EXIF 方向；统一到 `DEFAULT_MAX_DIMENSION=4096` 长边、编码目标
  `DEFAULT_MAX_BYTES=2MiB`；PNG/JPEG 编码阶梯（JPEG 质量 85）；
- 单图 base64 服务端硬上限 `MAX_INLINE_BASE64_BYTES=5MB`，超限无条件降级为引用；
- Pillow 不可用时按 base64 体积兜底，超限明确报错；
- 支持格式：PNG/JPEG/WebP/GIF/BMP；SVG 不内联（接口层不收 `image/svg+xml`），按文本源码读。

## 三、来源与落盘

| 来源 | 处理 |
|---|---|
| Web 上传 | `save_image_payload` 写入 `{runner.cwd}/uploads/image_<ts>_<sha8>.<ext>`，返回 `path/media_type/width/height/attachment_id` |
| QQ/微信 媒体 | `multimodal.build_user_input`：位图 → multimodal block；模型不支持多模态时退化为路径；SVG/其他文件/语音转写 → 文本 + 路径 |
| 工具图片（read/PDF 页） | `write_tool_image_sidecar` 写入 `{session_dir}/attachments/{tool_use_id}-{index}.<ext>` |
| 用户图片 meta | `ensure_user_image_meta`：补 `id/turn_id/path`、只归一化一次（`_meta.normalized`），缺 path 时写入 uploads |

`attachment_id` 用内容 sha256，用于去重与附件引用。

## 四、请求投影

`project_images_for_request(messages, current_turn_id)`：

- **当前回合**图片保留 base64；
- **历史回合**、`force_reference`、被降级（3MB/413）或超 5MB 的图片替换为引用文本
  （含路径/尺寸等元数据）；
- 顶层 image 块与 `tool_result` 内嵌图片统一处理；
- `ImageConfig.history_mode=current_turn`（默认）走上述策略；`full` 仅作显式回退。

## 五、请求预算与退役

`ImageConfig`（`setting.json → images`）：

| 字段 | 默认 | 含义 |
|---|---|---|
| `max_dimension` | 4096 | 长边上限 |
| `request_image_max_bytes` | 2 MiB | 单张编码目标 |
| `max_request_image_bytes` | 20 MiB | 请求内 base64 总预算 |
| `max_images_per_request` | 100 | 请求内图片块数量上限 |
| `outbound_image_hard_limit` | 100 | 所有图 ≤4096px 时的放宽上限 |
| `max_images_per_tool_result` | 8 | 单工具结果内联张数，超出转引用 |
| `eviction_batch` / `eviction_floor` | 4 / 3 | 每次退役数量 / 最新载体保底 |
| `dedupe_inline_images` | true | 同内容已内联且未退役时不重复添加 |
| `jpeg_quality` | 85 | 重编码质量 |

`apply_image_budget` 在请求前按“最旧优先”退役：超过数量/字节预算时把最旧的图片
降级为引用，保留最新 `eviction_floor` 个；`collect_current_turn_image_keys` 与投影同一口径。

## 六、持久化

- 写 `messages.jsonl` 前 `sanitize_images_for_persistence` 把图片块替换为引用文本/元数据，
  避免会话文件膨胀；
- 前端渲染图片依赖 sidecar 路径（会话附件目录）与上传路径；
- 记忆/压缩/搜索只看到引用，不接触 base64。

## 七、工具读图

- `read` 的图片分支与 PDF 无文本层的页面回退共用 `image_read`：归一化后返回 multimodal
  `ToolResult`；未超限原样返回（不重编码），超限缩放/转码；
- 工具结果里的图片块在写入会话时由 `write_tool_image_sidecar` 落盘并转引用；
  单工具结果内联超过 `max_images_per_tool_result` 张时其余转路径引用。

## 八、测试

`test/test_image_pipeline.py`、`test/test_image_history.py`、`test/test_image_storage.py`、
`test/test_multimodal_image.py`。

## 九、已知边界

- 归一化依赖 Pillow；缺失时只能按体积拒绝，无法缩放；
- 图片内容去重按 sha256；同图不同编码视为不同内容；
- 历史图片以引用形式存在，模型需要时可再用 `read`/`read_tool_result` 取回；
- 预算参数是全局配置，没有按会话/工作区单独覆盖。
