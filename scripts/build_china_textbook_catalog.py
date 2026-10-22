import hashlib
import json
import os
import re
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
TREE = Path(os.environ.get(
    "CHINA_TEXTBOOK_TREE",
    str(ROOT / "data" / "cili" / "tmp" / "china_textbook_tree.json"),
))
OUT_JSON = ROOT / "core/templates/textbooks/china_textbook_catalog.json"
OUT_MD = ROOT / "workspace/default/ChinaTextbook-清单.md"

if not TREE.exists():
    TREE.parent.mkdir(parents=True, exist_ok=True)
    url = "https://api.github.com/repos/one-leaf/ChinaTextbook/git/trees/master?recursive=1"
    request = urllib.request.Request(url, headers={"User-Agent": "cili-china-textbook-catalog"})
    with urllib.request.urlopen(request, timeout=120) as response:
        TREE.write_bytes(response.read())

data = json.loads(TREE.read_text(encoding="utf-8"))
sha = data.get("sha", "")
tree = data.get("tree", [])


def stage_label(raw: str) -> tuple[str, str]:
    if raw.startswith("小学"):
        return "小学", ("五四制" if "五•四" in raw else "六三制")
    if raw.startswith("初中"):
        return "初中", ("五四制" if "五•四" in raw else "六三制")
    if raw.startswith("高中"):
        return "高中", "六三制"
    if raw == "大学":
        return "大学", "other"
    return raw, "other"


def norm_subject(subject: str) -> str:
    mapping = {
        "生物学": "生物",
        "地理图册": "地理",
        "人文地理": "地理",
        "语文·书法练习指导": "语文",
        "体育与健康": "体育",
        "道德与法治": "道德与法治",
        "思想政治": "思想政治",
        "通用技术": "通用技术",
        "信息技术": "信息技术",
    }
    return mapping.get(subject, subject)


def norm_edition(edition: str) -> str:
    edition = re.sub(r"（主编：[^）]*）", "", edition)
    edition = re.sub(r"\(主编：[^)]*\)", "", edition)
    edition = edition.split("-", 1)[0]
    return edition.strip()


def safe_component(value: str, maxlen: int = 80) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", value or "")
    value = re.sub(r"\s+", " ", value).strip().rstrip(". ")
    return value[:maxlen] or "未命名"


GRADE_RE = re.compile(r"([一二三四五六七八九]年级|高[一二三]|[0-9]+至[0-9]+年级)")
SEMESTER_RE = re.compile(r"(上册|下册|全一册)")
TYPE_RULES = [
    ("supplement", re.compile(r"书法练习指导")),
    ("teacher", re.compile(r"教师用书|教师教学用书")),
    ("workbook", re.compile(r"学生活动手册|活动手册|练习册|作业本|同步练习")),
    ("atlas", re.compile(r"图册")),
    ("reader", re.compile(r"读本")),
    ("exam", re.compile(r"试卷|真题|试题|答案")),
]
HIGH_TYPE_RE = re.compile(r"(选择性必修|必修|选修)")
VOLUME_RE = re.compile(r"第[一二三四五六七八九十0-9]+册")

raw_books = defaultdict(list)
other_files = []
for item in tree:
    if item.get("type") != "blob":
        continue
    path = item["path"]
    match = re.match(r"(?i)(.*\.pdf)(?:\.(\d+))?$", path)
    if not match:
        other_files.append(item)
        continue
    source_base = match.group(1)
    index = int(match.group(2) or 0)
    raw_books[source_base].append((index, item))

# 仓库里同一个 PDF 可能同时存在：
#   - 整本文件：.../book.pdf
#   - 分片目录：.../book.pdf_merge_folder/book.pdf.1, .2, ...
# 以去掉 _merge_folder 后的路径为同一本书；优先整本文件，缺失时用分片。
books = {}
source_duplicates_merged = 0
for raw_base, entries in raw_books.items():
    folder, name = raw_base.rsplit("/", 1) if "/" in raw_base else ("", raw_base)
    if folder.endswith("_merge_folder"):
        normalized = folder[: -len("_merge_folder")].rstrip("/")
    else:
        normalized = raw_base
    books.setdefault(normalized, []).append((raw_base, entries))

selected_books = {}
for normalized, variants in books.items():
    plain_entries = []
    split_entries = []
    for raw_base, entries in variants:
        for index, item in entries:
            if re.search(r"\.pdf\.\d+$", item["path"], re.I):
                split_entries.append((index, item))
            else:
                plain_entries.append((index, item))
    if plain_entries:
        # 同一个 PDF 同时有整本和分片时优先整本，避免重复下载。
        selected_books[normalized] = [plain_entries[0]]
        if split_entries:
            source_duplicates_merged += 1
    else:
        selected_books[normalized] = split_entries
books = selected_books

records = []
target_seen = {}
for source_base, chunks in books.items():
    chunks = sorted(chunks, key=lambda pair: pair[0])
    parts = source_base.split("/")
    stage_raw = parts[0] if parts else ""
    stage, system = stage_label(stage_raw)
    subject_raw = parts[1] if len(parts) > 1 else ""
    subject = norm_subject(subject_raw)
    edition_raw = parts[2] if len(parts) > 2 else ""
    # 路径只有 3 段时，第 3 段是文件名，不是版本目录。
    if len(parts) == 3 and parts[2].lower().endswith(".pdf"):
        edition_raw = ""
    edition = norm_edition(edition_raw)
    title = re.sub(r"(?i)\.pdf$", "", parts[-1])

    grade = ""
    if len(parts) > 3 and re.fullmatch(r"([一二三四五六七八九]年级|高[一二三]|[0-9]+至[0-9]+年级)", parts[3]):
        grade = parts[3]
    if not grade:
        match = GRADE_RE.search(title)
        if match:
            grade = match.group(1)

    semester = ""
    match = SEMESTER_RE.search(title)
    if match:
        semester = match.group(1)

    book_type = "textbook"
    for type_name, pattern in TYPE_RULES:
        if pattern.search(title):
            book_type = type_name
            break

    high_type = ""
    match = HIGH_TYPE_RE.search(title)
    if match:
        high_type = match.group(1)

    volume = ""
    match = VOLUME_RE.search(title)
    if match:
        volume = match.group(0)

    files = []
    for index, item in chunks:
        files.append({
            "index": index if index else 1,
            "path": item["path"],
            "url": (
                "https://raw.githubusercontent.com/one-leaf/ChinaTextbook/"
                + sha + "/" + quote(item["path"], safe="/")
            ),
            "size": item.get("size", 0),
        })
    total_size = sum(file["size"] for file in files)

    leaf = ""
    if grade and semester:
        leaf = f"{safe_component(grade)}/{safe_component(semester)}"
    elif grade:
        leaf = safe_component(grade)
    elif high_type:
        leaf = safe_component(high_type)
        if volume:
            leaf += "/" + safe_component(volume)
    elif semester:
        leaf = safe_component(semester)

    target_rel = "/".join([
        "教材", "原始资料", safe_component(stage), safe_component(subject or "其他"),
        safe_component(edition_raw or edition or "未标注", 60),
    ] + ([leaf] if leaf else []) + [safe_component(title) + ".pdf"])

    base_target = target_rel
    # 同版本同书名仍有冲突时，用源路径短哈希稳定消歧，避免依赖排序的 -2。
    suffix = 0
    while target_rel in target_seen:
        suffix += 1
        digest = hashlib.sha1(f"{source_base}|{suffix}".encode("utf-8")).hexdigest()[:6]
        target_rel = base_target[:-4] + f"-{digest}.pdf"
    target_seen[target_rel] = source_base

    records.append({
        "id": source_base,
        "book_key": hashlib.sha1(source_base.encode("utf-8")).hexdigest()[:12],
        "stage": stage,
        "stage_raw": stage_raw,
        "system": system,
        "subject": subject,
        "subject_raw": subject_raw,
        "edition": edition,
        "edition_raw": edition_raw,
        "grade": grade,
        "semester": semester,
        "book_type": book_type,
        "high_type": high_type,
        "volume": volume,
        "title": title,
        "source_path": source_base,
        "target_path": target_rel,
        "split": any(re.search(r"\.pdf\.\d+$", file["path"], re.I) for file in files),
        "chunk_count": len(files),
        "total_size": total_size,
        "files": files,
    })

records.sort(key=lambda record: (
    {"小学": 0, "初中": 1, "高中": 2, "大学": 3}.get(record["stage"], 9),
    record["subject"], record["edition"], record["grade"], record["semester"], record["title"],
))

other = [
    {
        "path": item["path"],
        "size": item.get("size", 0),
        "url": (
            "https://raw.githubusercontent.com/one-leaf/ChinaTextbook/"
            + sha + "/" + quote(item["path"], safe="/")
        ),
    }
    for item in other_files
]

stats = {
    "books": len(records),
    "split_books": sum(1 for record in records if record["split"]),
    "plain_books": sum(1 for record in records if not record["split"]),
    "total_bytes": sum(record["total_size"] for record in records),
    "other_files": len(other),
    "source_duplicates_merged": source_duplicates_merged,
    "textbook_books": sum(1 for record in records if record["book_type"] == "textbook"),
    "book_type_counts": dict(Counter(record["book_type"] for record in records)),
    "stage_counts": dict(Counter(record["stage"] for record in records)),
    "subject_counts": {
        f"{key[0]}/{key[1]}": count
        for key, count in Counter((record["stage"], record["subject"]) for record in records).most_common()
    },
    "unknown_grade": sum(
        1 for record in records
        if not record["grade"] and not record["high_type"] and record["stage"] in ("小学", "初中")
    ),
}

catalog = {
    "schema_version": 1,
    "source": {
        "repo": "one-leaf/ChinaTextbook",
        "upstream": "TapXWorld/ChinaTextbook",
        "branch": "master",
        "commit": sha,
        "raw_base": f"https://raw.githubusercontent.com/one-leaf/ChinaTextbook/{sha}/",
        "generated_at": "2026-10-22",
        "license_note": "仓库未声明单独许可证；教材版权归原出版方，下载和使用前请自行确认授权范围。",
    },
    "download_policy": {
        "default_book_types": ["textbook"],
        "prefer_plain_pdf": True,
        "default_semester_scope": "both",
        "high_school": "all_textbook_volumes",
        "subjects_without_match": ["信息学"],
    },
    "merge": {
        "strategy": "binary_concat",
        "chunk_suffix": ".pdf.N",
        "order": "numeric_suffix",
        "readme": "https://github.com/one-leaf/ChinaTextbook#问题如何合并被拆分的文件",
        "note": "README 推荐 mergePDFs 程序，或 Windows 的 copy /b、macOS/Linux 的 cat。本目录按同样顺序做字节拼接。",
    },
    "stats": stats,
    "books": records,
    "other_files": other,
}

OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
OUT_JSON.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

lines = [
    "# ChinaTextbook 教材清单",
    "",
    "- 来源：`one-leaf/ChinaTextbook`，上游 `TapXWorld/ChinaTextbook`",
    f"- 分支：`master`，提交：`{sha}`",
    "- 生成日期：2026-10-22",
    f"- 图书条目：{stats['books']}，其中拆分书 {stats['split_books']} 本，未拆分 {stats['plain_books']} 本",
    f"- 总大小：{stats['total_bytes'] / 1024 / 1024 / 1024:.2f} GB",
    "- 机器可读清单：[china_textbook_catalog.json](../../core/templates/textbooks/china_textbook_catalog.json)",
    "",
]
lines.append("## 资料类型")
lines.append("")
lines.append("| book_type | 数量 |")
lines.append("|-----------|------|")
for type_name, count in Counter(record["book_type"] for record in records).most_common():
    lines.append(f"| {type_name} | {count} |")
lines.extend([
    "",
    "## 拆分与合并",
    "",
    "超过约 50MB 的文件被拆成 `.pdf.1`、`.pdf.2` 等分片。按仓库 README：将分片按序号合并为 `.pdf`；Windows 可用 `copy /b`，macOS/Linux 可用 `cat`，或使用 `mergePDFs` 程序。本清单按数字序号记录分片顺序。",
    "",
    "## 按学段与学科统计",
    "",
    "| 学段 | 学科 | 图书数 |",
    "|------|------|--------|",
])
for (stage, subject), count in Counter((record["stage"], record["subject"]) for record in records).most_common():
    lines.append(f"| {stage} | {subject} | {count} |")

lines.extend([
    "",
    "## 最大的 15 本书",
    "",
    "| 大小 | 分片 | 路径 |",
    "|------|------|------|",
])
for record in sorted(records, key=lambda item: item["total_size"], reverse=True)[:15]:
    lines.append(
        f"| {record['total_size'] / 1024 / 1024:.1f} MB | {record['chunk_count']} | `{record['source_path']}` |"
    )

lines.extend([
    "",
    "## 说明",
    "",
    "- 完整清单以 JSON 为准；本文只做汇总。",
    "- 自动下载默认只选 `book_type=textbook` 的课本，不下载教师用书、练习册、图册、读本和试卷。",
    "- 高中教材以“必修/选择性必修/选修 + 册次”标识，不直接绑定年级；选科后默认下载该学科该版本的全部课本，或由用户在预览中勾选。",
    "- 教材版权归原出版方，仓库未声明单独许可证；下载和使用前请确认授权范围。",
])

OUT_MD.parent.mkdir(parents=True, exist_ok=True)
OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

print(json.dumps(stats, ensure_ascii=False, indent=2))
print("catalog bytes", OUT_JSON.stat().st_size)
print("sample")
for record in records[:8]:
    print(
        record["stage"], record["subject"], record["edition"], record["grade"],
        record["semester"], record["title"], "split=", record["split"],
        "chunks=", record["chunk_count"],
    )
