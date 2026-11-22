"""内置技能目录：扫描 core/skills、解析 frontmatter、按角色/接口过滤。"""

from __future__ import annotations

import os
import re

from core.config import PROJECT_ROOT

# 统一技能目录（平铺，取代原 shared/root/sub 分层）
SKILLS_DIR = str(PROJECT_ROOT / "core" / "skills")

def _parse_skill_frontmatter(content: str) -> dict:
    """解析 skill.md 的 YAML frontmatter。

    Returns dict with name, description, tags, etc.
    Returns empty dict if no valid frontmatter found.
    """
    if not content.startswith("---"):
        return {}

    end_idx = content.find("---", 3)
    if end_idx == -1:
        return {}

    frontmatter_text = content[3:end_idx].strip()
    result = {}

    for line in frontmatter_text.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()

        # Remove quotes
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        # Parse tags array
        elif value.startswith("[") and value.endswith("]"):
            value = [t.strip().strip('"').strip("'") for t in value[1:-1].split(",") if t.strip()]

        result[key] = value

    return result


def _as_frontmatter_list(value) -> list[str]:
    """frontmatter 列表字段归一化：list 或逗号分隔字符串 → str 列表。"""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _visible_for(meta: dict, role: str, interface: str | None = None) -> bool:
    """frontmatter roles + interfaces 过滤：缺省视为全部可见。

    ``interfaces`` 语义：声明了 ``interfaces`` 的技能只在列出的接口可见。
    接口未声明（None：cron 回合 / 未设置接口的交互回合 / 子代理）时一律
    不可见——宁缺勿猜，与 ``interface_caps`` 的未声明语义一致。

    roles 维度语义与原 ``_visible_for_role`` 相同（内置技能只声明 master/worker/lite）。
    """
    roles = meta.get("roles")
    if roles and role not in _as_frontmatter_list(roles):
        return False
    interfaces = meta.get("interfaces")
    if interfaces and (interface is None or interface not in _as_frontmatter_list(interfaces)):
        return False
    return True


def list_skills(role: str, interface: str | None = None) -> list[dict]:
    """扫描 core/skills/，返回对 role/interface 可见的技能。

    Returns list of dicts with: id, name, description.
    """
    skills = []
    if not os.path.isdir(SKILLS_DIR):
        return skills

    for entry in sorted(os.listdir(SKILLS_DIR)):
        skill_dir = os.path.join(SKILLS_DIR, entry)
        skill_file = os.path.join(skill_dir, "skill.md")

        if not os.path.isdir(skill_dir) or not os.path.isfile(skill_file):
            continue

        try:
            with open(skill_file, encoding="utf-8") as f:
                meta = _parse_skill_frontmatter(f.read())
            if not _visible_for(meta, role, interface):
                continue
            skills.append({
                "id": entry,
                "name": meta.get("name", entry),
                "description": meta.get("description", ""),
                "when_to_use": meta.get("when_to_use", ""),
            })
        except Exception:
            continue

    return skills


def read_skill(role: str, skill_id: str, interface: str | None = None) -> str | None:
    """按目录名读取技能完整内容。

    Returns the full skill.md content, or None if not found or the skill is
    not visible to *role*/*interface* (frontmatter roles + interfaces filter).
    """
    if not re.match(r'^[a-zA-Z0-9_-]+$', skill_id):
        return None

    skill_file = os.path.join(SKILLS_DIR, skill_id, "skill.md")
    if not os.path.isfile(skill_file):
        return None

    try:
        with open(skill_file, encoding="utf-8") as f:
            content = f.read()
        if not _visible_for(_parse_skill_frontmatter(content), role, interface):
            return None
        return content
    except Exception:
        return None
