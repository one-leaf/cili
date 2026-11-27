"""Git-visible file helper：让 glob/grep 尊重项目的 .gitignore。

非 git 目录/未安装 git 时返回 None，调用方回退到原有文件系统遍历。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable


def git_visible_paths(root: str) -> set[str] | None:
    """返回 root 下 git 可见文件的绝对路径集合（含 untracked 非 ignored）。"""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        return None
    try:
        proc = subprocess.run(
            [
                "git", "-C", str(root_path), "ls-files", "-z",
                "--cached", "--others", "--exclude-standard",
            ],
            capture_output=True, timeout=20,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", "replace")
    return {
        os.path.normcase(str((root_path / rel).resolve()))
        for rel in raw.split("\0") if rel
    }


def is_git_ignored(path: str) -> bool:
    """path 是否被 git 忽略（用于显式点名忽略目录的覆盖逻辑）。"""
    p = Path(path).resolve()
    try:
        proc = subprocess.run(
            ["git", "-C", str(p.parent), "check-ignore", "-q", "--", p.name],
            capture_output=True, timeout=10,
        )
    except Exception:
        return False
    return proc.returncode == 0


def add_ignored_dir_files(paths: set[str], search_dir: Path,
                          explicit_dirs: Iterable[str]) -> None:
    """显式写进 pattern 的忽略目录：把其中文件加回可见集合。

    例如 .cili/tmp/*.txt 中 .cili 被 .gitignore 忽略，但用户明确点名了它。
    """
    for name in explicit_dirs:
        target = search_dir / name
        if not target.is_dir() or not is_git_ignored(str(target)):
            continue
        for p in target.rglob("*"):
            if p.is_file():
                paths.add(os.path.normcase(str(p.resolve())))


def pattern_dirs(pattern: str | None) -> set[str]:
    """提取 glob pattern 中显式写出的目录名（跳过 * / ** / 文件模式）。"""
    if not pattern:
        return set()
    return {
        part for part in Path(pattern).parts
        if part not in ("*", "**") and not any(ch in part for ch in "*?[")
    }