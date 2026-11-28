"""记忆索引/摘要的布局兼容。

旧布局：``{workspace}/.cili/memory/MEMORY.md``、``summary.md``
新布局：``{workspace}/.cili/cache/memory/MEMORY.md``、``summary.md``
M3 阶段读取优先新路径；写入仍由调用方决定（M7 再切）。
"""

from __future__ import annotations

from pathlib import Path


def _cache_dir(memory_dir: str | Path) -> Path:
    return Path(memory_dir).parent / "cache" / "memory"


def resolve_existing(memory_dir: str | Path, name: str) -> Path:
    """返回优先的新路径（存在时）或旧路径。"""
    old = Path(memory_dir) / name
    new = _cache_dir(memory_dir) / name
    return new if new.is_file() else old


def read_text(memory_dir: str | Path, name: str) -> str:
    """读取索引/摘要；旧/新路径都不存在时返回空串。"""
    path = resolve_existing(memory_dir, name)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
