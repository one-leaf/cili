"""Git-visible file helper：让 glob/grep 尊重项目的 .gitignore。

非 git 目录/未安装 git 时返回 None，调用方回退到原有文件系统遍历。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable


def _ignored_in_own_repo(path: Path) -> bool:
    """path 是否被『管辖它的那个仓库』忽略。

    从 path 自身做仓库发现再 check-ignore；直接查 ``path.parent`` 会在嵌套
    仓库（如独立成仓的工作区）里误用外层仓库的规则。
    """
    try:
        prefix_proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-prefix"],
            capture_output=True, timeout=10,
        )
        if prefix_proc.returncode != 0:
            return False
        rel = prefix_proc.stdout.decode("utf-8", "replace").strip().rstrip("/")
        if not rel:
            return False  # 仓库根本身不会被自己忽略
        proc = subprocess.run(
            ["git", "-C", str(path), "check-ignore", "-q", "--", rel],
            capture_output=True, timeout=10,
        )
    except Exception:
        return False
    return proc.returncode == 0


def git_visible_paths(root: str) -> set[str] | None:
    """返回 root 下 git 可见文件的绝对路径集合（含 untracked 非 ignored）。"""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        return None
    # 搜索根自身被忽略时 ls-files 必然为空，调用方会把整个目录判成没有文件
    # （如 path=.cili/tmp，或 pattern 点名了忽略目录）；此时回退文件系统遍历。
    if _ignored_in_own_repo(root_path):
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
                          explicit_paths: Iterable[str]) -> None:
    """显式写进 pattern 的忽略目录：把其中文件加回可见集合。

    explicit_paths 是相对 search_dir 的目录路径（如 ``.cili/tmp``）；被忽略的
    可能是其中某一层父目录，所以按完整路径判断，而不是逐段直接子目录。
    """
    for rel in explicit_paths:
        target = search_dir / rel
        if not target.is_dir() or not _ignored_in_own_repo(target.resolve()):
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


def literal_dir_prefix(pattern: str | None) -> str | None:
    """提取 pattern 中最长的字面目录前缀（.cili/tmp/probe/*.md → .cili/tmp/probe）。"""
    if not pattern:
        return None
    literal: list[str] = []
    wildcard = False
    for part in Path(pattern).parts:
        if part in ("*", "**") or any(ch in part for ch in "*?["):
            wildcard = True
            break
        literal.append(part)
    if not wildcard and literal:
        literal = literal[:-1]  # 没有通配符时末段是文件名
    if not literal:
        return None
    rel = Path(*literal)
    return None if rel.is_absolute() else str(rel)