"""MemoryStore + Journal — cili v3 记忆存储引擎。

纯文件操作、无 LLM 依赖、确定性可测。承担两部分职责：
- MemoryStore: entries/{type}/{slug}.md 条目读写、MEMORY.md 索引维护、老化/归档
- Journal:     journal.jsonl 摄入日志 + .cursor 游标（恰好一次消费的可靠载体）

命名约定：name 是全局唯一定位键（slug），跨类型不重复。store 按 name 原地替换。
版本控制：用 data/deps/git 内置 git（best-effort，不可用则静默跳过），commit 信息取真实 diff 摘要。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────
MEMORY_TYPES = ("fact", "preference", "skill", "reference")
SOURCES = ("session", "user", "web", "file", "python", "derived")
STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
STATUS_ARCHIVED = "archived"

# description 长度上限（memory_pipeline 也复用，勿单独改）
MAX_DESCRIPTION_LEN = 200

# 老化阈值（天）
STALE_AFTER_DAYS = 30
ARCHIVE_AFTER_DAYS = 90

# MEMORY.md 常驻注入上限（claude-code 验证过的双截断）
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25 * 1024

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_EPOCH = datetime(1970, 1, 1, 0, 0, 0)

_FM_ORDER = ("type", "name", "title", "description", "tags", "source", "refs",
             "created", "updated", "usage_count", "last_used", "status")

# 跨线程文件锁：多 agent 并发写同一 memory 目录时串行化（设计 §14.2 风险 6 兜底）
_STORE_LOCKS: dict[str, threading.Lock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _dir_lock(memory_dir: str) -> threading.Lock:
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(memory_dir)
        if lock is None:
            lock = threading.Lock()
            _STORE_LOCKS[memory_dir] = lock
        return lock


# ── 时间与格式化 ──────────────────────────────────────

def now_str() -> str:
    return datetime.now().strftime(TS_FORMAT)


def parse_ts(value: Any) -> datetime:
    """解析 frontmatter 时间戳；解析失败返回 epoch。"""
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value), TS_FORMAT)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return _EPOCH


def _fm_scalar(value: Any) -> str:
    """清理用于 frontmatter 双引号标量值：frontmatter 逐行解析无转义，须内联替换引号/换行。"""
    return str(value).replace('"', "'").replace("\r", " ").replace("\n", " ").strip()


def slugify(title: str) -> str:
    """标题 → slug：ASCII→kebab-case，中文→memory-{md5[:8]}（沿用 v2 算法，去掉扩展名）。"""
    title = (title or "").strip()
    if title.isascii():
        name = re.sub(r"[^a-z0-9\s-]", "", title.lower())
        name = re.sub(r"[\s-]+", "-", name).strip("-")
        if not name:
            name = "untitled"
    else:
        h = hashlib.md5(title.encode("utf-8")).hexdigest()[:8]
        name = f"memory-{h}"
    if len(name) > 96:
        h = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
        name = f"{name[:88]}-{h}"
    return name


_UUID_RE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_SKILL_UUID_RE = re.compile(r"^skill-[0-9a-f]{8}$")


def validate_name(name: str) -> None:
    """校验 slug：非空、路径安全、拒绝绝对路径与 UUID 样式的无意义名字。"""
    if not name:
        raise ValueError("name is required")
    if name in (".", "..") or "/" in name or "\\" in name or os.path.isabs(name):
        raise ValueError(f"Invalid name: {name!r}")
    if not name.isascii():
        raise ValueError(
            "name must be an ASCII kebab-case slug (e.g., 'rest-api-design'); "
            "for non-ASCII titles, omit 'name' and it will be derived from title"
        )
    if "." in name:
        raise ValueError(
            f"name must not contain dots (use hyphens): {name!r}"
        )
    if _UUID_RE.match(name) or _SKILL_UUID_RE.match(name):
        raise ValueError(
            f"name must be a meaningful kebab-case slug (e.g., 'python-async'), not a UUID: {name!r}"
        )


# ── frontmatter 序列化 / 解析 ─────────────────────────

def _serialize_frontmatter(fm: dict) -> list[str]:
    """条目 frontmatter 规范序列化（块状列表，标量双引号）。"""
    lines = ["---"]
    for key in _FM_ORDER:
        if key not in fm or fm[key] is None:
            continue
        value = fm[key]
        if key in ("tags", "refs"):
            if isinstance(value, str):
                value = [value]
            if not value:
                continue
            lines.append(f"{key}:")
            for item in value:
                lines.append(f'  - "{_fm_scalar(item)}"')
        elif key == "usage_count":
            lines.append(f"{key}: {int(value)}")
        else:
            lines.append(f'{key}: "{_fm_scalar(value)}"')
    lines.append("---")
    return lines


def _parse_frontmatter(content: str) -> dict:
    """逐行解析 frontmatter（标量、内联数组、块状列表），无 frontmatter 时返回 {}。"""
    if not content.startswith("---"):
        return {}
    end = content.find("---", 3)
    if end == -1:
        return {}
    result: dict = {}
    lines = content[3:end].splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        elif value.startswith("[") and value.endswith("]"):
            value = [t.strip().strip('"').strip("'") for t in value[1:-1].split(",") if t.strip()]
        elif value == "":
            items = []
            while i < len(lines):
                item_line = lines[i].strip()
                if item_line.startswith("- "):
                    item = item_line[2:].strip()
                    if item.startswith('"') and item.endswith('"'):
                        item = item[1:-1]
                    elif item.startswith("'") and item.endswith("'"):
                        item = item[1:-1]
                    items.append(item)
                    i += 1
                else:
                    break
            value = items
        result[key] = value
    if "usage_count" in result:
        try:
            result["usage_count"] = int(result["usage_count"])
        except (TypeError, ValueError):
            result["usage_count"] = 0
    return result


# ── 轻量 git（best-effort，用 data/deps/git 内置 git）──

def _find_git() -> str | None:
    """定位 git 可执行文件：内置 deps git 优先，其次系统 PATH。"""
    project_root = Path(__file__).resolve().parent.parent
    candidates = [
        project_root / "data" / "deps" / "git" / "cmd" / "git.exe",
        project_root / "data" / "deps" / "git" / "mingw64" / "bin" / "git.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("git")


def _git_cmd(memory_dir: str | Path, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    git = _find_git()
    if not git:
        raise FileNotFoundError("git not available")
    return subprocess.run(
        [git, *args],
        cwd=str(memory_dir),
        capture_output=True,
        text=True,
        # git 提交信息/日志为 UTF-8；Windows 默认 locale 是 GBK，需显式指定，否则读线程 UnicodeDecodeError
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _ensure_self_repo(memory_dir: str | Path) -> bool:
    """确保 memory 目录是独立的 git 仓库（而非父仓库的子目录）。

    若 memory 目录位于 cili 项目仓库（data/ 在其内）且 .git 缺失/损坏，
    git 会向上发现父仓库——此时必须重新 init，否则 add -A 会误提交整个项目。
    损坏的 .git 目录（如只有 objects/）先移为 .git.bak.<ts> 保留，再重建。
    """
    md = Path(memory_dir)
    try:
        probe = _git_cmd(md, ["rev-parse", "--show-toplevel"], timeout=10)
    except Exception:
        probe = None
    if probe is not None and probe.returncode == 0:
        top = os.path.normcase(os.path.abspath(probe.stdout.strip()))
        if top == os.path.normcase(str(md.resolve())):
            _git_cmd(md, ["config", "user.email", "cili@localhost"])
            _git_cmd(md, ["config", "user.name", "cili"])
            return True
    gitdir = md / ".git"
    if gitdir.exists():
        backup = md / f".git.bak.{int(time.time())}"
        try:
            os.replace(gitdir, backup)
        except OSError:
            # best-effort：备份 rename 失败可接受，下面直接重新 init 重建 .git
            pass
    result = _git_cmd(md, ["init", "-q"], timeout=30)
    if result.returncode != 0:
        return False
    _git_cmd(md, ["config", "user.email", "cili@localhost"])
    _git_cmd(md, ["config", "user.name", "cili"])
    return True


def _git_available(memory_dir: str | Path) -> bool:
    try:
        return _git_cmd(memory_dir, ["rev-parse", "--is-inside-work-tree"], timeout=10).returncode == 0
    except Exception:
        return False


def best_effort_commit(memory_dir: str | Path, subject: str) -> tuple[bool, str]:
    """在 memory 目录初始化/提交 git（尽力而为，失败静默跳过）。

    首次提交前 init + 本地 user 配置，避免全局配置缺失报错。
    提交信息 = 真实 diff 摘要（非 LLM 自述），满足设计 §8.1 审计要求。
    """
    md = Path(memory_dir)
    try:
        if not _ensure_self_repo(md):
            return False, "git init failed"
        add = _git_cmd(md, ["add", "-A"])
        if add.returncode != 0:
            return False, add.stderr.strip()[:200]
        stat = _git_cmd(md, ["diff", "--cached", "--stat", "-M"])
        diff_summary = stat.stdout.strip()
        if not diff_summary:
            return False, "no changes"
        message = f"{subject}\n\n{diff_summary}" if diff_summary else subject
        commit = _git_cmd(md, ["commit", "-m", message])
        if commit.returncode != 0:
            return False, (commit.stderr.strip() or commit.stdout.strip())[:200]
        return True, (commit.stdout.strip().splitlines()[-1] if commit.stdout.strip() else "committed")
    except Exception as e:
        return False, str(e)


def git_log_summary(memory_dir: str | Path, max_commits: int = 10) -> list[dict]:
    """读取 memory 仓库最近提交（审计用）。无 git / 未独立成仓库时返回 []。

    只读 memory 目录自身的仓库日志：先校验 --show-toplevel 就是 memory 目录，
    否则 git 会向上发现父仓库（如 cili 项目仓库）泄漏全局提交。
    """
    try:
        probe = _git_cmd(memory_dir, ["rev-parse", "--show-toplevel"], timeout=10)
        if probe.returncode != 0:
            return []
        top = os.path.normcase(os.path.abspath(probe.stdout.strip()))
        if top != os.path.normcase(str(Path(memory_dir).resolve())):
            return []
        result = _git_cmd(memory_dir, [
            "log", "--pretty=%h|%ct|%s", "-n", str(max_commits),
        ])
        if result.returncode != 0:
            return []
        out = []
        for line in result.stdout.strip().splitlines():
            if "|" not in line:
                continue
            short_hash, ts, *subject_parts = line.split("|")
            try:
                date = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError, OverflowError):
                date = ""
            out.append({"hash": short_hash, "date": date, "subject": "|".join(subject_parts)})
        return out
    except Exception:
        return []


# ── MemoryStore ──────────────────────────────────────

class MemoryStore:
    """条目存储 + MEMORY.md 索引 + 老化/归档。公共方法线程安全，变更原子写。"""

    def __init__(self, memory_dir: str | Path):
        self.memory_dir = Path(memory_dir).resolve()
        self.entries_dir = self.memory_dir / "entries"
        self.archive_dir = self.memory_dir / "archive"
        self.index_path = self.memory_dir / "MEMORY.md"
        self._lock = _dir_lock(str(self.memory_dir))

    # ── 内部定位 / 读写 ──

    def _entry_path(self, type_: str, name: str) -> Path:
        return self.entries_dir / type_ / f"{name}.md"

    def _archive_entry_path(self, type_: str, name: str) -> Path:
        return self.archive_dir / type_ / f"{name}.md"

    def _load_file(self, path: Path) -> tuple[dict, str]:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return {}, ""
        if content.startswith("---"):
            end = content.find("---", 3)
            body = content[end + 3:].lstrip("\n") if end != -1 else ""
        else:
            end = -1
            body = content
        fm = _parse_frontmatter(content)
        if fm and end != -1:
            fm["_body"] = body
        return fm, body

    def _save_file(self, path: Path, fm: dict, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(_serialize_frontmatter(fm))
        if body:
            content += "\n\n" + body
        if not content.endswith("\n"):
            content += "\n"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    def _locate(self, name: str) -> tuple[Path, dict, str] | None:
        """按 name 在 entries/ 与 archive/ 全类型下定位条目，返回 (path, fm, body)。"""
        validate_name(name)
        for base in (self.entries_dir, self.archive_dir):
            for type_ in MEMORY_TYPES:
                path = base / type_ / f"{name}.md"
                if path.is_file():
                    fm, body = self._load_file(path)
                    fm.setdefault("type", type_)
                    fm.setdefault("name", name)
                    return path, fm, body
        return None

    def _locate_in(self, name: str, base: Path) -> tuple[Path, dict, str] | None:
        validate_name(name)
        for type_ in MEMORY_TYPES:
            path = base / type_ / f"{name}.md"
            if path.is_file():
                fm, body = self._load_file(path)
                fm.setdefault("type", type_)
                fm.setdefault("name", name)
                return path, fm, body
        return None

    def _iter_entries(self, type_: str | None = None) -> list[dict]:
        """遍历 entries/ 下所有条目 frontmatter（附 _path/_body）。不包含 archive/。"""
        results: list[dict] = []
        types = [type_] if type_ else list(MEMORY_TYPES)
        for t in types:
            if t not in MEMORY_TYPES:
                raise ValueError(f"Unknown memory type: {t!r} (allowed: {', '.join(MEMORY_TYPES)})")
            tdir = self.entries_dir / t
            if not tdir.is_dir():
                continue
            for f in tdir.iterdir():
                if not f.name.endswith(".md") or f.name.endswith(".tmp"):
                    continue
                fm, _ = self._load_file(f)
                if not fm:
                    continue
                fm.setdefault("type", t)
                fm["_path"] = str(f)
                results.append(fm)
        return results

    def _iter_archived(self, type_: str | None = None) -> list[dict]:
        results: list[dict] = []
        types = [type_] if type_ else list(MEMORY_TYPES)
        for t in types:
            if t not in MEMORY_TYPES:
                continue
            tdir = self.archive_dir / t
            if not tdir.is_dir():
                continue
            for f in tdir.iterdir():
                if not f.name.endswith(".md") or f.name.endswith(".tmp"):
                    continue
                fm, _ = self._load_file(f)
                if not fm:
                    continue
                fm.setdefault("type", t)
                fm["_path"] = str(f)
                results.append(fm)
        return results

    @staticmethod
    def _cleanup_empty_dirs(path: Path, stop: Path) -> None:
        """自 path 父目录起向上删除空目录，直到 stop（不含）。"""
        current = path.parent
        while current != stop and stop in current.parents:
            try:
                if current.is_dir() and not any(current.iterdir()):
                    current.rmdir()
                else:
                    break
            except OSError:
                break
            current = current.parent

    def _public(self, fm: dict) -> dict:
        return {k: v for k, v in fm.items() if not k.startswith("_")}

    # ── store ──

    def store(self, *, type_: str, name: str | None = None, title: str = "",
              description: str = "", content: str = "", tags: list[str] | None = None,
              source: str = "user", refs: list[str] | None = None) -> dict:
        """新建或原地替换条目（按 name 定位）。name 缺省时由 title 派生。

        派生 name 与既有不同标题条目冲突时自动加 -2/-3 后缀，避免误覆盖。
        返回 {name, type, path, created, updated, replaced}。
        """
        if type_ not in MEMORY_TYPES:
            raise ValueError(f"Unknown memory type: {type_!r} (allowed: {', '.join(MEMORY_TYPES)})")
        if source not in SOURCES:
            raise ValueError(f"Unknown source: {source!r} (allowed: {', '.join(SOURCES)})")
        if len(description or "") > MAX_DESCRIPTION_LEN:
            raise ValueError(f"description must be {MAX_DESCRIPTION_LEN} characters or less")
        if not (title or "").strip() and not (content or "").strip():
            raise ValueError("title or content is required for store")

        name_param = (name or "").strip().lower()
        title = (title or "").strip() or name_param
        with self._lock:
            name, existing = self._resolve_store_target(type_, name_param, title)
            now = now_str()
            created = existing[1].get("created") or now if existing else now
            usage_count = int(existing[1].get("usage_count", 0)) if existing else 0
            last_used = existing[1].get("last_used") if existing else ""
            path = self._entry_path(type_, name)
            if existing and existing[0] != path:
                # 存在于 archive 且同类型 → 移回 entries（自动解除归档）
                os.replace(existing[0], path)
            fm = {
                "type": type_,
                "name": name,
                "title": title,
                "description": (description or "").strip(),
                "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
                "source": source,
                "refs": [str(r).strip() for r in (refs or []) if str(r).strip()],
                "created": created,
                "updated": now,
                "usage_count": usage_count,
                "last_used": last_used,
                "status": STATUS_ACTIVE,
            }
            self._save_file(path, fm, content or "")
            self._rebuild_index()
        return {"name": name, "type": type_, "path": str(path),
                "created": created, "updated": now, "replaced": existing is not None}

    def _resolve_store_target(self, type_: str, name_param: str, title: str) -> tuple[str, tuple[Path, dict, str] | None]:
        """解析 store 落点：(最终 name, 既有条目 or None)。"""
        derived = not name_param
        name = name_param or slugify(title)
        existing = self._locate(name)
        if derived and existing:
            existing_title = str(existing[1].get("title", "")).strip()
            if existing_title and existing_title != title:
                n = 2
                while True:
                    candidate = f"{name}-{n}"
                    ex = self._locate(candidate)
                    if ex is None:
                        name = candidate
                        break
                    n += 1
                existing = self._locate(name)
        if existing and existing[1].get("type") and existing[1]["type"] != type_:
            raise ValueError(
                f"name '{name}' is already used by a {existing[1]['type']} entry; "
                "name must be globally unique, pick a different name"
            )
        return name, existing

    # ── find / read / update / delete / list / stat ──

    def find(self, query: str, type_: str | None = None, status: str | None = None,
             limit: int = 20) -> list[dict]:
        """描述驱动召回：只匹配 frontmatter（name/title/description/tags/refs），不读正文。

        返回按 usage_count 倒序（active/stale，不含 archived）。不递增 usage（真实使用以 read 为准）。
        """
        query = (query or "").strip().lower()
        if not query:
            raise ValueError("query is required for find")
        with self._lock:
            entries = self._iter_entries(type_)
        matched = []
        for fm in entries:
            s = fm.get("status", STATUS_ACTIVE)
            if s == STATUS_ARCHIVED:
                continue
            if status is not None and s != status:
                continue
            hay = " ".join([
                str(fm.get("name", "")), str(fm.get("title", "")),
                str(fm.get("description", "")),
                " ".join(str(t) for t in (fm.get("tags") or [])),
                " ".join(str(r) for r in (fm.get("refs") or [])),
            ]).lower()
            if query in hay:
                matched.append(fm)
        matched.sort(key=lambda e: (e.get("usage_count", 0), str(e.get("updated", ""))), reverse=True)
        return [self._public(fm) for fm in matched[:limit]]

    def read(self, name: str) -> tuple[dict, str]:
        """读条目全文，递增 usage_count / last_used。返回 (fm, body)。"""
        with self._lock:
            found = self._locate(name)
            if not found:
                raise ValueError(f"no memory entry named '{name}'")
            path, fm, body = found
            fm["usage_count"] = int(fm.get("usage_count", 0)) + 1
            fm["last_used"] = now_str()
            fm.pop("_body", None)
            self._save_file(path, fm, body)
        return self._public(fm), body

    def peek(self, name: str) -> tuple[dict, str]:
        """只读条目全文，不递增 usage（供 UI / 迁移等非召回场景）。返回 (fm, body)。"""
        with self._lock:
            found = self._locate(name)
            if not found:
                raise ValueError(f"no memory entry named '{name}'")
            _path, fm, body = found
            fm.pop("_body", None)
            return self._public(fm), body

    def update(self, name: str, *, title: str | None = None, description: str | None = None,
               content: str | None = None, tags: list[str] | None = None,
               refs: list[str] | None = None, source: str | None = None,
               status: str | None = None) -> dict:
        """原地更新条目：preserve created/usage_count/last_used；refs 累积去重。"""
        if source is not None and source not in SOURCES:
            raise ValueError(f"Unknown source: {source!r}")
        if status is not None and status not in (STATUS_ACTIVE, STATUS_STALE):
            raise ValueError(f"Invalid status: {status!r} (allowed: active, stale)")
        with self._lock:
            found = self._locate(name)
            if not found:
                raise ValueError(f"no memory entry named '{name}'")
            path, fm, body = found
            if content is not None:
                body = content
            if title is not None:
                fm["title"] = (title or "").strip() or name
            if description is not None:
                if len(description) > MAX_DESCRIPTION_LEN:
                    raise ValueError(f"description must be {MAX_DESCRIPTION_LEN} characters or less")
                fm["description"] = (description or "").strip()
            if tags is not None:
                fm["tags"] = [str(t).strip() for t in tags if str(t).strip()]
            if refs is not None:
                merged = list(fm.get("refs") or [])
                for r in refs:
                    r = str(r).strip()
                    if r and r not in merged:
                        merged.append(r)
                fm["refs"] = merged
            if source is not None:
                fm["source"] = source
            if status is not None:
                fm["status"] = status
            fm["updated"] = now_str()
            fm.pop("_body", None)
            self._save_file(path, fm, body)
            self._rebuild_index()
        return {"name": name, "updated": fm["updated"], "path": str(path)}

    def delete(self, name: str) -> dict:
        with self._lock:
            found = self._locate(name)
            if not found:
                raise ValueError(f"no memory entry named '{name}'")
            path, fm, _ = found
            path.unlink()
            stop = self.entries_dir if self.entries_dir in path.parents else self.archive_dir
            self._cleanup_empty_dirs(path, stop)
            self._rebuild_index()
        return {"name": name, "type": fm.get("type"), "removed": True}

    def archive(self, name: str) -> dict:
        """移入 archive/（从索引移除，不再参与 find）。"""
        with self._lock:
            found = self._locate_in(name, self.entries_dir)
            if not found:
                raise ValueError(f"no active memory entry named '{name}'")
            path, fm, body = found
            dest = self._archive_entry_path(fm.get("type") or "fact", name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, dest)
            fm["status"] = STATUS_ARCHIVED
            fm["updated"] = now_str()
            fm.pop("_body", None)
            self._save_file(dest, fm, body)
            self._cleanup_empty_dirs(path, self.entries_dir)
            self._rebuild_index()
        return {"name": name, "archived": True}

    def restore(self, name: str, type_: str | None = None) -> dict:
        """从 archive/ 恢复回 entries/（status 置 active）。"""
        with self._lock:
            found = self._locate_in(name, self.archive_dir)
            if not found:
                raise ValueError(f"no archived memory entry named '{name}'")
            path, fm, body = found
            fm_type = type_ or fm.get("type") or "fact"
            if fm_type not in MEMORY_TYPES:
                raise ValueError(f"Unknown memory type: {fm_type!r}")
            dest = self._entry_path(fm_type, name)
            if dest.exists():
                raise ValueError(f"active entry '{name}' already exists, can't restore")
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, dest)
            fm["type"] = fm_type
            fm["status"] = STATUS_ACTIVE
            fm["updated"] = now_str()
            fm.pop("_body", None)
            self._save_file(dest, fm, body)
            self._cleanup_empty_dirs(path, self.archive_dir)
            self._rebuild_index()
        return {"name": name, "restored": True}

    def list(self, type_: str | None = None, status: str | None = None) -> list[dict]:
        """列条目（默认 active+stale，按 updated 倒序；status 可过滤含 archived）。"""
        with self._lock:
            if status == STATUS_ARCHIVED:
                entries = self._iter_archived(type_)
            else:
                entries = self._iter_entries(type_)
        result = []
        for fm in entries:
            s = fm.get("status", STATUS_ACTIVE)
            if status is not None and s != status:
                continue
            if status is None and s == STATUS_ARCHIVED:
                continue
            result.append(self._public(fm))
        result.sort(key=lambda e: str(e.get("updated", "")), reverse=True)
        return result

    def stat(self) -> dict:
        with self._lock:
            entries = self._iter_entries()
            archived = self._iter_archived()
        by_type = {t: 0 for t in MEMORY_TYPES}
        for fm in entries:
            by_type[fm.get("type", "fact")] += 1
        stale = sum(1 for fm in entries if self.is_stale(fm))
        index_lines = index_bytes = 0
        if self.index_path.is_file():
            index_text = self.index_path.read_text(encoding="utf-8")
            index_lines = len(index_text.splitlines())
            index_bytes = len(index_text.encode("utf-8"))
        return {
            "total": len(entries),
            "archived": len(archived),
            "stale": stale,
            "by_type": by_type,
            "index_lines": index_lines,
            "index_bytes": index_bytes,
        }

    # ── 老化 / 归档 ──

    def is_stale(self, fm: dict) -> bool:
        return (datetime.now() - parse_ts(fm.get("updated"))).days > STALE_AFTER_DAYS

    def stale_note(self, fm: dict) -> str | None:
        """>30 天未更新 → "请验证" 时效标注（仿 claude-code memoryAge）。"""
        if self.is_stale(fm):
            return "⚠ 此为历史观察（>30 天未更新），使用前请对照当前代码/事实验证"
        return None

    def archive_stale(self) -> list[str]:
        """自动归档：updated >90 天 且 usage_count=0 → 移 archive/（cron 每日调用）。"""
        archived: list[str] = []
        with self._lock:
            for fm in self._iter_entries():
                if fm.get("status") == STATUS_ARCHIVED:
                    continue
                age_days = (datetime.now() - parse_ts(fm.get("updated"))).days
                if age_days > ARCHIVE_AFTER_DAYS and int(fm.get("usage_count", 0)) == 0:
                    path = Path(fm["_path"])
                    body = fm.get("_body", "")
                    dest = self._archive_entry_path(fm.get("type") or "fact", fm["name"])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(path, dest)
                    fm["status"] = STATUS_ARCHIVED
                    fm["updated"] = now_str()
                    fm.pop("_body", None)
                    self._save_file(dest, fm, body)
                    self._cleanup_empty_dirs(path, self.entries_dir)
                    archived.append(fm["name"])
            if archived:
                self._rebuild_index()
        return archived

    # ── MEMORY.md 索引 ──

    def _rebuild_index(self) -> None:
        """按 updated 倒序重建索引；200 行 / 25KB 双截断（claude-code 验证过的控制手段）。"""
        entries = [fm for fm in self._iter_entries() if fm.get("status") != STATUS_ARCHIVED]
        entries.sort(key=lambda e: str(e.get("updated", "")), reverse=True)
        lines = [
            "# Memory Index",
            "<!-- cili 记忆索引：MemoryStore 自动维护，请勿手改 -->",
            "<!-- 常驻注入预算：≤200 行 / 25KB，按 updated 倒序 -->",
            "",
        ]
        for fm in entries:
            title = _fm_scalar(fm.get("title") or fm.get("name"))
            desc = _fm_scalar(fm.get("description") or "")
            lines.append(f"- [{title}](entries/{fm.get('type')}/{fm.get('name')}.md) — {desc}")
        if len(lines) > INDEX_MAX_LINES:
            lines = lines[:INDEX_MAX_LINES]
        while len("\n".join(lines).encode("utf-8")) > INDEX_MAX_BYTES and len(lines) > 5:
            lines.pop()
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写同目录临时文件再 os.replace，避免崩溃留下半写索引。
        # 与 Journal._write_cursor 的 tmp + replace 模式一致。
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=self.index_path.parent, delete=False, suffix=".tmp"
        ) as tmp:
            tmp.write("\n".join(lines) + "\n")
            tmp_path = tmp.name
        os.replace(tmp_path, self.index_path)


# ── Journal（摄入日志 + 游标）─────────────────────────

class Journal:
    """journal.jsonl（append-only）+ .cursor（已整合游标）——恰好一次消费。

    追加端去重：同 key（消息 id）只写一次；消费端去重：只读 cursor 之后的
    未整合记录，处理成功后单调推进 cursor。崩溃不推 cursor 即可重跑。
    """

    def __init__(self, memory_dir: str | Path):
        self.memory_dir = Path(memory_dir).resolve()
        self.journal_path = self.memory_dir / "journal.jsonl"
        self.cursor_path = self.memory_dir / ".cursor"
        self._lock = _dir_lock(str(self.memory_dir))

    # ── 游标 ──

    def cursor(self) -> int:
        try:
            return int(self.cursor_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return 0

    def _write_cursor(self, value: int) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cursor_path.with_name(".cursor.tmp")
        tmp.write_text(str(value), encoding="utf-8")
        os.replace(tmp, self.cursor_path)

    def advance(self, to_cursor: int) -> int:
        """单调推进游标（不后退）。返回新游标。"""
        with self._lock:
            current = self.cursor()
            target = max(current, int(to_cursor))
            if target != current:
                self._write_cursor(target)
            return target

    # ── 追加 ──

    def append(self, *, key: str, session_key: str = "", type_guess: str = "",
               title: str = "", description: str = "", content: str = "",
               tags: list[str] | None = None, refs: list[str] | None = None,
               source: str = "session", integrated: bool = False,
               raw: bool = False, ts: str | None = None) -> int:
        """追加一条摄入记录，返回其 cursor id。同 key 已存在则返回既有 cursor（去重）。"""
        if not key:
            raise ValueError("key is required for journal append")
        with self._lock:
            records = self._read_records()
            for record in records:
                if record.get("key") == key:
                    return int(record.get("cursor", 0))
            cursor = int(records[-1].get("cursor", 0)) + 1 if records else 1
            record = {
                "cursor": cursor,
                "key": key,
                "ts": ts or now_str(),
                "session_key": session_key,
                "type_guess": type_guess,
                "title": title,
                "description": description,
                "content": content,
                "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
                "refs": [str(r).strip() for r in (refs or []) if str(r).strip()],
                "source": source,
                "integrated": integrated,
                "raw": raw,
            }
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            return cursor

    # ── 读取 / 消费 ──

    def _read_records(self) -> list[dict]:
        if not self.journal_path.is_file():
            return []
        records: list[dict] = []
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(f"[memory] 损坏 journal 行（跳过单行）: {self.journal_path}")
                        continue
        except OSError:
            return []
        return records

    def read_pending(self, limit: int = 20) -> list[dict]:
        """游标之后、未整合（integrated!=true）的记录，按 cursor 升序。"""
        with self._lock:
            cur = self.cursor()
            records = self._read_records()
        return [r for r in records if int(r.get("cursor", 0)) > cur and not r.get("integrated")][:limit]

    def pending_count(self) -> int:
        cur = self.cursor()
        return sum(1 for r in self._read_records()
                   if int(r.get("cursor", 0)) > cur and not r.get("integrated"))

    def compact(self, keep: int = 500) -> int:
        """删除已整合且超预算的旧记录，journal 保底不膨胀（仿 nanobot compact_history）。"""
        with self._lock:
            cur = self.cursor()
            records = self._read_records()
            pending = [r for r in records if int(r.get("cursor", 0)) > cur]
            processed = [r for r in records if int(r.get("cursor", 0)) <= cur]
            keep_records = sorted(pending + processed[-keep:], key=lambda r: int(r.get("cursor", 0)))
            if len(keep_records) == len(records):
                return 0
            tmp = self.journal_path.with_name("journal.jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for record in keep_records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            os.replace(tmp, self.journal_path)
            return len(records) - len(keep_records)
