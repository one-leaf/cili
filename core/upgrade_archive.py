"""升级包文件树操作：内容感知复制、残余清理、回滚恢复。

供 updater（升级/回滚）与测试共用；不含网络与版本检查逻辑。
"""

from __future__ import annotations

import logging
import os
import shutil

logger = logging.getLogger(__name__)


def _files_equal(a: str, b: str) -> bool:
    """两文件内容是否完全一致（先比大小，再逐块比对；读取失败视为不同）。"""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca = fa.read(1024 * 1024)
                cb = fb.read(1024 * 1024)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


def _copy_tree(src: str, dst: str, exclude_dirs: set[str],
               changes: dict | None = None, root: str | None = None) -> None:
    """递归复制文件树，跳过 exclude_dirs 中的顶层目录。

    内容未变的文件跳过写入（与 scripts/upgrade.ps1 的 Copy-WithLog 检查逻辑一致），
    避免改写未变更文件的 mtime，也减少对运行中文件的无谓写入。

    changes 传入 {"added": [], "updated": []} 时收集实际写入的文件（相对 root
    的路径，root 缺省取首次调用的 dst）；None 时不收集（备份/回滚路径）。
    """
    if root is None:
        root = dst
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if item in exclude_dirs:
            continue
        if os.path.isdir(s):
            os.makedirs(d, exist_ok=True)
            _copy_tree(s, d, exclude_dirs, changes, root)
        elif os.path.exists(d) and _files_equal(s, d):
            continue
        else:
            if changes is not None:
                key = "updated" if os.path.exists(d) else "added"
                changes[key].append(os.path.relpath(d, root).replace("\\", "/"))
            shutil.copy2(s, d)


def _prune_dir(dst_dir: str, src_dir: str, removed: list | None = None,
               root: str | None = None) -> None:
    """递归删除 dst_dir 中 src_dir 不存在的内容（共享代码目录内部的清理）。

    removed 传入列表时收集被删除的条目（相对 root 的路径，root 缺省取首次
    调用的 dst_dir）；None 时不收集。
    """
    if root is None:
        root = dst_dir
    for item in os.listdir(dst_dir):
        s = os.path.join(dst_dir, item)
        if not os.path.exists(os.path.join(src_dir, item)):
            if os.path.isdir(s) and not os.path.islink(s):
                shutil.rmtree(s)
            else:
                os.remove(s)
            if removed is not None:
                removed.append(os.path.relpath(s, root).replace("\\", "/"))
        elif os.path.isdir(s) and not os.path.islink(s):
            _prune_dir(s, os.path.join(src_dir, item), removed, root)


def _prune_stale_files(dst: str, src: str, exclude_dirs: set[str],
                       removed: list | None = None) -> None:
    """删除升级前存在、但新版本代码包已移除的残余文件/目录。

    覆盖语义只处理"新增/覆盖"，不处理"删除"，导致旧版曾发布、新版已移除的
    文件（以及 __pycache__ 等生成物）成为残余；本函数补齐删除语义。

    仅清理新版本仍保留的代码目录内部（如 core/cron.d/ 下被移除的配置）；
    顶层本地目录（.claude/、reference/、.pytest_cache/、__pycache__/ 等不在
    代码包中的目录）与本地顶层文件整体保留，避免误删用户本地资源。

    removed 传入列表时收集被删除文件的相对路径（相对 dst）。
    """
    top_src_dirs = set()
    if os.path.isdir(src):
        for name in os.listdir(src):
            if os.path.isdir(os.path.join(src, name)):
                top_src_dirs.add(name)
    for item in os.listdir(dst):
        if item in exclude_dirs:
            continue
        abs_item = os.path.join(dst, item)
        if os.path.isdir(abs_item) and not os.path.islink(abs_item):
            # 本地目录（新代码包中不存在）整体保留，不进入清理
            if item not in top_src_dirs:
                continue
            _prune_dir(abs_item, os.path.join(src, item), removed, dst)
        # 顶层文件：新版本仍在的文件已由覆盖阶段处理；本地独有文件一律保留
