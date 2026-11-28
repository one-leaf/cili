"""`.cili` 布局迁移：把所有工作区迁到 runtime/cache/config 新布局。

迁移是幂等的：成功后写 `runtime/.layout-migrated`；旧文件先备份再移动。
本模块只做本地迁移；共享 Git marker/lock 与启动接入后续接入。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from core.fs_utils import atomic_write_json

logger = logging.getLogger(__name__)

LAYOUT_VERSION = 4
SKIP_ENV = "CILI_SKIP_LAYOUT_MIGRATION"
_RESERVED_SESSION_FILES = {
    "messages.jsonl", "index.json", "meta.json", "goal.json",
    "approval_audit.jsonl", "index.json.legacy",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _marker_path(data_dir: Path) -> Path:
    return data_dir / "runtime" / ".layout-migrated"


def _read_version(data_dir: Path) -> int:
    try:
        data = json.loads(_marker_path(data_dir).read_text(encoding="utf-8"))
        return int(data.get("version") or 0)
    except (OSError, ValueError):
        return 0


def _write_marker(data_dir: Path) -> None:
    atomic_write_json(_marker_path(data_dir), {
        "version": LAYOUT_VERSION,
        "migrated_at": _now(),
    })


def write_layout_marker(data_dir: str | Path) -> None:
    """供新建工作区直接写 marker，避免再走迁移。"""
    _write_marker(Path(data_dir))


def _write_shared_marker(data_dir: Path) -> None:
    try:
        from core.node_identity import get_node_id
        migrated_by = get_node_id()
    except Exception:
        migrated_by = ""
    atomic_write_json(data_dir / "config" / "layout.json", {
        "version": LAYOUT_VERSION,
        "migrated_at": _now(),
        "migrated_by_node": migrated_by,
    })


def _backup(data_dir: Path) -> Path:
    from core.config import DATA_DIR

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", data_dir.as_posix())[-40:]
    dst = DATA_DIR / "backup" / f"layout-migration-{stamp}-{key}-{secrets.token_hex(3)}"
    shutil.copytree(data_dir, dst, dirs_exist_ok=True)
    return dst


def _move(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if dst.exists():
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            shutil.rmtree(src, ignore_errors=True)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def _move_dir(src: Path, dst: Path) -> None:
    if src.is_dir():
        _move(src, dst)


def _migrate_workflows(data_dir: Path) -> None:
    root = data_dir / "workflows"
    if not root.is_dir():
        return
    for wf in root.iterdir():
        if not wf.is_dir() or wf.name.startswith("."):
            continue
        runtime = data_dir / "runtime" / "workflows" / wf.name
        _move(wf / "state.json", runtime / "state.json")
        _move_dir(wf / "runs", runtime / "runs")


def _migrate_cron(data_dir: Path) -> None:
    root = data_dir / "cron"
    if not root.is_dir():
        return
    runtime = data_dir / "runtime" / "cron"
    _move(root / ".migrated.json", runtime / ".migrated.json")
    for job in root.iterdir():
        if job.is_dir() and not job.name.startswith("."):
            _move(job / "state.json", runtime / job.name / "state.json")


def _migrate_tools(data_dir: Path) -> None:
    todo = data_dir / "tools" / "todo"
    if todo.is_dir():
        for f in todo.glob("*.json"):
            _move(f, data_dir / "runtime" / "todo" / f.name)
    loop = data_dir / "tools" / "loop"
    if loop.is_dir():
        shutil.rmtree(loop, ignore_errors=True)
    _move_dir(todo, data_dir / "runtime" / "todo")


def _migrate_memory(data_dir: Path) -> None:
    memory = data_dir / "memory"
    if not memory.is_dir():
        return
    cache = data_dir / "cache" / "memory"
    for name in ("MEMORY.md", "summary.md"):
        _move(memory / name, cache / name)

    journal = memory / "journal.jsonl"
    if journal.is_file():
        groups: dict[str, list[dict]] = {}
        for line in journal.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            key = str(rec.get("session_key") or "_orphan")
            groups.setdefault(key, []).append(rec)
        for key, records in groups.items():
            path = data_dir / "runtime" / "memory" / "sessions" / key / "journal.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        journal.rename(journal.with_suffix(".jsonl.migrated"))

    extract = memory / ".extract"
    if extract.is_dir():
        for f in extract.glob("*.json"):
            _move(f, data_dir / "runtime" / "memory" / "sessions" / f.stem / "pointer.json")
    cursor = memory / ".cursor"
    if cursor.is_file():
        _move(cursor, data_dir / "runtime" / "memory" / "sessions" / "_orphan" / ".cursor")

    git_dir = memory / ".git"
    if git_dir.is_dir():
        shutil.rmtree(git_dir, ignore_errors=True)
    for f in memory.glob("tmp*.tmp"):
        f.unlink(missing_ok=True)


def _migrate_approval_rules(data_dir: Path) -> None:
    old = data_dir / "approval_rules.json"
    if not old.is_file():
        return
    try:
        data = json.loads(old.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    target = data_dir / "config" / "approval_rules"
    target.mkdir(parents=True, exist_ok=True)
    for rule in data.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rid = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(rule.get("id") or secrets.token_hex(4)))
        atomic_write_json(target / f"{rid}.json", {"version": 1, "rule": rule})
    atomic_write_json(
        target / "_constraints.json",
        {"version": 1, "constraints": data.get("constraints") or []},
    )
    old.rename(old.with_suffix(".json.migrated"))


def _migrate_sensitive_words(data_dir: Path) -> None:
    old = data_dir / "sensitive_words.json"
    if not old.is_file():
        return
    try:
        data = json.loads(old.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    target = data_dir / "config" / "sensitive_words"
    rules_dir = target / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target / "settings.json", {
        "enabled": bool(data.get("enabled", False)),
        "fixed": data.get("fixed") or {},
    })
    import hashlib
    for rule in data.get("rules") or []:
        if not isinstance(rule, dict) or not rule.get("word"):
            continue
        name = hashlib.sha1(str(rule["word"]).encode("utf-8")).hexdigest()[:16]
        atomic_write_json(rules_dir / f"{name}.json", rule)
    old.rename(old.with_suffix(".json.migrated"))


def _migrate_courseware(data_dir: Path) -> None:
    workspace_root = data_dir.parent
    new_dir = workspace_root / "courseware" / "quiz-results"
    for old in (data_dir / "courseware" / "quiz-results.jsonl",
                workspace_root / "courseware" / "quiz-results.jsonl"):
        if old.is_file():
            new_dir.mkdir(parents=True, exist_ok=True)
            dst = new_dir / "legacy.jsonl"
            if dst.exists():
                dst.write_text(dst.read_text(encoding="utf-8") + old.read_text(encoding="utf-8"),
                               encoding="utf-8")
                old.unlink(missing_ok=True)
            else:
                _move(old, dst)
    for directory in (workspace_root, workspace_root / "学习资料"):
        for name in ("成绩记录.html", "错题本.md"):
            old = directory / name
            if old.is_file():
                _move(old, directory / f"{Path(name).stem}-legacy{old.suffix}")


def _migrate_sessions(data_dir: Path) -> None:
    sessions = data_dir / "sessions"
    if not sessions.is_dir():
        return
    cache = data_dir / "cache" / "tool_output"
    for session_dir in sessions.iterdir():
        if not session_dir.is_dir():
            continue
        sid = session_dir.name
        target = cache / sid
        for f in list(session_dir.iterdir()):
            if f.is_file() and f.name not in _RESERVED_SESSION_FILES and f.suffix in (".txt", ".json"):
                _move(f, target / f.name)
        _move_dir(session_dir / "attachments", target / "attachments")
        for exec_dir in list(session_dir.glob("exec_*")):
            if not exec_dir.is_dir():
                continue
            etarget = cache / exec_dir.name.removeprefix("exec_")
            for f in list(exec_dir.iterdir()):
                if f.is_file() and f.name not in {"index.json"} and f.suffix in (".txt", ".json"):
                    _move(f, etarget / f.name)
            _move_dir(exec_dir / "attachments", etarget / "attachments")
        for legacy in session_dir.glob("index.json.legacy"):
            legacy.unlink(missing_ok=True)
        for goal in session_dir.glob("goal.json"):
            goal.unlink(missing_ok=True)


def _migrate_downloads(data_dir: Path) -> None:
    old = data_dir / "tmp" / "downloads"
    if old.is_dir():
        dst = data_dir.parent / "downloads"
        dst.mkdir(parents=True, exist_ok=True)
        for f in list(old.iterdir()):
            _move(f, dst / f.name)
        shutil.rmtree(old, ignore_errors=True)


def migrate_data_dir(data_dir: str | Path) -> bool:
    """迁移单个工作区数据目录；已迁移返回 False。"""
    data_dir = Path(data_dir)
    if not data_dir.is_dir() or _read_version(data_dir) >= LAYOUT_VERSION:
        return False
    backup = _backup(data_dir)
    logger.info("[layout-migration] backup %s -> %s", data_dir, backup)
    _migrate_workflows(data_dir)
    _migrate_cron(data_dir)
    _migrate_tools(data_dir)
    _migrate_memory(data_dir)
    _migrate_approval_rules(data_dir)
    _migrate_sensitive_words(data_dir)
    _migrate_courseware(data_dir)
    _migrate_sessions(data_dir)
    _migrate_downloads(data_dir)
    _write_marker(data_dir)
    _write_shared_marker(data_dir)
    return True


def _data_dirs() -> list[Path]:
    from core.config import (
        SYSTEM_DATA_DIR,
        get_all_workspace_uuids,
        get_workspace_data_dir,
        workspaces_root,
    )

    dirs: list[Path] = [SYSTEM_DATA_DIR]
    try:
        for uuid in get_all_workspace_uuids():
            dirs.append(Path(get_workspace_data_dir(uuid)))
    except Exception:
        logger.warning("[layout-migration] 枚举工作区失败", exc_info=True)
    try:
        for child in workspaces_root().iterdir():
            if child.is_dir() and (child / ".cili").is_dir():
                dirs.append(child / ".cili")
    except OSError:
        pass
    seen: set[str] = set()
    unique: list[Path] = []
    for d in dirs:
        key = os.path.normcase(str(d.resolve()))
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def ensure_layout_migrated() -> list[Path]:
    """迁移所有工作区 + System；返回实际迁移的目录。"""
    if os.environ.get(SKIP_ENV) == "1":
        logger.warning("[layout-migration] %s=1，跳过", SKIP_ENV)
        return []
    migrated: list[Path] = []
    for data_dir in _data_dirs():
        try:
            if migrate_data_dir(data_dir):
                migrated.append(data_dir)
        except Exception:
            logger.error("[layout-migration] 迁移失败: %s", data_dir, exc_info=True)
            raise
    return migrated
