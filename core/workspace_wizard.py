"""新建工作区向导的确定性服务层。

向导不依赖模型：项目大类、目录位置、AGENTS.md 骨架和初始目录都由本模块生成。
学习项目按「一个科目一个工作区」展开，并共享同一个学习组。
"""

from __future__ import annotations

import ctypes
import os
import re
import secrets
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import (
    PROJECT_ROOT,
    save_workspace_config,
    validate_workspace_name,
)
from core.fs_utils import atomic_write_text

WORKSPACE_PARENT = PROJECT_ROOT / "workspace"
DATA_ROOT = PROJECT_ROOT / "data"
SYSTEM_DATA_DIR = DATA_ROOT / ".cili"

CATEGORIES: dict[str, dict[str, Any]] = {
    "study": {
        "id": "study",
        "name": "学习项目",
        "description": "按科目拆分工作区；生成学习计划、错题本和学习规范。",
    },
    "office": {
        "id": "office",
        "name": "日常办公",
        "description": "文档、表格、演示与资料归档；生成办公目录约定。",
    },
    "research": {
        "id": "research",
        "name": "科研项目",
        "description": "文献、实验、写作与引用管理；生成科研工作目录。",
    },
    "blank": {
        "id": "blank",
        "name": "空白项目",
        "description": "只创建基础工作区和最小 AGENTS.md。",
    },
}

LOCATIONS: list[dict[str, str]] = [
    {"id": "cili", "label": "cili 工作区"},
    {"id": "home", "label": "用户目录"},
    {"id": "documents", "label": "文档目录"},
    {"id": "custom", "label": "自定义目录"},
]

STAGES = ("小学", "初中", "高中")
SYSTEMS = ("六三制", "五四制")
# 年级随学制联动：六三制小学到六年级；五四制小学到五年级、初中从六年级起
GRADES: dict[str, dict[str, tuple[str, ...]]] = {
    "六三制": {
        "小学": ("一年级", "二年级", "三年级", "四年级", "五年级", "六年级"),
        "初中": ("七年级", "八年级", "九年级"),
        "高中": ("高一", "高二", "高三"),
    },
    "五四制": {
        "小学": ("一年级", "二年级", "三年级", "四年级", "五年级"),
        "初中": ("六年级", "七年级", "八年级", "九年级"),
        "高中": ("高一", "高二", "高三"),
    },
}
STUDY_SUBJECTS = {
    "小学": ("语文", "数学", "英语", "科学", "道德与法治"),
    "初中": ("语文", "数学", "英语", "物理", "化学", "生物", "历史", "地理", "道德与法治", "信息学"),
    "高中": ("语文", "数学", "英语", "物理", "化学", "生物", "历史", "地理", "思想政治", "信息技术", "信息学"),
}

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class WorkspaceWizardError(ValueError):
    """向导输入、路径或创建校验失败。"""


def documents_root() -> Path:
    """返回系统文档目录；Windows 优先解析 Known Folder。"""
    if sys.platform == "win32":
        try:
            buf = ctypes.c_wchar_p()
            # FOLDERID_Documents = {FDD39AD0-23AF-4618-825A-612EA0C454F8}
            guid = ctypes.GUID(
                0xFDD39AD0, 0x23AF, 0x4618,
                (0x82, 0x5A, 0x61, 0x2E, 0xA0, 0xC4, 0x54, 0xF8),
            )
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(guid), 0, None, ctypes.byref(buf)
            ) == 0:
                result = Path(buf.value)
                ctypes.windll.ole32.CoTaskMemFree(buf)
                if result:
                    return result
        except Exception:  # noqa: BLE001 — 目录探测失败时走通用回退
            pass
    candidates = [Path.home() / "Documents", Path.home() / "OneDrive" / "Documents"]
    return next((item for item in candidates if item.exists()), Path.home() / "Documents")


def slugify(value: str, *, fallback: str = "workspace") -> str:
    """把显示名转换为安全的目录名；保留中文。"""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value or "")
    value = re.sub(r"\s+", "-", value.strip())
    value = re.sub(r"-{2,}", "-", value).strip(".-")
    value = value[:40].rstrip(".-")
    if not value or value in (".", "..") or value.upper().split(".")[0] in WINDOWS_RESERVED:
        value = fallback
    return value or fallback


def _canonical(path: Path | str) -> Path:
    return Path(os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))))


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return child == parent or parent in child.parents
    except ValueError:
        return False


def resolve_location(
    location: str,
    slug: str,
    *,
    custom_directory: str = "",
) -> Path:
    """把位置预设和 slug 解析为最终目录。"""
    if location == "custom":
        if not custom_directory:
            raise WorkspaceWizardError("自定义目录不能为空")
        return _canonical(custom_directory)
    if location == "documents":
        root = documents_root()
    elif location == "home":
        root = Path.home() / "cili"
    elif location == "cili":
        root = WORKSPACE_PARENT
    else:
        raise WorkspaceWizardError("位置预设非法")
    return _canonical(root / slug) if slug else _canonical(root)


def validate_workspace_directory(
    directory: Path | str,
    *,
    existing_workspaces: list[dict[str, Any]] | None = None,
    exclude_uuid: str = "",
) -> list[str]:
    """校验新工作区目录，返回警告；非法时抛出 ``WorkspaceWizardError``。"""
    candidate = _canonical(directory)
    parent = _canonical(WORKSPACE_PARENT)
    warnings: list[str] = []

    if candidate == parent or _is_within(parent, candidate):
        raise WorkspaceWizardError("该目录包含 cili 工作区父目录，会暴露其它工作区")
    if candidate == _canonical(DATA_ROOT) or _is_within(_canonical(DATA_ROOT), candidate):
        raise WorkspaceWizardError("不能把系统数据目录作为工作区")
    if candidate == _canonical(SYSTEM_DATA_DIR) or _is_within(_canonical(SYSTEM_DATA_DIR), candidate):
        raise WorkspaceWizardError("不能把系统工作区数据目录作为工作区")

    for entry in existing_workspaces or []:
        if entry.get("uuid") == exclude_uuid or not entry.get("directory"):
            continue
        registered = _canonical(entry["directory"])
        label = entry.get("workspace_name", "")
        if registered == parent and candidate.parent == parent:
            # 兼容旧布局：Default 还指向 workspace 根时，允许先创建兄弟工作区；
            # 后续迁移会把这些已注册的直属子目录保留在原位。
            warnings.append("默认工作区尚未迁移；新目录会作为其兄弟目录创建")
            continue
        if candidate == registered:
            raise WorkspaceWizardError(f"目录已被工作区「{label}」使用")
        if _is_within(registered, candidate):
            raise WorkspaceWizardError(f"目录位于工作区「{label}」内部")
        if _is_within(candidate, registered):
            raise WorkspaceWizardError(f"目录包含工作区「{label}」")

    probe = candidate
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    if not os.access(probe, os.W_OK):
        raise WorkspaceWizardError(f"目录不可写: {probe}")
    if candidate.exists() and not candidate.is_dir():
        raise WorkspaceWizardError("目标路径已存在且不是目录")
    if len(str(candidate)) >= 240:
        warnings.append("目录较长，部分老程序可能不支持")
    if re.search(r"[^\x00-\x7f]", candidate.name):
        warnings.append("目录名包含中文，少数外部工具可能显示异常")
    return warnings


def _clean_line(value: Any, default: str = "", limit: int = 200) -> str:
    text = re.sub(r"[\r\n\t\x00-\x08\x0b\x0c\x0e-\x1f]", " ", str(value or ""))
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:limit] or default


def _render_agents_md(
    *,
    category: str,
    name: str,
    profile: dict[str, Any],
    subject: str = "",
) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    category_name = CATEGORIES.get(category, {}).get("name", category)
    lines = [
        f"# {name}",
        "",
        f"> 本文件由 cili 工作区向导生成；类别：{category_name}；生成时间：{today}。",
        "> 可在「工作区设置 → 项目提示词」中继续修改。",
        "",
        "## 工作区约定",
        "- 先读本文件，再开始改动。",
        "- 大范围改动前先说明计划；不确定时先问。",
        "- 不要自动执行 git commit / push。",
        "- 临时文件放 `.cili/tmp/`。",
    ]

    if category == "study":
        stage = _clean_line(profile.get("stage"), "未设置", 20)
        grade = _clean_line(profile.get("grade"), "未设置", 20)
        edition = _clean_line(profile.get("textbook_edition"), "未指定", 80)
        region = _clean_line(profile.get("exam_region"), "未指定", 80)
        focus = _clean_line(profile.get("focus"), "同步学习", 40)
        learner = _clean_line(profile.get("learner_name"), "学生", 40)
        subject_text = subject or _clean_line(profile.get("subject"), "综合学习", 40)
        minutes = profile.get("daily_minutes")
        lines.extend([
            "",
            "## 角色",
            f"你是 {learner} 的 {subject_text} 学科教练，负责引导思考，而不是直接代替完成作业。",
            "",
            "## 学习档案",
            f"- 学段：{stage}；年级：{grade}",
            f"- 教材版本：{edition}；考区：{region}",
            f"- 当前重点：{focus}" + (f"；建议每天 {minutes} 分钟" if minutes else ""),
            "",
            "## 教学原则",
            "1. 先问后讲：确认卡点再讲解。",
            "2. 分层提示：思路 → 关键步骤 → 完整解法。",
            "3. 每次讲完用 1-2 道变式题确认掌握。",
            "4. 不直接代写作业；给完整答案前必须先给思路。",
            "",
            "## 学习闭环",
            "课前小测 → 引导学习 → 题型归纳 → 课后小测",
            "",
            "## 错题与计划",
            "- 错题记录：题目、错因、正确思路、同类变式、复习日期。",
            "- 每次课后更新 `学习计划.md`；每周生成一次家长摘要。",
        ])
    elif category == "office":
        role = _clean_line(profile.get("role"), "办公助理", 80)
        language = _clean_line(profile.get("doc_language"), "中文", 20)
        lines.extend([
            "",
            "## 办公约定",
            f"- 岗位/角色：{role}；文档语言：{language}",
            "- 先给结论，再给要点；表格优先于长段落。",
            "- 文件名使用 `YYYY-MM-DD-主题`；完成文件放入 `归档/YYYY-MM/`。",
            "- 不外发文件；删除前必须确认。",
        ])
    elif category == "research":
        field = _clean_line(profile.get("field"), "综合研究", 80)
        direction = _clean_line(profile.get("direction"), "待定", 200)
        citation = _clean_line(profile.get("citation_style"), "GB/T 7714", 40)
        lines.extend([
            "",
            "## 研究约定",
            f"- 学科：{field}；方向：{direction}",
            f"- 引用格式：{citation}",
            "- 不编造文献、数据、页码；不确定的引用标注「待核实」。",
            "- 文献记录到 `文献库.md`；数据、代码、结果保持可复现分离。",
        ])
    else:
        lines.extend([
            "",
            "## 项目说明",
            "- 本工作区未预设项目类型；请在了解需求后与用户一起补全本文件。",
        ])

    return "\n".join(lines) + "\n"


def _scaffold(category: str, subject: str = "") -> tuple[list[str], dict[str, str]]:
    if category == "study":
        subject_dir = subject or "综合"
        dirs = [
            subject_dir, "错题本", "学习资料", "笔记", "练习", "教材",
        ]
        files = {
            "学习计划.md": "# 学习计划\n\n## 本周目标\n\n## 每日任务\n\n## 复盘\n",
            "错题本.md": "# 错题本\n\n## 题目\n\n## 错因\n\n## 正确思路\n\n## 同类变式\n\n## 复习日期\n",
            "学习资料/课程标准.md": "---\ntitle: 课程标准摘要\nstatus: skeleton\n---\n\n> 待补充；只放提纲与来源，不复制教材全文。\n",
            "学习资料/教材目录.md": "---\ntitle: 教材目录\nstatus: skeleton\n---\n\n> 按教材版本和章节补充。\n",
            "学习资料/学习规范.md": "# 学习规范\n\n- 课前小测。\n- 引导学习。\n- 题型归纳。\n- 课后小测。\n",
            "教材/README.md": "# 教材原件\n\n请只放置你已获授权的教材文件；文件保留在本地。\n",
        }
    elif category == "office":
        dirs = ["收件箱", "文档", "表格", "演示", "归档"]
        files = {"README.md": "# 办公工作区\n\n- 收件箱：待处理。\n- 归档：已完成文件按月份移动。\n"}
    elif category == "research":
        dirs = ["文献", "笔记", "数据", "代码", "初稿", "输出"]
        files = {
            "文献库.md": "# 文献库\n\n| 题录 | DOI/URL | 摘要 | 标签 | 状态 |\n|---|---|---|---|---|\n",
            "研究计划.md": "# 研究计划\n\n## 问题\n\n## 方法\n\n## 里程碑\n",
        }
    else:
        dirs = []
        files = {}
    return dirs, files


def _study_specs(profile: dict[str, Any], subjects: list[str]) -> list[dict[str, Any]]:
    clean: list[str] = []
    for subject in subjects:
        text = _clean_line(subject, "", 40)
        if text and text not in clean:
            clean.append(text)
    if not clean:
        raise WorkspaceWizardError("学习项目至少选择一个科目")
    stage = _clean_line(profile.get("stage"), "", 20)
    if stage not in STAGES:
        raise WorkspaceWizardError("学习项目必须选择学段")
    system = _clean_line(profile.get("system"), "六三制", 10)
    if system not in GRADES:
        raise WorkspaceWizardError("学制非法")
    grade = _clean_line(profile.get("grade"), "", 20)
    if grade and grade not in GRADES[system].get(stage, ()):
        raise WorkspaceWizardError(f"{stage}「{system}」没有「{grade}」，请重新选择年级")
    overrides = profile.get("subject_overrides")
    overrides = overrides if isinstance(overrides, dict) else {}
    specs: list[dict[str, Any]] = []
    for subject in clean:
        spec_profile = {**profile, "subject": subject}
        override = overrides.get(subject)
        if isinstance(override, dict):
            edition = _clean_line(override.get("textbook_edition"), "", 80)
            if edition:
                spec_profile["textbook_edition"] = edition
        specs.append({"subject": subject, "profile": spec_profile})
    return specs


def _workspace_title(
    *,
    category: str,
    base_name: str,
    profile: dict[str, Any],
    subject: str = "",
) -> str:
    if category != "study":
        return base_name
    grade = _clean_line(profile.get("grade"), "", 20)
    title = "-".join(item for item in (grade, subject) if item)
    return title or base_name


def preview_workspaces(
    *,
    category: str,
    base_name: str,
    location: str = "cili",
    slug: str = "",
    custom_directory: str = "",
    profile: dict[str, Any] | None = None,
    subjects: list[str] | None = None,
    existing_workspaces: list[dict[str, Any]] | None = None,
    allow_existing: bool = False,
) -> dict[str, Any]:
    """预览向导将创建的一个或多个工作区；不写文件。"""
    if category not in CATEGORIES:
        raise WorkspaceWizardError("项目类别非法")
    if base_name.strip():
        name_error = validate_workspace_name(base_name)
        if name_error:
            raise WorkspaceWizardError(name_error)
    profile = profile or {}
    subjects = subjects or []
    existing_names = {
        str(entry.get("name") or entry.get("workspace_name") or "").strip()
        for entry in existing_workspaces or []
    }
    existing_names.discard("")
    if category == "study":
        specs = _study_specs(profile, subjects)
    else:
        specs = [{"subject": "", "profile": profile}]

    group: dict[str, str] | None = None
    if category == "study":
        learner = _clean_line(profile.get("learner_name"), "学习", 40)
        stage = _clean_line(profile.get("stage"), "学习", 20)
        group = {"id": "g-" + secrets.token_hex(4), "name": f"{learner}-{stage}"}

    used_slugs: set[str] = set()
    workspaces: list[dict[str, Any]] = []
    all_warnings: list[str] = []
    for spec in specs:
        subject = spec["subject"]
        item_profile = spec["profile"]
        title = _workspace_title(
            category=category, base_name=base_name, profile=item_profile, subject=subject,
        )
        slug_source = title if category == "study" or not slug else slug.strip()
        if slug_source:
            slug_value = slugify(slug_source, fallback="")
            if not slug_value:
                raise WorkspaceWizardError(f"目录名非法: {slug_source}")
        else:
            # 名称与目录名都为空：最终目录直接用父目录本身
            slug_value = ""
        base_slug = slug_value
        suffix = 2
        while slug_value and slug_value in used_slugs:
            slug_value = f"{base_slug}-{suffix}"
            suffix += 1
        used_slugs.add(slug_value)
        if location == "custom" and len(specs) > 1:
            directory = (
                _canonical(Path(custom_directory) / slug_value)
                if slug_value else _canonical(custom_directory)
            )
        else:
            directory = resolve_location(location, slug_value, custom_directory=custom_directory)
        if not title:
            if location == "custom":
                title = Path(custom_directory).name or "workspace"
            else:
                title = directory.name
        title_error = validate_workspace_name(title)
        if title_error:
            raise WorkspaceWizardError(title_error)
        if title in existing_names:
            raise WorkspaceWizardError(f"已存在同名工作区「{title}」，请修改项目名称")
        warnings = validate_workspace_directory(directory, existing_workspaces=existing_workspaces)
        all_warnings.extend(f"{title}: {item}" for item in warnings)
        exists = directory.exists()
        is_empty = not exists or not any(directory.iterdir())
        if exists and (directory / ".cili").exists():
            raise WorkspaceWizardError(f"目标目录已包含工作区数据: {directory}")
        if exists and not is_empty and not allow_existing:
            raise WorkspaceWizardError(f"目标目录已存在且非空: {directory}；请勾选「允许写入已有目录」")

        scaffold_dirs, scaffold_files = _scaffold(category, subject)
        workspaces.append({
            "category": category,
            "subject": subject,
            "name": title,
            "slug": slug_value,
            "directory": str(directory),
            "profile": item_profile,
            "exists": exists,
            "is_empty": is_empty,
            "warnings": warnings,
            "agents_md": _render_agents_md(
                category=category, name=title, profile=item_profile, subject=subject,
            ),
            "scaffold": {"dirs": scaffold_dirs, "files": sorted(scaffold_files)},
        })

    return {
        "category": category,
        "category_name": CATEGORIES[category]["name"],
        "name": base_name,
        "location": location,
        "group": group,
        "workspaces": workspaces,
        "warnings": all_warnings,
    }


def _write_initial_files(workspace_dir: Path, item: dict[str, Any], *, overwrite_agents: bool) -> None:
    agents_path = workspace_dir / "AGENTS.md"
    if overwrite_agents or not agents_path.exists():
        atomic_write_text(agents_path, item["agents_md"])
    for rel_dir in item["scaffold"]["dirs"]:
        (workspace_dir / rel_dir).mkdir(parents=True, exist_ok=True)
    _, files = _scaffold(item.get("category", ""), item.get("subject", ""))
    for rel_file, content in files.items():
        path = workspace_dir / rel_file
        if not path.exists():
            atomic_write_text(path, content)


def create_workspaces(
    *,
    category: str,
    base_name: str,
    location: str = "cili",
    slug: str = "",
    custom_directory: str = "",
    profile: dict[str, Any] | None = None,
    subjects: list[str] | None = None,
    existing_workspaces: list[dict[str, Any]] | None = None,
    allow_existing: bool = False,
    overwrite_agents: bool = False,
    git_enabled: bool = True,
    memory_enabled: bool = True,
    new_uuid: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """创建向导预检过的工作区；单项失败不影响已成功项。"""
    preview = preview_workspaces(
        category=category,
        base_name=base_name,
        location=location,
        slug=slug,
        custom_directory=custom_directory,
        profile=profile,
        subjects=subjects,
        existing_workspaces=existing_workspaces,
        allow_existing=allow_existing,
    )
    new_id = new_uuid or (lambda: secrets.token_hex(4))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for item in preview["workspaces"]:
        directory = Path(item["directory"])
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _write_initial_files(directory, item, overwrite_agents=overwrite_agents)
            uuid = new_id()
            config = {
                "workspace_name": item["name"],
                "directory": str(directory),
                "slug": item["slug"],
                "location": location,
                "category": category,
                "subject": item["subject"],
                "profile": item["profile"],
                "created_at": now,
                "updated_at": now,
                "git_enabled": git_enabled,
                "memory_enabled": memory_enabled,
            }
            if preview.get("group"):
                config["study_group_id"] = preview["group"]["id"]
                config["study_group_name"] = preview["group"]["name"]
            if not save_workspace_config(uuid, config):
                raise WorkspaceWizardError("工作区索引写入失败")

            data_dir = directory / ".cili"
            (data_dir / "sessions").mkdir(parents=True, exist_ok=True)

            git_message = ""
            if git_enabled:
                from core.workspace_git import init_workspace_git

                git_message = init_workspace_git(uuid)[1]
            created.append({
                "uuid": uuid,
                "name": item["name"],
                "subject": item["subject"],
                "directory": str(directory),
                "study_group": preview.get("group"),
                "profile": item["profile"],
                "git_message": git_message,
            })
        except (WorkspaceWizardError, OSError) as e:
            failed.append({
                "name": item["name"],
                "subject": item["subject"],
                "directory": str(directory),
                "error": str(e),
            })

    if failed and not created:
        raise WorkspaceWizardError(failed[0]["error"])
    return {
        "category": category,
        "group": preview.get("group"),
        "created": created,
        "failed": failed,
        "skipped": [],
        "warnings": preview["warnings"],
    }
