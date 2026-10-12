"""QQ 侧的出站文本消毒入口（实现已提取到 ``core/text_sanitize.py``）。

保留本模块是为了让 QQ 的调用点与测试的 import 路径不变。
"""

from core.text_sanitize import strip_scaffolding

# 保留旧名：QQ 的 sink 与测试都用 ``sanitize_qq_text``
sanitize_qq_text = strip_scaffolding

__all__ = ["sanitize_qq_text"]
