"""v2 -> v3 memory format one-time migration script (design §10).

Usage:
    python scripts/migrate_memory.py            # migrate all workspaces
    python scripts/migrate_memory.py --dry-run  # report only, write nothing

Mapping:
    memory/knowledge/{topic}/{date}/*.md  ->  entries/fact/{slug}.md   (tags include topic; time -> created/updated)
    memory/skills/{name}/skill.md         ->  entries/skill/{slug}.md
    data/agents/{uuid}/user-profile.md    ->  entries/preference/user-profile.md

Output: new entries/ + MEMORY.md index + first git commit. The old memory/
directory is moved intact to memory-legacy/ (kept for manual review).
Idempotent and re-runnable: workspaces already on v3 are skipped.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import AGENTS_DIR
from core.memory_store import (
    MemoryStore,
    _parse_frontmatter,
    _serialize_frontmatter,
    best_effort_commit,
    now_str,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("migrate_memory")


# ── frontmatter helpers ────────────────────────────────────────────

def _split_fm(text: str) -> tuple[dict, str]:
    """Split leading `---` frontmatter block from the body. Returns ({}, text) if absent."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body = "\n".join(lines[i + 1:]).strip()
            # _parse_frontmatter expects the full text (leading `---` included)
            return _parse_frontmatter(text), body
    return {}, text


def _first_text(text: str, limit: int = 200) -> str:
    text = text.strip().replace("\n", " ").replace("\r", " ")
    while "  " in text:
        text = text.replace("  ", " ")
    return text[:limit]


def _rewrite_ts(path: Path, created: str, updated: str) -> None:
    """Rewrite an entry's created/updated in place, preserving the body."""
    text = path.read_text(encoding="utf-8")
    fm, body = _split_fm(text)
    fm["created"] = created
    fm["updated"] = updated
    path.write_text("\n".join(_serialize_frontmatter(fm)) + "\n\n" + body, encoding="utf-8")


# ── per-source migrations ──────────────────────────────────────────

def _migrate_knowledge(legacy: Path, store: MemoryStore) -> list[str]:
    """knowledge/{topic}/{date}/*.md -> fact entries."""
    migrated: list[str] = []
    kdir = legacy / "knowledge"
    if not kdir.is_dir():
        return migrated
    for md in sorted(kdir.rglob("*.md")):
        rel = md.relative_to(kdir)
        parts = rel.parts
        topic = parts[0] if parts else ""
        text = md.read_text(encoding="utf-8")
        fm, body = _split_fm(text)
        title = str(fm.get("title") or md.stem)
        tags = [topic] if topic else []
        old_tags = fm.get("tags")
        if isinstance(old_tags, list):
            tags += [t for t in old_tags if t not in tags]
        refs: list[str] = []
        old_refs = fm.get("references")
        if isinstance(old_refs, list):
            refs = [str(r) for r in old_refs]
        elif old_refs:
            refs = [str(old_refs)]
        if fm.get("source"):
            refs.append(f"source:{fm['source']}")
        created = updated = str(fm.get("time") or fm.get("updated_at") or now_str())
        result = store.store(
            type_="fact",
            name=None,
            title=title,
            description=_first_text(body) or title,
            content=body or "(empty)",
            tags=tags,
            source="derived",
            refs=refs,
        )
        _rewrite_ts(Path(result["path"]), created, updated)
        migrated.append(f"fact: {md} -> {result['name']}")
    return migrated


def _migrate_skills(legacy: Path, store: MemoryStore) -> list[str]:
    """skills/{name}/skill.md -> skill entries."""
    migrated: list[str] = []
    sdir = legacy / "skills"
    if not sdir.is_dir():
        return migrated
    for skill_dir in sorted(sdir.iterdir()):
        skill_md = skill_dir / "skill.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        fm, body = _split_fm(text)
        skill_name = skill_dir.name
        title = str(fm.get("title") or skill_name)
        description = str(fm.get("description") or _first_text(body))
        old_tags = fm.get("tags")
        tags = [str(t) for t in old_tags] if isinstance(old_tags, list) else []
        refs = [f"source:{fm['source']}"] if fm.get("source") else []
        created = updated = str(fm.get("updated_at") or fm.get("time") or now_str())
        result = store.store(
            type_="skill",
            name=None,
            title=title,
            description=description,
            content=body or "(empty)",
            tags=tags,
            source="derived",
            refs=refs,
        )
        _rewrite_ts(Path(result["path"]), created, updated)
        migrated.append(f"skill: {skill_md} -> {result['name']}")
    return migrated


def _migrate_user_profile(uuid_dir: Path, store: MemoryStore) -> list[str]:
    """user-profile.md -> preference entry (name=user-profile)."""
    profile = uuid_dir / "user-profile.md"
    if not profile.is_file():
        return []
    existing = {e["name"] for e in store.list(type_="preference")}
    if "user-profile" in existing:
        return []
    text = profile.read_text(encoding="utf-8")
    fm, body = _split_fm(text)
    updated = str(fm.get("updated_at") or now_str())
    result = store.store(
        type_="preference",
        name="user-profile",
        title="用户画像",
        description="用户身份、表达风格、决策方式与边界（原 user-profile.md）",
        content=body or text,
        tags=["user-profile"],
        source="user",
        refs=[],
    )
    _rewrite_ts(Path(result["path"]), updated, updated)
    return [f"preference: {profile} -> {result['name']}"]


# ── workspace-level migration ──────────────────────────────────────

def migrate_workspace(uuid_dir: Path, dry_run: bool = False) -> dict:
    uuid = uuid_dir.name
    mem = uuid_dir / "memory"
    legacy = uuid_dir / "memory-legacy"
    v2_present = (mem / "knowledge").is_dir() or (mem / "skills").is_dir()
    already_v3 = (mem / "entries").is_dir()

    if already_v3 or (not v2_present and not legacy.is_dir()):
        return {"workspace": uuid, "skipped": True}

    if dry_run:
        counts = {"facts": 0, "skills": 0, "profile": 0}
        src = legacy if legacy.is_dir() else mem
        kdir = src / "knowledge"
        if kdir.is_dir():
            counts["facts"] = len(list(kdir.rglob("*.md")))
        sdir = src / "skills"
        if sdir.is_dir():
            counts["skills"] = len([p for p in sdir.rglob("skill.md")])
        if (uuid_dir / "user-profile.md").is_file():
            counts["profile"] = 1
        return {"workspace": uuid, "dry_run": True, "counts": counts}

    # Move old memory/ out of the way (kept intact), then build v3 fresh.
    if v2_present and not already_v3:
        if not legacy.exists():
            os.rename(mem, legacy)
        mem.mkdir(parents=True, exist_ok=True)

    store = MemoryStore(str(mem))
    migrated: list[str] = []
    if legacy.is_dir():
        migrated += _migrate_knowledge(legacy, store)
        migrated += _migrate_skills(legacy, store)
    migrated += _migrate_user_profile(uuid_dir, store)

    store._rebuild_index()
    ok, note = best_effort_commit(str(mem), "migrate v2 -> v3")
    return {"workspace": uuid, "migrated": migrated, "git": note if ok else ""}


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate v2 memory to v3 format (design §10).")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--uuid", default="", help="migrate only this workspace uuid")
    args = parser.parse_args()

    if not AGENTS_DIR.is_dir():
        logger.info("No agents dir at %s", AGENTS_DIR)
        return

    uuids = [AGENTS_DIR / name for name in sorted(os.listdir(AGENTS_DIR))]
    uuids = [d for d in uuids if d.is_dir() and d.name != "system"]
    if args.uuid:
        uuids = [d for d in uuids if d.name == args.uuid]

    total = 0
    for uuid_dir in uuids:
        result = migrate_workspace(uuid_dir, dry_run=args.dry_run)
        if result.get("skipped"):
            continue
        if args.dry_run:
            counts = result.get("counts", {})
            logger.info(
                "%s: dry-run → %d fact(s), %d skill(s), %d profile → %d total",
                result["workspace"], counts["facts"], counts["skills"], counts["profile"],
                counts["facts"] + counts["skills"] + counts["profile"],
            )
            total += counts["facts"] + counts["skills"] + counts["profile"]
        else:
            migrated = result.get("migrated", [])
            logger.info("%s: migrated %d file(s) → %s", result["workspace"], len(migrated), result.get("git", "no git"))
            for line in migrated:
                logger.info("  %s", line)
            total += len(migrated)
    logger.info("Done. %d migration(s)." % total)


if __name__ == "__main__":
    main()
