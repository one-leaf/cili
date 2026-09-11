"""Memory tool - store and manage long-term memory (knowledge, skills)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT
from core.tools.base import Tool, ToolResult, UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END

# T16：find 全量 IO —— (path, mtime_ns, size) 键控的内容缓存。
# 记忆文件只在 store/update/delete 或外部编辑时变更，mtime+size 不变即可
# 复用缓存，避免每次 find 重读全部 .md。内容小（markdown），LRU 淘汰。
_FIND_CACHE_MAX = 512
_FIND_CACHE: "OrderedDict[tuple[str, int, int], str]" = OrderedDict()
_FIND_CACHE_LOCK = threading.Lock()


def _fm_value(value: Any) -> str:
    """清理用于 frontmatter 双引号值的字符串。

    frontmatter 解析是逐行的、只剥掉首尾引号、没有转义处理，
    所以内嵌引号和换行必须直接替换掉，否则会破坏文件结构。
    """
    return str(value).replace('"', "'").replace("\r", " ").replace("\n", " ").strip()



class MemoryTool(Tool):
    name = "memory"
    description = (
        "Long-term memory tool for storing and finding cross-session knowledge and reusable skills. "
        "Knowledge is stored as Markdown in knowledge/{topic}/{date}/{file}.md. "
        "Skills are stored as Markdown with frontmatter in skills/{skill-name}/skill.md. "
        "Use the 'find' action to search by keyword, 'read' to read full file content."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["store", "find", "update", "delete"],
                "description": "Operation type: store (create), find (search by keyword), update (modify), delete (remove)."
            },
            "query": {
                "type": "string",
                "description": "Keyword to search for (case-insensitive substring match). Required for find. "
                               "Matches knowledge titles/tags/content and skill names/descriptions/tags/content."
            },
            "memory_type": {
                "type": "string",
                "enum": ["knowledge", "skill"],
                "description": "Memory type: knowledge=facts (Markdown), skill=reusable techniques (Markdown). "
                               "Required for store/update/delete; optional for find (omit to search both)."
            },
            "topic": {
                "type": "string",
                "description": "Topic directory name (kebab-case, e.g., 'api-design', 'deploy'). Use 'misc' for uncategorized. Required for knowledge."
            },
            "skill_name": {
                "type": "string",
                "description": "Skill directory name in kebab-case (e.g., 'python-async', 'k8s-deploy', 'find-sjtu-professor-info'). Must be meaningful and descriptive, NOT a UUID. Required for skill type operations."
            },
            "filename": {
                "type": "string",
                "description": "Content filename with .md extension. Auto-generated from title if not provided. For knowledge only."
            },
            "title": {
                "type": "string",
                "description": "Memory title (required for knowledge store)"
            },
            "name": {
                "type": "string",
                "description": "Skill display name (max 64 chars). Required for skill store."
            },
            "description": {
                "type": "string",
                "description": "Skill description (max 200 chars, used for progressive loading). Required for skill store."
            },
            "content": {
                "type": "string",
                "description": "Content body (knowledge) or skill body (Markdown). Required for store/update. "
                               "IMPORTANT: Content must be complete. If source document is long and was truncated "
                               "by read/python tool, use offset/limit to fetch remaining parts before storing. "
                               "Do NOT store partial content with placeholder like '内容已截取'."
            },
            "source": {
                "type": "string",
                "enum": ["manual", "web_search", "browser", "python"],
                "description": "Knowledge source: manual=user provided, web_search=search results, browser=browser fetch, python=code execution. Only for knowledge."
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tag list for categorization and retrieval"
            },
            "source_ref": {
                "type": "string",
                "description": "Source reference for this knowledge. Examples: 'file:E:/docs/config.yaml' for file sources, 'session:abc123' for conversation sessions, 'web:https://...' for web sources. Added to the references list. Only for knowledge."
            }
        },
        "required": ["action"]
    }

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session_manager=None):
        super().__init__(cwd, workspace_uuid, session_manager)
        # Memory base directory: use get_workspace_data_dir() for consistent fallback
        from core.config import get_workspace_data_dir
        self.memory_dir = str(get_workspace_data_dir(workspace_uuid) / "memory")

    @staticmethod
    def _validate_component(name: str, field: str) -> None:
        """Reject path-traversal / absolute components in LLM-provided names.

        Raises ValueError so the caller's try/except turns it into an error result.
        """
        if not name or name in (".", "..") or "/" in name or "\\" in name or os.path.isabs(name):
            raise ValueError(f"Invalid {field}: {name!r}")

    def _safe_memory_path(self, *parts: str) -> str:
        """Resolve parts under memory_dir, rejecting traversal or absolute escapes.

        On Windows, os.path.join(base, "C:\\...") silently returns the absolute
        path, so a naive join can escape memory_dir entirely. This helper
        validates every component and double-checks the resolved path stays
        inside memory_dir.
        """
        base = Path(self.memory_dir).resolve()
        target = base.joinpath(*parts).resolve()
        if not target.is_relative_to(base):
            raise ValueError("Memory path escapes the memory directory")
        return str(target)

    # ─── public entry point ────────────────────────────────────────────

    def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "store")
        memory_type = kwargs.get("memory_type")

        try:
            if action == "find":
                return self._find(kwargs)

            if not memory_type:
                return ToolResult("Error: memory_type is required", error=True)

            if action == "store":
                return self._store(kwargs)
            elif action == "update":
                return self._update(kwargs)
            elif action == "delete":
                return self._delete(kwargs)
            else:
                return ToolResult(f"Error: unknown action '{action}'", error=True)
        except Exception as e:
            return ToolResult(f"Error: {e}", error=True)

    # ─── find ──────────────────────────────────────────────────────────

    _FIND_MAX_RESULTS = 20

    def _find(self, kwargs: dict) -> ToolResult:
        """按关键词检索 knowledge 与 skills（大小写不敏感子串匹配）。

        模型检索记忆时习惯调 find（而非 grep/read 组合），此 action
        一次调用覆盖两种类型，返回完整路径与匹配片段供后续 read 读取全文。
        结果按文件修改时间倒序（最新在前）。
        """
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return ToolResult("Error: query is required for find", error=True)

        memory_type = kwargs.get("memory_type")
        if memory_type and memory_type not in ("knowledge", "skill"):
            return ToolResult(f"Error: unknown memory_type '{memory_type}'", error=True)

        needle = query.lower()
        entries: list[tuple[float, str]] = []
        if memory_type in (None, "knowledge"):
            entries.extend(self._find_in_knowledge(needle))
        if memory_type in (None, "skill"):
            entries.extend(self._find_in_skills(needle))
        # 全局按文件修改时间倒序（最新在前），knowledge/skill 混合排序
        entries.sort(key=lambda x: x[0], reverse=True)

        if not entries:
            return ToolResult(
                f"No memory matches for '{query}'. "
                "Use the 'store' action to create it if worth keeping."
            )

        total = len(entries)
        lines = [f"Found {total} match(es) for '{query}':", ""]
        lines.extend(entry for _mtime, entry in entries[:self._FIND_MAX_RESULTS])
        if total > self._FIND_MAX_RESULTS:
            lines.append("")
            lines.append(f"... and {total - self._FIND_MAX_RESULTS} more. Refine the query to narrow results.")
        return ToolResult(
            UNTRUSTED_DATA_BEGIN + "\n".join(lines) + UNTRUSTED_DATA_END
        )

    def _find_in_knowledge(self, needle: str) -> list[tuple[float, str]]:
        """遍历 knowledge 目录，返回 (mtime, 格式化条目) 列表。"""
        base = os.path.join(self.memory_dir, "knowledge")
        results: list[tuple[float, str]] = []
        for mtime, fpath in self._iter_memory_markdown_files(base):
            content = self._read_memory_file(fpath)
            if content is None or needle not in content.lower():
                continue
            fm = self._parse_knowledge_frontmatter(content)
            title = str(fm.get("title", "")) or os.path.basename(fpath)
            lines = [f"[knowledge] {title}", f"  path: {fpath}"]
            tags = fm.get("tags")
            if tags:
                lines.append(f"  tags: [{', '.join(str(t) for t in tags)}]")
            snippet = self._extract_match_snippet(content, needle)
            if snippet:
                lines.append(f"  snippet: {snippet}")
            results.append((mtime, "\n".join(lines)))
        return results

    def _find_in_skills(self, needle: str) -> list[tuple[float, str]]:
        """遍历 skills 目录，返回 (mtime, 格式化条目) 列表。"""
        base = os.path.join(self.memory_dir, "skills")
        results: list[tuple[float, str]] = []
        for mtime, fpath in self._iter_memory_markdown_files(base):
            content = self._read_memory_file(fpath)
            if content is None or needle not in content.lower():
                continue
            fm = self._parse_skill_frontmatter(content)
            name = str(fm.get("name", "")) or os.path.basename(os.path.dirname(fpath))
            lines = [f"[skill] {name}"]
            lines.append(f"  path: {fpath}")
            desc = str(fm.get("description", ""))
            if desc:
                lines.append(f"  description: {desc}")
            tags = fm.get("tags")
            if tags:
                lines.append(f"  tags: [{', '.join(str(t) for t in tags)}]")
            snippet = self._extract_match_snippet(content, needle)
            if snippet:
                lines.append(f"  snippet: {snippet}")
            results.append((mtime, "\n".join(lines)))
        return results

    # ─── find helpers ──────────────────────────────────────────────────

    @staticmethod
    def _iter_memory_markdown_files(base_dir: str) -> list[tuple[float, str]]:
        """递归列出 base_dir 下所有 .md 文件，按修改时间倒序（最新在前）。"""
        if not os.path.isdir(base_dir):
            return []
        found: list[tuple[float, str]] = []
        for root, _dirs, files in os.walk(base_dir):
            for fname in files:
                if fname.endswith(".md"):
                    fpath = os.path.join(root, fname)
                    try:
                        mtime = os.path.getmtime(fpath)
                    except OSError:
                        mtime = 0.0
                    found.append((mtime, fpath))
        found.sort(key=lambda x: x[0], reverse=True)
        return found

    @staticmethod
    def _read_memory_file(fpath: str) -> str | None:
        """读取文件内容，带 (path, mtime_ns, size) 键控缓存（T16）。"""
        try:
            stat = os.stat(fpath)
            key = (os.path.abspath(fpath), stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None
        with _FIND_CACHE_LOCK:
            cached = _FIND_CACHE.get(key)
            if cached is not None:
                _FIND_CACHE.move_to_end(key)
                return cached
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return None
        with _FIND_CACHE_LOCK:
            _FIND_CACHE[key] = content
            _FIND_CACHE.move_to_end(key)
            while len(_FIND_CACHE) > _FIND_CACHE_MAX:
                _FIND_CACHE.popitem(last=False)
        return content

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """剥掉 frontmatter，返回正文。无 frontmatter 时原样返回。"""
        if not content.startswith("---"):
            return content
        end_idx = content.find("---", 3)
        if end_idx == -1:
            return content
        return content[end_idx + 3:]

    @classmethod
    def _extract_match_snippet(cls, content: str, needle: str, width: int = 150) -> str:
        """提取第一个命中行片段；命中都在 frontmatter 时返回正文开头预览。"""
        body = cls._strip_frontmatter(content)
        idx = body.lower().find(needle)
        if idx == -1:
            # 命中在 frontmatter（title/description/tags），给正文开头预览
            preview_src = body.strip()
            if not preview_src:
                return ""
            preview = preview_src[:width].replace("\n", " ")
            return preview + ("..." if len(preview_src) > width else "")
        start = body.rfind("\n", 0, idx) + 1
        end = body.find("\n", idx)
        if end == -1:
            end = len(body)
        line = body[start:end].strip()
        if len(line) <= width:
            return line
        rel = idx - start
        left = max(0, min(rel - width // 3, len(line) - width))
        return ("..." if left > 0 else "") + line[left:left + width].strip() + ("..." if left + width < len(line) else "")

    # ─── store ─────────────────────────────────────────────────────────

    def _store(self, kwargs: dict) -> ToolResult:
        memory_type = kwargs["memory_type"]
        if memory_type == "knowledge":
            return self._store_knowledge(kwargs)
        elif memory_type == "skill":
            return self._store_skill(kwargs)
        return ToolResult(f"Error: unknown memory_type '{memory_type}'", error=True)

    def _store_knowledge(self, kwargs: dict) -> ToolResult:
        """Store a knowledge Markdown file with frontmatter.

        If a knowledge with the same title already exists (across all topics),
        automatically update it instead of creating a duplicate.
        References are merged when updating.
        """
        title = kwargs.get("title", "")
        if not title:
            return ToolResult("Error: title is required for knowledge", error=True)

        # Dedup: search for existing knowledge with same title across all topics
        existing_path = self._find_knowledge_by_title_across_topics(title)
        if existing_path:
            # Found existing — update in place, merge references
            return self._update_knowledge_with_refs(existing_path, kwargs)

        topic = kwargs.get("topic", "misc")
        filename = kwargs.get("filename")
        date_str = datetime.now().strftime("%Y-%m-%d")

        # Auto-generate filename from title if not provided
        if not filename:
            filename = self._title_to_filename(title, ".md")
        elif not filename.endswith(".md"):
            filename = filename + ".md"

        self._validate_component(topic, "topic")
        self._validate_component(filename, "filename")

        # Build directory path
        date_dir = self._safe_memory_path("knowledge", topic, date_str)
        os.makedirs(date_dir, exist_ok=True)

        file_path = self._resolve_filename_conflict(date_dir, filename)

        # Build knowledge markdown
        content = self._build_knowledge_markdown(kwargs)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult(f"Successfully stored knowledge in {file_path}")

    def _update_knowledge_with_refs(self, existing_path: str, kwargs: dict) -> ToolResult:
        """Update existing knowledge, merging references from old and new."""
        try:
            with open(existing_path, "r", encoding="utf-8") as f:
                old_content = f.read()
            old_fm = self._parse_knowledge_frontmatter(old_content)
            old_refs = old_fm.get("references", [])
            if isinstance(old_refs, str):
                old_refs = [old_refs]
        except Exception:
            old_refs = []

        # Get new source_ref if provided
        new_ref = kwargs.get("source_ref", "")
        merged_refs = list(old_refs)
        if new_ref and new_ref not in merged_refs:
            merged_refs.append(new_ref)

        # Build updated content with merged references
        content = self._build_knowledge_markdown(kwargs, references=merged_refs)
        with open(existing_path, "w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult(f"Found existing knowledge with same title, updated: {existing_path}")

    def _store_skill(self, kwargs: dict) -> ToolResult:
        """Store a skill as Markdown with frontmatter.

        Skill format:
        ---
        name: Skill Name
        description: Short description for progressive loading
        tags: [tag1, tag2]
        created: 2026-08-21 10:30:00
        updated: 2026-08-21 10:30:00
        ---

        ## Overview
        Skill content in markdown...
        """
        skill_name = kwargs.get("skill_name", "")
        name = kwargs.get("name", "")
        description = kwargs.get("description", "")

        if not skill_name:
            return ToolResult("Error: skill_name is required for skill", error=True)
        if not name:
            return ToolResult("Error: name is required for skill", error=True)
        if not description:
            return ToolResult("Error: description is required for skill", error=True)

        # Validate skill_name format (reject UUID-like names)
        if re.match(r'^skill-[a-f0-9]{8}$', skill_name) or re.match(r'^[a-f0-9-]{36}$', skill_name):
            return ToolResult("Error: skill_name must be a meaningful kebab-case name (e.g., 'python-async'), not a UUID", error=True)

        # Validate lengths
        if len(name) > 64:
            return ToolResult("Error: name must be 64 characters or less", error=True)
        if len(description) > 200:
            return ToolResult("Error: description must be 200 characters or less", error=True)

        self._validate_component(skill_name, "skill_name")

        # Build skill directory
        skill_dir = self._safe_memory_path("skills", skill_name)
        os.makedirs(skill_dir, exist_ok=True)

        skill_path = os.path.join(skill_dir, "skill.md")

        # Check if skill already exists — auto-update if so
        if os.path.exists(skill_path):
            # Parse existing to preserve created time
            with open(skill_path, "r", encoding="utf-8") as f:
                existing = f.read()
            existing_fm = self._parse_skill_frontmatter(existing)

            # Merge: use provided values, fall back to existing
            merged_name = name
            merged_desc = description
            merged_tags = kwargs.get("tags", existing_fm.get("tags", []))
            merged_content = kwargs.get("content", "")
            created = existing_fm.get("created", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            lines = ["---"]
            lines.append(f'name: "{merged_name}"')
            lines.append(f'description: "{merged_desc}"')
            if merged_tags:
                tags_str = ", ".join(merged_tags)
                lines.append(f"tags: [{tags_str}]")
            lines.append(f"created: {created}")
            lines.append(f"updated: {updated}")
            lines.append("---")
            lines.append("")
            if merged_content:
                lines.append(merged_content)

            with open(skill_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

            return ToolResult(f"Skill '{skill_name}' already exists, updated: {skill_path}")

        # Build skill markdown
        content = self._build_skill_markdown(kwargs)
        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult(f"Successfully stored skill in {skill_path}")

    def _build_knowledge_markdown(self, kwargs: dict, references: list[str] | None = None) -> str:
        """Build knowledge Markdown with frontmatter.

        Format:
        ---
        title: "标题"
        source: manual
        references:
          - "file:E:/docs/config.yaml"
          - "session:abc123"
        time: 2026-08-21 10:30:00
        tags: [tag1, tag2]
        ---

        正文内容...
        """
        title = kwargs.get("title", "")
        source = kwargs.get("source", "manual")
        source_ref = kwargs.get("source_ref", "")
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tags = kwargs.get("tags", [])
        content = kwargs.get("content", "")

        # Build references list: merge explicit list + source_ref
        refs: list[str] = list(references) if references else []
        if source_ref and source_ref not in refs:
            refs.append(source_ref)

        lines = ["---"]
        lines.append(f'title: "{_fm_value(title)}"')
        lines.append(f"source: {_fm_value(source)}")
        if refs:
            lines.append("references:")
            for ref in refs:
                # Normalize file paths
                if ref.startswith("file:"):
                    norm = ref[len("file:"):].replace("\\", "/")
                    lines.append(f'  - "file:{_fm_value(norm)}"')
                else:
                    lines.append(f'  - "{_fm_value(ref)}"')
        lines.append(f"time: {time_str}")
        if tags:
            tags_str = ", ".join(_fm_value(t) for t in tags)
            lines.append(f"tags: [{tags_str}]")
        lines.append("---")
        lines.append("")
        if content:
            lines.append(content)

        return "\n".join(lines)

    def _build_skill_markdown(self, kwargs: dict) -> str:
        """Build skill Markdown with frontmatter.

        Format:
        ---
        name: Skill Name
        description: Short description
        tags: [tag1, tag2]
        created: 2026-08-21 10:30:00
        updated: 2026-08-21 10:30:00
        ---

        ## Overview
        Skill content...
        """
        name = kwargs.get("name", "")
        description = kwargs.get("description", "")
        tags = kwargs.get("tags", [])
        content = kwargs.get("content", "")
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = ["---"]
        lines.append(f'name: "{_fm_value(name)}"')
        lines.append(f'description: "{_fm_value(description)}"')
        if tags:
            tags_str = ", ".join(_fm_value(t) for t in tags)
            lines.append(f"tags: [{tags_str}]")
        lines.append(f"created: {time_str}")
        lines.append(f"updated: {time_str}")
        lines.append("---")
        lines.append("")
        if content:
            lines.append(content)
        else:
            lines.append("## Overview")
            lines.append("")
            lines.append(f"This skill: {description}")

        return "\n".join(lines)

    # ─── list ──────────────────────────────────────────────────────────

    def _parse_skill_frontmatter(self, content: str) -> dict:
        """Parse skill frontmatter into dict."""
        from core.tools.skill import _parse_skill_frontmatter
        return _parse_skill_frontmatter(content)

    # ─── update ────────────────────────────────────────────────────────

    def _update(self, kwargs: dict) -> ToolResult:
        memory_type = kwargs["memory_type"]

        if memory_type == "knowledge":
            return self._update_knowledge(kwargs)
        elif memory_type == "skill":
            return self._update_skill(kwargs)

        return ToolResult(f"Error: unknown memory_type '{memory_type}'", error=True)

    def _update_knowledge(self, kwargs: dict) -> ToolResult:
        """Update an existing knowledge file, merging references."""
        title = kwargs.get("title", "")
        if not title:
            return ToolResult("Error: title is required to find knowledge", error=True)

        topic = kwargs.get("topic", "misc")
        self._validate_component(topic, "topic")
        found_path = self._find_knowledge_by_title(topic, title)

        if not found_path:
            return ToolResult(
                f"Error: no knowledge with title '{title}' found in topic '{topic}'",
                error=True,
            )

        # Merge references from existing file
        return self._update_knowledge_with_refs(found_path, kwargs)

    def _update_skill(self, kwargs: dict) -> ToolResult:
        """Update an existing skill."""
        skill_name = kwargs.get("skill_name", "")
        if not skill_name:
            return ToolResult("Error: skill_name is required to find skill", error=True)
        self._validate_component(skill_name, "skill_name")

        skill_path = self._safe_memory_path("skills", skill_name, "skill.md")
        if not os.path.exists(skill_path):
            return ToolResult(f"Error: skill '{skill_name}' not found", error=True)

        # Parse existing frontmatter to preserve created time
        with open(skill_path, "r", encoding="utf-8") as f:
            existing = f.read()
        existing_fm = self._parse_skill_frontmatter(existing)

        # Build updated skill
        name = kwargs.get("name", existing_fm.get("name", ""))
        description = kwargs.get("description", existing_fm.get("description", ""))
        tags = kwargs.get("tags", existing_fm.get("tags", []))
        content = kwargs.get("content", "")

        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        created = existing_fm.get("created", time_str)

        # Validate lengths
        if len(name) > 64:
            return ToolResult("Error: name must be 64 characters or less", error=True)
        if len(description) > 200:
            return ToolResult("Error: description must be 200 characters or less", error=True)

        # Build new content
        lines = ["---"]
        lines.append(f'name: "{_fm_value(name)}"')
        lines.append(f'description: "{_fm_value(description)}"')
        if tags:
            tags_str = ", ".join(_fm_value(t) for t in tags)
            lines.append(f"tags: [{tags_str}]")
        lines.append(f"created: {created}")
        lines.append(f"updated: {time_str}")
        lines.append("---")
        lines.append("")
        if content:
            lines.append(content)

        with open(skill_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return ToolResult(f"Successfully updated skill: {skill_path}")

    # ─── delete ────────────────────────────────────────────────────────

    def _delete(self, kwargs: dict) -> ToolResult:
        memory_type = kwargs["memory_type"]

        if memory_type == "knowledge":
            return self._delete_knowledge(kwargs)
        elif memory_type == "skill":
            return self._delete_skill(kwargs)

        return ToolResult(f"Error: unknown memory_type '{memory_type}'", error=True)

    def _delete_knowledge(self, kwargs: dict) -> ToolResult:
        """Delete an existing knowledge file."""
        title = kwargs.get("title", "")
        if not title:
            return ToolResult("Error: title is required to find knowledge", error=True)

        topic = kwargs.get("topic", "misc")
        self._validate_component(topic, "topic")
        found_path = self._find_knowledge_by_title(topic, title)

        if not found_path:
            return ToolResult(
                f"Error: no knowledge with title '{title}' found in topic '{topic}'",
                error=True,
            )

        os.remove(found_path)

        # Clean up empty date and topic directories
        self._cleanup_empty_parents(found_path, levels=2)

        return ToolResult(f"Successfully deleted {found_path}")

    def _delete_skill(self, kwargs: dict) -> ToolResult:
        """Delete a skill directory."""
        skill_name = kwargs.get("skill_name", "")
        if not skill_name:
            return ToolResult("Error: skill_name is required to delete skill", error=True)
        self._validate_component(skill_name, "skill_name")

        skill_dir = self._safe_memory_path("skills", skill_name)
        if not os.path.isdir(skill_dir):
            return ToolResult(f"Error: skill '{skill_name}' not found", error=True)

        # Remove all files in skill directory
        import shutil
        shutil.rmtree(skill_dir)

        return ToolResult(f"Successfully deleted skill: {skill_name}")

    # ─── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _resolve_filename_conflict(directory: str, filename: str) -> str:
        """Resolve filename conflict by appending a counter suffix."""
        file_path = os.path.join(directory, filename)
        if not os.path.exists(file_path):
            return file_path
        base = filename.rsplit(".", 1)[0]
        ext = filename[len(base):]
        counter = 2
        while os.path.exists(file_path):
            filename = f"{base}-{counter}{ext}"
            file_path = os.path.join(directory, filename)
            counter += 1
        return file_path

    @staticmethod
    def _cleanup_empty_parents(path: str, levels: int = 2) -> None:
        """Remove empty parent directories up to `levels` above path."""
        current = os.path.dirname(path)
        for _ in range(levels):
            try:
                if os.path.isdir(current) and not os.listdir(current):
                    os.rmdir(current)
                else:
                    break
            except OSError:
                break
            current = os.path.dirname(current)

    def _find_knowledge_by_title(self, topic: str, title: str) -> str | None:
        """Find a knowledge file by searching its title field.

        Searches all date directories under the topic, newest first.
        """
        type_dir = os.path.join(self.memory_dir, "knowledge")
        topic_dir = os.path.join(type_dir, topic)
        if not os.path.isdir(topic_dir):
            return None

        # Collect all files, sorted by date desc (newest first)
        all_files = []
        for date_dir_name in os.listdir(topic_dir):
            date_path = os.path.join(topic_dir, date_dir_name)
            if not os.path.isdir(date_path):
                continue
            for fname in os.listdir(date_path):
                if fname.endswith(".md"):
                    all_files.append(os.path.join(date_path, fname))

        # Sort newest date directory first
        all_files.sort(reverse=True)

        for fpath in all_files:
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                file_title = self._parse_markdown_title(content)
                if file_title == title:
                    return fpath
            except Exception:
                continue

        return None

    def _find_knowledge_by_title_across_topics(self, title: str) -> str | None:
        """Find a knowledge file by title, searching across all topics.

        Used for dedup during store — prevents creating duplicate knowledge
        with the same title in different topics.
        Returns newest match first.
        """
        type_dir = os.path.join(self.memory_dir, "knowledge")
        if not os.path.isdir(type_dir):
            return None

        # Collect all knowledge files across all topics and dates
        all_files: list[tuple[str, str]] = []  # (filepath, date_str)
        for topic_name in os.listdir(type_dir):
            topic_path = os.path.join(type_dir, topic_name)
            if not os.path.isdir(topic_path):
                continue
            for date_dir_name in os.listdir(topic_path):
                date_path = os.path.join(topic_path, date_dir_name)
                if not os.path.isdir(date_path):
                    continue
                for fname in os.listdir(date_path):
                    if fname.endswith(".md"):
                        fpath = os.path.join(date_path, fname)
                        all_files.append((fpath, date_dir_name))

        # Sort by date desc (newest first)
        all_files.sort(key=lambda x: x[1], reverse=True)

        for fpath, _ in all_files:
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                file_title = self._parse_markdown_title(content)
                if file_title == title:
                    return fpath
            except Exception:
                continue

        return None

    def _parse_markdown_title(self, content: str) -> str:
        """Extract title from markdown frontmatter."""
        frontmatter = self._parse_knowledge_frontmatter(content)
        return frontmatter.get("title", "")

    def _parse_knowledge_frontmatter(self, content: str) -> dict:
        """Parse knowledge markdown frontmatter, including references list.

        Handles YAML-style lists:
          references:
            - "file:..."
            - "session:..."
        """
        if not content.startswith("---"):
            return {}

        end_idx = content.find("---", 3)
        if end_idx == -1:
            return {}

        frontmatter_text = content[3:end_idx].strip()
        result: dict = {}
        lines = frontmatter_text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            i += 1

            if not stripped or ":" not in stripped:
                continue

            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()

            # Remove quotes
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            # Parse inline array: [a, b, c]
            elif value.startswith("[") and value.endswith("]"):
                value = [t.strip().strip('"').strip("'") for t in value[1:-1].split(",") if t.strip()]
            # YAML list (empty value, next lines are - items)
            elif value == "":
                list_items: list[str] = []
                while i < len(lines):
                    item_line = lines[i].strip()
                    if item_line.startswith("- "):
                        item = item_line[2:].strip()
                        if item.startswith('"') and item.endswith('"'):
                            item = item[1:-1]
                        elif item.startswith("'") and item.endswith("'"):
                            item = item[1:-1]
                        list_items.append(item)
                        i += 1
                    else:
                        break
                value = list_items

            result[key] = value

        return result

    @staticmethod
    def _title_to_filename(title: str, extension: str = ".md") -> str:
        """Convert title to a safe filename with given extension.

        - ASCII titles → kebab-case: "Python asyncio" → "python-asyncio.md"
        - Non-ASCII titles → short hash: "用户偏好" → "memory-a1b2c3d4.md"
        - Falls back to "untitled.md" for empty results
        - Truncates long names with hash to stay under 100 chars
        """
        # Check if title is ASCII-only
        is_ascii = title.isascii()

        if is_ascii:
            name = title.lower()
            name = re.sub(r'[^a-z0-9\s-]', '', name)
            name = re.sub(r'[\s-]+', '-', name)
            name = name.strip('-')
            if not name:
                name = "untitled"
        else:
            # Non-ASCII: use an 8-char hash of the title
            h = hashlib.md5(title.encode("utf-8")).hexdigest()[:8]
            name = f"memory-{h}"

        # Truncate long names (keep under 100 chars total, leaving room for extension)
        max_name_len = 100 - len(extension)
        if len(name) > max_name_len:
            # Keep first 84 chars + 8-char hash for uniqueness
            h = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
            name = f"{name[:84]}-{h}"

        return f"{name}{extension}"

