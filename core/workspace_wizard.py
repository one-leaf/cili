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
    remove_workspace_entry,
    save_workspace_meta,
    validate_workspace_name,
    workspaces_root,
)
from core.fs_utils import atomic_write_text, path_key
from core.time_utils import now_str

WORKSPACE_PARENT = workspaces_root()
DATA_ROOT = PROJECT_ROOT / "data"
SUBJECT_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "subjects"
CODE_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "code"
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
    "code": {
        "id": "code",
        "name": "编程项目",
        "description": "先选开发语言与项目类型；生成编码准则和语言/工程约束。",
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

# 编程项目：语言与项目类型各选其一；id 与 templates/code 下的模板文件同名。
CODE_LANGUAGES: list[dict[str, str]] = [
    {"id": "python", "name": "Python"},
    {"id": "ts", "name": "TypeScript / JavaScript"},
    {"id": "java", "name": "Java"},
    {"id": "csharp", "name": "C# / .NET"},
    {"id": "go", "name": "Go"},
    {"id": "rust", "name": "Rust"},
    {"id": "cpp", "name": "C / C++"},
    {"id": "php", "name": "PHP"},
    {"id": "kotlin", "name": "Kotlin"},
    {"id": "swift", "name": "Swift"},
    {"id": "other", "name": "其他"},
]

CODE_PROJECT_TYPES: list[dict[str, str]] = [
    {"id": "web-api", "name": "Web 后端（HTTP API）"},
    {"id": "web-frontend", "name": "Web 前端"},
    {"id": "fullstack", "name": "全栈 Web 应用"},
    {"id": "cli", "name": "命令行工具"},
    {"id": "desktop", "name": "桌面应用"},
    {"id": "mobile", "name": "移动应用"},
    {"id": "data", "name": "数据处理 / 脚本"},
    {"id": "library", "name": "库 / SDK"},
    {"id": "game", "name": "游戏"},
    {"id": "other", "name": "其他 / 自定义"},
]

CODE_SCAFFOLD_DIRS: dict[str, list[str]] = {
    "web-api": ["src", "tests", "docs"],
    "web-frontend": ["src", "public", "tests"],
    "fullstack": ["backend", "frontend", "docs"],
    "cli": ["src", "tests"],
    "desktop": ["src", "tests"],
    "mobile": ["src", "assets"],
    "data": ["data", "notebooks", "outputs"],
    "library": ["src", "tests", "examples"],
    "game": ["src", "assets"],
}

CODE_GITIGNORE: dict[str, list[str]] = {
    "python": ["__pycache__/", "*.py[cod]", ".venv/", "venv/", ".pytest_cache/", ".ruff_cache/"],
    "ts": ["node_modules/", "dist/", "build/", "*.log"],
    "java": ["target/", "build/", ".gradle/", "*.class"],
    "csharp": ["bin/", "obj/"],
    "go": ["/bin/", "*.exe", "*.test"],
    "rust": ["/target/"],
    "cpp": ["build/", "*.o", "*.obj", "*.exe"],
    "php": ["vendor/"],
    "kotlin": ["build/", ".gradle/", "local.properties"],
    "swift": [".build/", "DerivedData/", "*.xcuserstate"],
}

CODE_GITIGNORE_COMMON = [".env", ".DS_Store", "Thumbs.db", "desktop.ini"]

def _official_catalog():
    """教材目录以数据包索引为准（内置 dataset_index.json 兜底，不访问官方缓存）。"""
    from core.textbooks.pack_store import dataset_catalog

    return dataset_catalog()


def stage_options() -> list[str]:
    """官方学段列表（含五•四学制、特殊教育等）。"""
    return _official_catalog().stage_options()


def grade_options(stage: str) -> list[str]:
    """指定学段的年级列表：归一化官方标签，过滤「教师用书」等非年级标签。"""
    from core.textbooks.smartedu import GRADE_ALIASES, GRADE_ORDER

    try:
        raw = _official_catalog().grades(stage)
    except Exception:  # noqa: BLE001
        raw = []
    if "高中" in stage:
        # 高中教材多数只标「高中年级」，始终提供完整三段
        return ["高一", "高二", "高三"]
    mapped = {GRADE_ALIASES.get(g, g) for g in raw}
    return [g for g in GRADE_ORDER if g in mapped]


def study_subjects_by_stage() -> dict[str, list[str]]:
    """各学段的学科列表（来自官方目录的学科标签）。"""
    catalog = _official_catalog()
    return {
        stage: [item["subject"] for item in catalog.subjects(stage=stage)]
        for stage in catalog.stage_options()
    }


def system_for_stage(stage: str) -> str:
    """由学段推导学制（仅用于模板占位文案）。"""
    return "五四制" if "五•四学制" in stage else "六三制"


def template_stage(stage: str) -> str:
    """模板文件名使用的学段：去掉五•四学制后缀，未知学段回退通用模板。"""
    base = stage.replace("（五•四学制）", "").strip()
    return base if base in ("小学", "初中", "高中") else stage

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
    return Path(path_key(path, resolve=True))


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
        if not entry.get("directory"):
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
    return warnings


def _clean_line(value: Any, default: str = "", limit: int = 200) -> str:
    text = re.sub(r"[\r\n\t\x00-\x08\x0b\x0c\x0e-\x1f]", " ", str(value or ""))
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:limit] or default


def _textbook_range_lines(
    stage: str, subject: str, edition: str, grade: str
) -> str:
    """从官方教材目录取该年级/版本/学科的课本清单；失败时给出兜底文案。"""
    try:
        from core.textbooks.smartedu import normalize_subject

        catalog = _official_catalog()
        books = catalog.find_books(
            stage=stage, subject=normalize_subject(subject),
            edition=edition, grade=grade,
        )
        if not books and subject:
            books = catalog.find_books(stage=stage, subject=subject,
                                       edition=edition, grade=grade)
    except Exception:  # noqa: BLE001 —— 教材目录不可用不阻断工作区创建
        return "- 以学校实际使用的教材为准。"
    titles: list[str] = []
    for book in sorted(books, key=lambda b: (b.grade, b.volume, b.title)):
        line = f"- 《{book.title}》（{book.edition}）" if book.edition else f"- 《{book.title}》"
        if line not in titles:
            titles.append(line)
    if not titles:
        return "- 以学校实际使用的教材为准（目录中暂无该年级电子课本）。"
    return "\n".join(titles[:12])


def _render_study_template(profile: dict[str, Any], subject: str) -> str:
    """按「学段-学科」模板生成学科教练提示词；模板缺失回退通用模板。"""
    stage = _clean_line(profile.get("stage"), "小学", 20)
    from core.textbooks.smartedu import normalize_subject

    tpl_subject = normalize_subject(subject)
    path = SUBJECT_TEMPLATE_DIR / f"{template_stage(stage)}-{tpl_subject}.md"
    if not path.is_file():
        path = SUBJECT_TEMPLATE_DIR / "通用.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = "## 角色\n\n你是 {{learner}} 的 {{subject}} 学科教练，负责引导思考，而不是直接代替完成作业。"
    grade = _clean_line(profile.get("grade"), "", 20)
    minutes = profile.get("daily_minutes")
    mapping = {
        "learner": _clean_line(profile.get("learner_name"), "同学", 40),
        "subject": subject or "综合学习",
        "stage": stage,
        "grade": grade or "当前年级",
        "system": _clean_line(profile.get("system"), system_for_stage(stage), 10),
        "edition": _clean_line(profile.get("textbook_edition"), "学校在用版本", 80),
        "exam_region": _clean_line(profile.get("exam_region"), "本地", 80),
        "focus": _clean_line(profile.get("focus"), "同步学习", 40),
        "daily_minutes": f"{int(minutes)} 分钟" if minutes else "30 分钟",
        "textbook_range": _textbook_range_lines(
            stage,
            subject,
            _clean_line(profile.get("textbook_edition"), "", 80),
            grade,
        ),
    }
    for key, value in mapping.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _code_selector(
    options: list[dict[str, str]], value: Any
) -> tuple[str, str]:
    """归一化语言/类型选择，返回（模板 id，显示名）。

    不在表中视为自定义名称（「其他」），用 ``other`` 通用模板。
    """
    text = _clean_line(value, "", 40)
    for item in options:
        if item["id"] == text:
            return item["id"], item["name"]
    return "other", text


def _read_code_template(rel: str, fallback: str) -> str:
    try:
        return (CODE_TEMPLATE_DIR / rel).read_text(encoding="utf-8").strip()
    except OSError:
        return fallback


def _render_code_template(profile: dict[str, Any]) -> str:
    """编程项目 AGENTS.md 主体：概况 + 四条准则 + 语言约束 + 项目类型约束。"""
    language_id, language_name = _code_selector(CODE_LANGUAGES, profile.get("language"))
    type_id, type_name = _code_selector(CODE_PROJECT_TYPES, profile.get("project_type"))
    sections = [
        f"## 项目概况\n\n- 开发语言：{language_name}\n- 项目类型：{type_name}\n"
        "- 工具链命令（构建、测试、格式）如与本文件不一致，以实际项目为准并同步更新本文件。",
        _read_code_template(
            "coding-guidelines.md",
            "## 编码前先思考\n\n不确定时先问，摆明取舍与假设。\n\n"
            "## 简单优先\n\n用解决问题的最少代码。\n\n"
            "## 外科手术式修改\n\n只动必须动的。\n\n"
            "## 目标驱动执行\n\n先定义可验证的成功标准。",
        ),
        _read_code_template(f"languages/{language_id}.md", ""),
        _read_code_template(f"project-types/{type_id}.md", ""),
    ]
    text = "\n\n".join(part for part in sections if part)
    return (
        text.replace("{{language}}", language_name)
        .replace("{{project_type}}", type_name)
    )


def _gitignore_text(language_id: str) -> str:
    lines = list(CODE_GITIGNORE.get(language_id, []))
    for item in CODE_GITIGNORE_COMMON:
        if item not in lines:
            lines.append(item)
    return "\n".join(lines) + "\n"


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
        "- 工具参数用工作区相对路径；不要用 `../` 跳出工作区。",
        "- 临时文件放 `.cili/tmp/`。",
    ]

    if category == "study":
        lines.extend([
            "",
            _render_study_template(profile, subject),
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
    elif category == "code":
        lines.extend([
            "",
            _render_code_template(profile),
        ])
    else:
        lines.extend([
            "",
            "## 项目说明",
            "- 本工作区未预设项目类型；请在了解需求后与用户一起补全本文件。",
        ])

    return "\n".join(lines) + "\n"


def _scaffold(
    category: str, subject: str = "", profile: dict[str, Any] | None = None
) -> tuple[list[str], dict[str, str]]:
    profile = profile or {}
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
    elif category == "code":
        type_id, _ = _code_selector(CODE_PROJECT_TYPES, profile.get("project_type"))
        language_id, _ = _code_selector(CODE_LANGUAGES, profile.get("language"))
        dirs = list(CODE_SCAFFOLD_DIRS.get(type_id, ["src", "tests"]))
        files = {
            "README.md": "# 项目说明\n\n## 运行\n\n## 测试\n",
            ".gitignore": _gitignore_text(language_id),
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
    if not _clean_line(profile.get("learner_name"), "", 40):
        raise WorkspaceWizardError("学习项目必须填写孩子称呼")
    stage = _clean_line(profile.get("stage"), "", 20)
    if not stage:
        raise WorkspaceWizardError("学习项目必须选择学段")
    try:
        valid_stages = stage_options()
    except Exception:  # noqa: BLE001 —— 目录不可用时按手动输入处理
        valid_stages = []
    if valid_stages and stage not in valid_stages:
        raise WorkspaceWizardError(f"学段非法: {stage}")
    system = system_for_stage(stage)
    grade = _clean_line(profile.get("grade"), "", 20)
    if grade:
        try:
            valid_grades = grade_options(stage)
        except Exception:  # noqa: BLE001
            valid_grades = []
        if valid_grades and grade not in valid_grades:
            raise WorkspaceWizardError(f"{stage}没有「{grade}」，请重新选择年级")
    overrides = profile.get("subject_overrides")
    overrides = overrides if isinstance(overrides, dict) else {}
    specs: list[dict[str, Any]] = []
    for subject in clean:
        spec_profile = {**profile, "subject": subject, "system": system}
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
    # 孩子称呼放在最前：同一孩子的工作区按名称排序时自然聚在一起，
    # 家里多个孩子也能靠名称/目录名区分。
    # 目录名不带年级：年级是会随学年变化的进度状态，写入 AGENTS.md 即可
    # （升级时只改 AGENTS.md，工作区与学习记录跨学年延续）。
    learner = _clean_line(profile.get("learner_name"), "", 40)
    title = "-".join(item for item in (learner, subject) if item)
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
        if category == "code":
            if not _clean_line(profile.get("language"), "", 40):
                raise WorkspaceWizardError("请选择开发语言")
            if not _clean_line(profile.get("project_type"), "", 40):
                raise WorkspaceWizardError("请选择项目类型")
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

        scaffold_dirs, scaffold_files = _scaffold(category, subject, item_profile)
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
    _, files = _scaffold(
        item.get("category", ""), item.get("subject", ""), item.get("profile") or {},
    )
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
    now = now_str()
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for item in preview["workspaces"]:
        directory = Path(item["directory"])
        saved_uuid: str | None = None
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
            if not save_workspace_meta(uuid, config):
                raise WorkspaceWizardError("工作区索引写入失败")
            saved_uuid = uuid

            data_dir = directory / ".cili"
            (data_dir / "sessions").mkdir(parents=True, exist_ok=True)
            from core.layout_migration import write_layout_marker
            write_layout_marker(data_dir)
            (data_dir / "config" / "approval_rules").mkdir(parents=True, exist_ok=True)
            (data_dir / "config" / "sensitive_words" / "rules").mkdir(parents=True, exist_ok=True)

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
            if saved_uuid:
                remove_workspace_entry(saved_uuid)
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
