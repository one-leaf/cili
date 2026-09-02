"""Memory tool - store and manage long-term memory (knowledge, skills)."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any

from core.config import PROJECT_ROOT
from core.tools.shared.base import Tool, ToolResult


class MemoryTool(Tool):
    name = "memory"
    description = (
        "Long-term memory tool for storing cross-session knowledge and reusable skills. "
        "Knowledge is stored as Markdown in knowledge/{topic}/{date}/{file}.md. "
        "Skills are stored as Markdown with frontmatter in skills/{skill-name}/skill.md. "
        "Use 'find'/'grep' to search, 'read' to read file content."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["store", "update", "delete"],
                "description": "Operation type: store (create), update (modify), delete (remove). Use 'find'/'grep' to search skills/knowledge, and 'read' tool to read file content."
            },
            "memory_type": {
                "type": "string",
                "enum": ["knowledge", "skill"],
                "description": "Memory type: knowledge=facts (Markdown), skill=reusable techniques (Markdown)"
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
            }
        },
        "required": ["action", "memory_type"]
    }

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session_manager=None):
        super().__init__(cwd, workspace_uuid, session_manager)
        # Memory base directory: use get_workspace_data_dir() for consistent fallback
        from core.config import get_workspace_data_dir
        self.memory_dir = str(get_workspace_data_dir(workspace_uuid) / "memory")

    # ─── public entry point ────────────────────────────────────────────

    def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "store")
        memory_type = kwargs.get("memory_type")

        if not memory_type:
            return ToolResult("Error: memory_type is required", error=True)

        try:
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

    # ─── store ─────────────────────────────────────────────────────────

    def _store(self, kwargs: dict) -> ToolResult:
        memory_type = kwargs["memory_type"]
        if memory_type == "knowledge":
            return self._store_knowledge(kwargs)
        elif memory_type == "skill":
            return self._store_skill(kwargs)
        return ToolResult(f"Error: unknown memory_type '{memory_type}'", error=True)

    def _store_knowledge(self, kwargs: dict) -> ToolResult:
        """Store a knowledge Markdown file with frontmatter."""
        title = kwargs.get("title", "")
        if not title:
            return ToolResult("Error: title is required for knowledge", error=True)

        topic = kwargs.get("topic", "misc")
        filename = kwargs.get("filename")
        date_str = datetime.now().strftime("%Y-%m-%d")

        # Auto-generate filename from title if not provided
        if not filename:
            filename = self._title_to_filename(title, ".md")
        elif not filename.endswith(".md"):
            filename = filename + ".md"

        # Build directory path
        date_dir = os.path.join(self.memory_dir, "knowledge", topic, date_str)
        os.makedirs(date_dir, exist_ok=True)

        file_path = self._resolve_filename_conflict(date_dir, filename)

        # Build knowledge markdown
        content = self._build_knowledge_markdown(kwargs)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult(f"Successfully stored knowledge in {file_path}")

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

        # Build skill directory
        skill_dir = os.path.join(self.memory_dir, "skills", skill_name)
        os.makedirs(skill_dir, exist_ok=True)

        skill_path = os.path.join(skill_dir, "skill.md")

        # Check if skill already exists
        if os.path.exists(skill_path):
            return ToolResult(f"Error: skill '{skill_name}' already exists. Use update action to modify.", error=True)

        # Build skill markdown
        content = self._build_skill_markdown(kwargs)
        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult(f"Successfully stored skill in {skill_path}")

    def _build_knowledge_markdown(self, kwargs: dict) -> str:
        """Build knowledge Markdown with frontmatter.

        Format:
        ---
        title: "标题"
        source: manual
        time: 2026-08-21 10:30:00
        tags: [tag1, tag2]
        ---

        正文内容...
        """
        title = kwargs.get("title", "")
        source = kwargs.get("source", "manual")
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tags = kwargs.get("tags", [])
        content = kwargs.get("content", "")

        lines = ["---"]
        lines.append(f'title: "{title}"')
        lines.append(f"source: {source}")
        lines.append(f"time: {time_str}")
        if tags:
            tags_str = ", ".join(tags)
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
        lines.append(f'name: "{name}"')
        lines.append(f'description: "{description}"')
        if tags:
            tags_str = ", ".join(tags)
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
        from core.tools.shared.skill import _parse_skill_frontmatter
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
        """Update an existing knowledge file."""
        title = kwargs.get("title", "")
        if not title:
            return ToolResult("Error: title is required to find knowledge", error=True)

        topic = kwargs.get("topic", "misc")
        found_path = self._find_knowledge_by_title(topic, title)

        if not found_path:
            return ToolResult(
                f"Error: no knowledge with title '{title}' found in topic '{topic}'",
                error=True,
            )

        content = self._build_knowledge_markdown(kwargs)
        with open(found_path, "w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult(f"Successfully updated {found_path}")

    def _update_skill(self, kwargs: dict) -> ToolResult:
        """Update an existing skill."""
        skill_name = kwargs.get("skill_name", "")
        if not skill_name:
            return ToolResult("Error: skill_name is required to find skill", error=True)

        skill_path = os.path.join(self.memory_dir, "skills", skill_name, "skill.md")
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
        lines.append(f'name: "{name}"')
        lines.append(f'description: "{description}"')
        if tags:
            tags_str = ", ".join(tags)
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

        skill_dir = os.path.join(self.memory_dir, "skills", skill_name)
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

    def _parse_markdown_title(self, content: str) -> str:
        """Extract title from markdown frontmatter."""
        frontmatter = self._parse_skill_frontmatter(content)
        return frontmatter.get("title", "")

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

    @staticmethod
    def move_to_latest_date(file_path: str) -> ToolResult:
        """Move a memory file to today's date directory.

        Called after reading a knowledge file to track last access time.
        No-op if the file is already in today's directory.
        Only applies to knowledge files, not skills.
        """
        if not os.path.exists(file_path):
            return ToolResult(f"Error: file not found: {file_path}", error=True)

        # Normalize path separators
        normalized_path = file_path.replace("\\", "/")

        # Skills don't use date directories
        if "/skills/" in normalized_path:
            return ToolResult("Skills don't use date-based organization")

        date_str = datetime.now().strftime("%Y-%m-%d")

        # Parse: .../memory/{type}/{topic}/{date}/{filename}
        parts = normalized_path.split("/")
        try:
            memory_idx = next(i for i, p in enumerate(parts) if p == "memory")
        except StopIteration:
            return ToolResult("Error: not a memory file path", error=True)

        try:
            mem_type = parts[memory_idx + 1]
            topic = parts[memory_idx + 2]
            current_date = parts[memory_idx + 3]
            filename = "/".join(parts[memory_idx + 4:])
        except IndexError:
            return ToolResult("Error: malformed memory path", error=True)

        if current_date == date_str:
            return ToolResult("File already in today's directory, no move needed")

        base_path = "/".join(parts[:memory_idx + 1])
        new_path = f"{base_path}/{mem_type}/{topic}/{date_str}/{filename}"

        os.makedirs(os.path.dirname(new_path), exist_ok=True)

        # Handle filename conflicts
        new_path = MemoryTool._resolve_filename_conflict(
            f"{base_path}/{mem_type}/{topic}/{date_str}", filename
        )

        os.rename(file_path, new_path)

        # Clean up empty source directories
        MemoryTool._cleanup_empty_parents(file_path, levels=2)

        return ToolResult(f"Moved to {new_path}")
