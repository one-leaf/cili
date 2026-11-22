"""工具输出上限常量（统一单位：TOKENS / CHARS / BYTES / LINES）。

各工具按自身输出形态选用；数值保持历史语义，只做集中声明。
"""

from __future__ import annotations

# 通用工具结果上限（字符）
TOOL_RESULT_MAX_CHARS = 50_000
# bash/pwsh 更严格，防止命令输出撑爆上下文
BASH_RESULT_MAX_CHARS = 30_000
BASH_MAX_OUTPUT_LINES = 2_000
BASH_MAX_OUTPUT_TOKENS = 10_000
# edit/write 结果文本上限（可按工具分别调整）
EDIT_RESULT_MAX_CHARS = 100_000
WRITE_RESULT_MAX_CHARS = 100_000
# glob 列表上限
GLOB_RESULT_MAX_CHARS = 100_000
# grep 列表/计数输出上限（与 shell 硬上限一致）
GREP_LIST_MAX_CHARS = 30_000
# read 文本：token 预算与单行字符上限
READ_MAX_OUTPUT_TOKENS = 10_000
READ_MAX_CHARS_PER_LINE = 2_000
# read_tool_result：单次读取字节上限与 limit 参数上限
READ_TOOL_RESULT_MAX_BYTES = 9_000
READ_TOOL_RESULT_MAX_LIMIT = 20_000
# 工具结果超过该字节数会外置为文件（runner 与 read_tool_result 的共同契约）
LARGE_OUTPUT_EXTERNALIZE_BYTES = 10_000
# pdf2markdown 结果上限（与通用结果上限一致）
PDF2MD_RESULT_MAX_CHARS = TOOL_RESULT_MAX_CHARS
