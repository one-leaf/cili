"""默认工作区迁移：workspace/ 根 → workspace/default/。

旧布局中 Default 工作区直接占用 workspace/ 根目录；新布局下默认工作区位于
workspace/default/，根目录只容纳各独立工作区子目录。本模块在
``ensure_default_workspace()`` 每次启动时调用，幂等：

1. 索引中不存在 directory 指向 ``<项目根>/workspace`` 的条目 → 直接返回
   （全新安装或已迁移）。
2. 存在旧根条目时执行迁移：
   - 若索引中另有 directory 指向 ``<项目根>/workspace/default`` 的条目：
     其 .cili 含真实数据则放弃迁移（两套会话/记忆无法合并）；仅自动创建的
     空骨架则删除该条目，由旧条目接管目录。
   - workspace/ 根下除 default/ 与受保护子树（已注册独立工作区）外的全部
     目录与文件——含 .git、.cili 等隐藏项——移动到 workspace/default/。
   - 全部移动成功后，旧条目的 directory 更新为 workspace/default；任何一项
     失败则尽力搬回已移动项，索引保持指向旧根，下次启动重试。
   - 兜底：索引仍指向旧根、但旧根下只剩 default/ 且 default/.cili 存在
     （上次索引更新失败）→ 直接修复索引。
"""

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from core import config as config_mod

logger = logging.getLogger(__name__)

# .cili/.git 最后移动：中途失败时旧根仍保有完整的会话与 Git 历史
_DATA_DIRS = (".cili", ".git")
_SKELETON_DIRS = {"sessions"}  # 自动创建的空 .cili 骨架只含空 sessions/
_LOCK_STALE_SECONDS = 600
_MERGE_SUFFIX_TMPL = ".from-root-{ts}"

_process_lock = threading.Lock()


def _canonical(path: str | Path) -> str:
    """resolve 符号链接/junction 后的绝对路径 + normcase（Windows 大小写不敏感）。"""
    return os.path.normcase(str(Path(path).resolve()))


def _is_within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent + os.sep)


def _is_skeleton(cili_dir: Path) -> bool:
    """判定 .cili 是否只是自动创建的空骨架（.cili/sessions/，无任何文件）。"""
    if not cili_dir.is_dir():
        return False
    for cur, _dirs, files in os.walk(cili_dir):
        if files:
            return False
        rel = Path(cur).relative_to(cili_dir)
        if len(rel.parts) >= 2 or (rel.parts and rel.parts[0] not in _SKELETON_DIRS):
            return False
    return True


def _acquire_file_lock(lock_path: Path) -> bool:
    """跨进程互斥：O_EXCL 抢锁，超过 10 分钟视为陈旧进程残留，接管。"""
    try:
        if lock_path.exists():
            if time.time() - lock_path.stat().st_mtime < _LOCK_STALE_SECONDS:
                return False
            lock_path.unlink()
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f)
        return True
    except FileExistsError:
        return False
    except OSError as e:
        logger.warning(f"[migration] 锁文件不可用，按无锁继续: {e}")
        return True


def _find_entry_by_directory(entries: list[dict], directory: str | Path) -> dict | None:
    target = _canonical(directory)
    for entry in entries:
        if entry.get("directory") and _canonical(entry["directory"]) == target:
            return entry
    return None


def _move_into(child: Path, dest: Path, ts: str, merged_ok: list[str]) -> Path | None:
    """把 child 移动到 dest，同名冲突不覆盖：目录递归合并，文件改名让位。

    返回实际落点（整体一次性移动，可回滚）；目录发生合并时返回 None（无法回滚）。
    """
    if not dest.exists():
        shutil.move(str(child), str(dest))
        return dest
    if child.is_dir():
        if dest.is_file():
            # 目标位置同名文件让位（改为合并目录）
            target = dest.parent / f"{dest.name}{_MERGE_SUFFIX_TMPL.format(ts=ts)}"
            shutil.move(str(dest), str(target))
        dest.mkdir(parents=True, exist_ok=True)
        merged = False
        for sub in list(child.iterdir()):
            merged = _move_into(sub, dest / sub.name, ts, merged_ok) is None or merged
        try:
            child.rmdir()
        except OSError as e:
            logger.warning(f"[migration] 源目录未能清空，保留原位: {child} ({e})")
            return None
        if merged:
            merged_ok.append(str(child))
        return None
    target = dest.parent / f"{dest.name}{_MERGE_SUFFIX_TMPL.format(ts=ts)}"
    n = 0
    while target.exists():
        n += 1
        target = dest.parent / f"{dest.name}{_MERGE_SUFFIX_TMPL.format(ts=ts)}-{n}"
    shutil.move(str(child), str(target))
    return target


def migrate_default_workspace() -> str | None:
    """执行旧布局迁移，返回迁移后的 Default 工作区 uuid；未触发迁移返回 None。

    见模块 docstring 的触发条件与移动规则。任何移动失败都会尽力回滚并保持
    索引指向旧根，保证旧路径仍可启动。
    """
    with _process_lock:
        ws_root = Path(config_mod.PROJECT_ROOT) / "workspace"
        default_dir = ws_root / "default"
        lock_path = Path(config_mod.DATA_DIR) / "workspace_migration.lock"

        if not _acquire_file_lock(lock_path):
            logger.info("[migration] 另一进程正在迁移默认工作区，本次跳过")
            return None

        try:
            entries = config_mod.load_workspaces_index()
            # 条件 1：旧根条目不存在 → 全新安装或已迁移，直接跳过
            legacy = _find_entry_by_directory(entries, ws_root)
            if not legacy:
                return None
            return _do_migrate(ws_root, default_dir, entries, legacy)
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def _do_migrate(
    ws_root: Path,
    default_dir: Path,
    entries: list[dict],
    legacy: dict[str, Any],
) -> str | None:
    legacy_uuid = legacy.get("uuid", "")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    # 兜底：文件已搬入 default/ 但上次索引更新失败（旧根下只剩 default/）→ 修索引即可
    if _is_moved_but_index_stale(ws_root, default_dir):
        logger.warning("[migration] 检测到文件已就位、索引未更新，直接修复索引")
        return _repoint_index(legacy, default_dir)

    # 目标位置已有工作区条目：.cili 无真实数据（缺失或空骨架）才允许接管
    occupant = _find_entry_by_directory(entries, default_dir)
    if occupant and occupant.get("uuid") != legacy_uuid:
        if not (default_dir / ".cili").exists() or _is_skeleton(default_dir / ".cili"):
            config_mod.remove_workspace_entry(occupant.get("uuid", ""))
            entries = config_mod.load_workspaces_index()
            logger.info(f"[migration] 移除自动创建的空 Default 条目: {occupant.get('uuid')}")
        else:
            logger.error(
                "[migration] workspace/default 已被含真实数据的工作区占用，"
                "无法自动合并；请先手动处理该工作区后再启动"
            )
            return None

    # 其他条目指向旧根本身 → 数据归属不明，中止交由用户处理
    root_canon = _canonical(ws_root)
    duplicates = [
        e.get("workspace_name", "")
        for e in entries
        if e.get("uuid") != legacy_uuid and e.get("directory") and _canonical(e["directory"]) == root_canon
    ]
    if duplicates:
        logger.error(f"[migration] 旧根目录被多个工作区条目占用（{duplicates}），迁移中止")
        return None

    # 受保护集合：其余工作区的目录；待移动项若是（或包含、或位于）受保护路径则保留原位
    protected = {
        _canonical(e["directory"])
        for e in entries
        if e.get("uuid") not in (None, "system", legacy_uuid) and e.get("directory")
    }
    default_canon = _canonical(default_dir)
    if default_canon in protected or any(
        _is_within(default_canon, p) or _is_within(p, default_canon) for p in protected
    ):
        logger.error("[migration] workspace/default 与独立工作区路径冲突，迁移中止")
        return None

    # 预检：目标 .git/.cili 冲突直接中止（Git 历史与会话数据无法合并）
    if (default_dir / ".git").exists() and (ws_root / ".git").exists():
        logger.error("[migration] workspace/default/.git 已存在，不能合并两段 Git 历史，迁移中止")
        return None
    target_cili = default_dir / ".cili"
    if target_cili.exists() and not _is_skeleton(target_cili):
        logger.error("[migration] workspace/default/.cili 已含数据，不能与旧根 .cili 合并，迁移中止")
        return None
    if target_cili.exists():
        shutil.rmtree(target_cili)

    if not ws_root.is_dir():
        logger.warning(f"[migration] 旧根目录不存在，仅更新索引: {ws_root}")
        return _repoint_index(legacy, default_dir)

    # 计算移动集合：default/ 本身与受保护子树原地保留，其余（含隐藏项）全部搬入
    move_items: list[Path] = []
    skipped: list[str] = []
    for child in sorted(ws_root.iterdir(), key=lambda p: (p.name in _DATA_DIRS, p.name)):
        if _canonical(child) == default_canon:
            continue
        c = _canonical(child)
        if any(p == c or _is_within(c, p) or _is_within(p, c) for p in protected):
            skipped.append(child.name)
            continue
        move_items.append(child)

    moved: list[tuple[Path, Path]] = []  # 可整体回滚的移动
    merged_ok: list[str] = []  # 同名合并过的目录，无法自动回滚
    failed: list[str] = []
    default_dir.mkdir(parents=True, exist_ok=True)
    for child in move_items:
        try:
            dest = default_dir / child.name
            actual = _move_into(child, dest, ts, merged_ok)
            if actual is not None:
                moved.append((child, actual))
        except OSError as e:
            logger.error(f"[migration] 移动失败: {child} → {default_dir / child.name} ({e})")
            failed.append(child.name)

    if failed:
        _rollback(moved, merged_ok)
        logger.error(f"[migration] 迁移未完成（失败项: {failed}），索引保持指向旧根，下次启动重试")
        return None

    (default_dir / ".cili" / "sessions").mkdir(parents=True, exist_ok=True)
    uuid = _repoint_index(legacy, default_dir)
    logger.info(
        f"[migration] Default workspace moved: {ws_root} -> {default_dir}"
        f" (moved {len(move_items)} entries, skipped {len(skipped)}, merged {len(merged_ok)})"
    )
    if skipped:
        logger.info(f"[migration] 独立工作区目录保留原位: {skipped}")
    return uuid


def _is_moved_but_index_stale(ws_root: Path, default_dir: Path) -> bool:
    """索引仍指向旧根，但旧根下只剩 default/ 且 default/.cili 存在。"""
    if not default_dir.is_dir() or not (default_dir / ".cili").exists():
        return False
    if not ws_root.is_dir():
        return False
    return all(child.name == default_dir.name for child in ws_root.iterdir())


def _repoint_index(legacy: dict[str, Any], default_dir: Path) -> str | None:
    legacy = dict(legacy)
    legacy["directory"] = str(default_dir)
    legacy["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not config_mod.save_workspace_config(legacy.get("uuid", ""), legacy):
        # 文件已在 default/ 下；下次启动按「索引待修复」兜底逻辑修复
        logger.error("[migration] 索引更新失败；文件已在 default/ 下，下次启动自动修复索引")
        return legacy.get("uuid")
    return legacy.get("uuid")


def _rollback(moved: list[tuple[Path, Path]], merged_ok: list[str]) -> None:
    """把整体移动过的项搬回旧根；递归合并过的目录无法还原，仅记录。"""
    for src, dest in reversed(moved):
        try:
            if dest.exists():
                shutil.move(str(dest), str(src))
        except OSError as e:
            logger.error(f"[migration] 回滚失败，请手动检查: {dest} → {src} ({e})")
    if merged_ok:
        logger.error(f"[migration] 以下目录与目标同名合并且无法自动回滚: {merged_ok}")
