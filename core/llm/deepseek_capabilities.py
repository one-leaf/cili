"""模型能力判定：DeepSeek 服务端联网工具是否可用。

服务端工具（deepseek_web_search / deepseek_read_url）只有 DeepSeek 的
Anthropic 兼容端点在部分模型上支持，且上游没有能力查询接口。判定分两层：

- ``is_deepseek_model`` / ``tools_visible``：模型是否属于 DeepSeek、协议是否为
  anthropic。名字门控只回答「是不是 DeepSeek」，具体模型是否支持由实际请求决定。
- ``mark_unsupported``：工具首次收到 ``unsupported server tool`` /
  ``Access denied for web search`` 后，把该模型记入进程级禁用集合，后续请求
  不再下发这两个工具。进程重启后重新探测一次。
"""

from __future__ import annotations

import threading
from urllib.parse import urlparse

# 进程级降级缓存：模型名（小写）→ 不支持
_disabled_models: set[str] = set()
_lock = threading.Lock()


def is_deepseek_model(model) -> bool:
    """模型是否属于 DeepSeek（官方端点、预设 backend 或网关别名）。"""
    if model is None:
        return False
    if (getattr(model, "backend", "") or "").lower() == "deepseek":
        return True
    if "deepseek" in (getattr(model, "name", "") or "").lower():
        return True
    host = urlparse(getattr(model, "base_url", "") or "").hostname or ""
    return host == "api.deepseek.com" or host.endswith(".deepseek.com")


def tools_visible(model) -> bool:
    """当前模型/协议下是否可见并允许调用 DeepSeek 服务端联网工具。"""
    if model is None:
        return False
    if getattr(model, "protocol", "") != "anthropic":
        return False
    if not is_deepseek_model(model):
        return False
    return _key(model) not in _disabled_models


def mark_unsupported(model_name: str) -> None:
    """记录某模型不支持服务端联网工具（进程级，线程安全）。"""
    key = (model_name or "").strip().lower()
    if not key:
        return
    with _lock:
        _disabled_models.add(key)


def is_unsupported(model_name: str) -> bool:
    """某模型是否已被标记为不支持。"""
    return (model_name or "").strip().lower() in _disabled_models


def reset_unsupported_cache() -> None:
    """清空降级缓存（测试用）。"""
    with _lock:
        _disabled_models.clear()


def _key(model) -> str:
    return (getattr(model, "name", "") or "").strip().lower()
