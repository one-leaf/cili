"""Render a courseware plan JSON into a single self-contained HTML file.

Usage:
    python render_html.py plan.json out.html [--workspace-root DIR] [--workspace-uuid UUID]

Design: the model only writes JSON. This script owns layout, escaping, assets,
diagrams, illustrations and KaTeX inlining.
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import mimetypes
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
TEMPLATE_DIR = SKILL_DIR / "templates" / "parchment-v1"
ASSETS_DIR = SKILL_DIR / "assets"

sys.path.insert(0, str(SCRIPT_DIR))
import render_diagrams  # noqa: E402
import render_illustrations  # noqa: E402

LAYOUTS = {
    "cover", "objectives", "section", "concept", "steps",
    "two-column", "diagram", "quiz", "summary",
}
BLOCK_TYPES = {
    "text", "bullets", "formula", "callout", "table", "image",
    "diagram", "illustration", "mistake", "code",
}
VISUAL_TYPES = {"image", "diagram", "illustration"}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def esc_json(value: Any) -> str:
    return html.escape(json.dumps(value, ensure_ascii=False), quote=True)


def render_inline(text: str) -> str:
    """Escape text, rendering $...$ as KaTeX placeholders."""
    text = "" if text is None else str(text)
    parts: list[str] = []
    pos = 0
    for match in re.finditer(r"\$([^$\n]+)\$", text):
        parts.append(esc(text[pos:match.start()]))
        parts.append(f'<span class="cw-formula" data-tex="{esc(match.group(1))}"></span>')
        pos = match.end()
    parts.append(esc(text[pos:]))
    return "".join(parts)


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if mime.startswith("image/") and mime != "image/svg+xml":
        try:
            from PIL import Image

            with Image.open(path) as img:
                img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
                img.thumbnail((1600, 1600))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=82, optimize=True)
                return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            pass
    raw = path.read_bytes()
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def resolve_image(raw: str, plan_dir: Path, workspace_root: Path | None) -> Path | None:
    candidates: list[Path] = []
    p = Path(raw)
    if p.is_absolute():
        candidates.append(p)
    if workspace_root is not None:
        candidates.append(workspace_root / raw)
    candidates.append(plan_dir / raw)
    candidates.append(Path.cwd() / raw)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def mascot_svg() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">'
        '<ellipse cx="150" cy="264" rx="92" ry="16" fill="#f7c548" stroke="#8b6b4a" stroke-width="4"/>'
        '<path d="M76 138 Q76 92 150 92 Q224 92 224 138 L224 232 Q224 252 204 252 L96 252 Q76 252 76 232 Z" fill="#fff6df" stroke="#4a3423" stroke-width="5"/>'
        '<line x1="150" y1="96" x2="150" y2="248" stroke="#8b6b4a" stroke-width="4"/>'
        '<path d="M98 150 Q120 142 140 150 M160 150 Q182 142 204 150 M98 186 Q120 178 140 186 M160 186 Q182 178 204 186" fill="none" stroke="#8b6b4a" stroke-width="4" stroke-linecap="round"/>'
        '<circle cx="122" cy="214" r="6" fill="#4a3423"/><circle cx="178" cy="214" r="6" fill="#4a3423"/>'
        '<path d="M136 228 Q150 238 164 228" fill="none" stroke="#4a3423" stroke-width="4" stroke-linecap="round"/>'
        '<g transform="rotate(18 232 120)"><rect x="220" y="56" width="24" height="92" rx="8" fill="#f2812f" stroke="#4a3423" stroke-width="4"/>'
        '<path d="M220 148 L232 176 L244 148 Z" fill="#fff1c9" stroke="#4a3423" stroke-width="4"/>'
        '<rect x="220" y="56" width="24" height="18" rx="6" fill="#e4572e" stroke="#4a3423" stroke-width="4"/></g>'
        '<path d="M60 74 L66 88 L80 94 L66 100 L60 114 L54 100 L40 94 L54 88 Z" fill="#f7c548" stroke="#8b6b4a" stroke-width="3"/>'
        '<path d="M244 204 L248 214 L258 218 L248 222 L244 232 L240 222 L230 218 L240 214 Z" fill="#f7c548" stroke="#8b6b4a" stroke-width="3"/>'
        "</svg>"
    )


def render_block(block: dict[str, Any], plan_dir: Path, workspace_root: Path | None) -> str:
    kind = str(block.get("type", ""))
    if kind == "text":
        style = str(block.get("style", "body"))
        cls = "cw-text" + (" cw-text-lead" if style == "lead" else " cw-text-note" if style == "note" else "")
        return f'<p class="{cls}">{render_inline(block.get("text", ""))}</p>'
    if kind == "bullets":
        items = block.get("items") or []
        ordered = bool(block.get("ordered"))
        cls = "cw-bullets cw-bullets-ordered" if ordered else "cw-bullets"
        tag = "ol" if ordered else "ul"
        lis = "".join(f"<li>{render_inline(item)}</li>" for item in items)
        return f'<{tag} class="{cls}">{lis}</{tag}>'
    if kind == "formula":
        display = bool(block.get("display"))
        return (
            f'<span class="cw-formula{" cw-formula-display" if display else ""}" '
            f'data-tex="{esc(block.get("latex", ""))}" data-display="{1 if display else 0}"></span>'
        )
    if kind == "callout":
        tone = str(block.get("tone", "tip"))
        cls = "cw-callout" + (" cw-callout-warn" if tone == "warn" else " cw-callout-key" if tone == "key" else "")
        return f'<div class="{cls}"><div>{render_inline(block.get("text", ""))}</div></div>'
    if kind == "table":
        headers = block.get("headers") or []
        rows = block.get("rows") or []
        caption = block.get("caption")
        head = "".join(f"<th>{esc(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows)
        cap = f"<caption>{esc(caption)}</caption>" if caption else ""
        return f'<div class="cw-table-wrap"><table class="cw-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody>{cap}</table></div>'
    if kind == "image":
        raw = str(block.get("path", ""))
        resolved = resolve_image(raw, plan_dir, workspace_root)
        caption = block.get("caption")
        cap_html = f"<figcaption>{esc(caption)}</figcaption>" if caption else ""
        if resolved is None:
            return f'<figure class="cw-figure"><div class="cw-callout cw-callout-warn"><div>图片缺失：{esc(raw)}</div></div>{cap_html}</figure>'
        try:
            src = data_uri(resolved)
        except Exception:
            return f'<figure class="cw-figure"><div class="cw-callout cw-callout-warn"><div>图片无法读取：{esc(raw)}</div></div>{cap_html}</figure>'
        return f'<figure class="cw-figure"><img src="{src}" alt="{esc(block.get("alt", ""))}">{cap_html}</figure>'
    if kind == "diagram":
        svg = render_diagrams.render(str(block.get("kind", "")), block.get("params") or {})
        caption = block.get("caption")
        cap_html = f"<figcaption>{esc(caption)}</figcaption>" if caption else ""
        return f'<figure class="cw-figure">{svg}{cap_html}</figure>'
    if kind == "illustration":
        svg = render_illustrations.render(str(block.get("kind", "")), block.get("params") or {})
        caption = block.get("caption")
        cap_html = f"<figcaption>{esc(caption)}</figcaption>" if caption else ""
        return f'<figure class="cw-figure">{svg}{cap_html}</figure>'
    if kind == "mistake":
        rows = [
            ("题目", block.get("question", "")),
            ("我的答案", block.get("my_answer", "")),
            ("正确答案", block.get("correct_answer", "")),
            ("解析", block.get("analysis", "")),
        ]
        html_rows = []
        for label, value in rows:
            cls = "cw-mistake-wrong" if label == "我的答案" else "cw-mistake-right" if label == "正确答案" else ""
            html_rows.append(f'<div class="cw-mistake-row"><b>{label}</b><span class="{cls}">{render_inline(value)}</span></div>')
        source = block.get("source")
        src_html = f'<div class="cw-mistake-note">来源：{esc(source)}</div>' if source else ""
        return f'<div class="cw-mistake"><div class="cw-mistake-title">典型错例 · {esc(block.get("error_type", ""))}</div>{"".join(html_rows)}{src_html}</div>'
    if kind == "code":
        return f'<pre class="cw-code"><code>{esc(block.get("code", ""))}</code></pre>'
    return f'<div class="cw-callout cw-callout-warn"><div>不支持的内容块：{esc(kind)}</div></div>'


def render_blocks(blocks: list[dict[str, Any]], plan_dir: Path, workspace_root: Path | None) -> str:
    return "".join(render_block(b, plan_dir, workspace_root) for b in blocks or [])


def render_objectives(blocks: list[dict[str, Any]], plan_dir: Path, workspace_root: Path | None) -> str:
    items: list[str] = []
    for block in blocks or []:
        if block.get("type") == "bullets":
            items.extend(str(x) for x in (block.get("items") or []))
        elif block.get("type") == "text":
            items.append(str(block.get("text", "")))
        else:
            items.append(render_block(block, plan_dir, workspace_root))
    return "".join(
        f'<div class="cw-objective"><span class="cw-objective-mark"></span><span>{render_inline(item)}</span></div>'
        for item in items
    )


def render_summary(blocks: list[dict[str, Any]], plan_dir: Path, workspace_root: Path | None) -> tuple[str, str]:
    items: list[str] = []
    note = ""
    for block in blocks or []:
        if block.get("type") == "bullets":
            items.extend(str(x) for x in (block.get("items") or []))
        elif block.get("type") in ("text", "callout"):
            if block.get("type") == "text" and not note:
                note = str(block.get("text", ""))
            elif block.get("type") == "callout":
                note = str(block.get("text", ""))
            else:
                items.append(str(block.get("text", "")))
        else:
            items.append(render_block(block, plan_dir, workspace_root))
    body = "".join(f'<div class="cw-summary-item"><span>{render_inline(x)}</span></div>' for x in items)
    return body, note


def render_question(q: dict[str, Any], scene_id: str, index: int) -> str:
    qid = str(q.get("id") or f"q{index}")
    qtype = str(q.get("type") or "single")
    stem = str(q.get("stem") or "")
    options = [str(x) for x in (q.get("options") or [])]
    answer = q.get("answer")
    explain = str(q.get("explain") or "")
    hint = str(q.get("hint") or "")
    data = (
        f'data-qid="{esc(qid)}" data-scene="{esc(scene_id)}" data-type="{esc(qtype)}" '
        f'data-answer="{esc_json(answer)}" data-options="{esc_json(options)}" '
        f'data-question="{esc(stem)}" data-explain="{esc(explain)}"'
    )
    opts_html = ""
    if qtype in ("single", "multiple"):
        letters = [chr(65 + i) for i in range(len(options))]
        input_type = "radio" if qtype == "single" else "checkbox"
        for letter, opt in zip(letters, options):
            opts_html += (
                f'<label class="cw-option" data-letter="{letter}">'
                f'<input type="{input_type}" name="{esc(scene_id)}-{esc(qid)}" value="{letter}">'
                f'<span class="cw-option-letter">{letter}</span><span>{render_inline(opt)}</span></label>'
            )
        opts_html = f'<div class="cw-quiz-options">{opts_html}</div>'
    else:
        count = len(answer) if isinstance(answer, list) else 1
        inputs = "".join(f'<input type="text" placeholder="第 {i + 1} 空" aria-label="第 {i + 1} 空">' for i in range(count))
        opts_html = f'<div class="cw-fill-row">{inputs}</div>'
    hint_html = f'<div class="cw-mistake-note">提示：{render_inline(hint)}</div>' if hint else ""
    return (
        f'<div class="cw-quiz" {data}>'
        f'<div class="cw-quiz-head"><span class="cw-quiz-index">{index}.</span>'
        f'<p class="cw-quiz-stem">{render_inline(stem)}</p></div>'
        f'{opts_html}{hint_html}'
        f'<div class="cw-quiz-actions">'
        f'<button class="cw-btn cw-check" type="button">检查</button>'
        f'<button class="cw-btn cw-btn-ghost cw-reveal" type="button">显示答案</button>'
        f'<span class="cw-send-status"></span></div>'
        f'<div class="cw-quiz-feedback"></div></div>'
    )


def render_questions(questions: list[dict[str, Any]], scene_id: str) -> str:
    return "".join(render_question(q, scene_id, i + 1) for i, q in enumerate(questions or []))


def split_columns(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    left: list[dict[str, Any]] = []
    right: list[dict[str, Any]] = []
    for block in blocks or []:
        if block.get("type") in VISUAL_TYPES and not right:
            right.append(block)
        else:
            left.append(block)
    return left, right


def render_scene(scene: dict[str, Any], index: int, total: int, plan: dict[str, Any],
                 plan_dir: Path, workspace_root: Path | None) -> str:
    layout = str(scene.get("layout") or "concept")
    title = str(scene.get("title") or "")
    subtitle = str(scene.get("subtitle") or "")
    source = str(scene.get("source") or "")
    notes = str(scene.get("notes") or "")
    subject = str((plan.get("meta") or {}).get("subject") or "")
    blocks = scene.get("blocks") or []
    mapping: dict[str, str] = {
        "TITLE": esc(title),
        "SUBTITLE": esc(subtitle),
        "SUBJECT": esc(subject),
        "SOURCE": esc(source),
        "PAGE": f"{index + 1:02d}",
        "BODY": "",
        "LEFT": "",
        "RIGHT": "",
        "FIGURE": "",
        "CAPTION": "",
        "QUESTIONS": "",
        "SUMMARY_NOTE": "",
        "META_CHIPS": "",
        "MASCOT": mascot_svg(),
    }
    meta = plan.get("meta") or {}
    chips = [meta.get("grade"), meta.get("textbook"), f"{meta.get('duration_min')} 分钟" if meta.get("duration_min") else ""]
    mapping["META_CHIPS"] = "".join(f"<span>{esc(c)}</span>" for c in chips if c)

    if layout == "cover":
        if not subtitle:
            mapping["SUBTITLE"] = "一起把今天的内容学会吧"
    elif layout == "objectives":
        mapping["BODY"] = render_objectives(blocks, plan_dir, workspace_root)
    elif layout == "two-column":
        left, right = split_columns(blocks)
        mapping["LEFT"] = render_blocks(left, plan_dir, workspace_root)
        mapping["RIGHT"] = render_blocks(right, plan_dir, workspace_root)
    elif layout == "diagram":
        diagram = scene.get("diagram") or {}
        mapping["FIGURE"] = render_diagrams.render(str(diagram.get("kind", "")), diagram.get("params") or {})
        mapping["CAPTION"] = esc(diagram.get("caption", ""))
        mapping["BODY"] = render_blocks(blocks, plan_dir, workspace_root)
    elif layout == "quiz":
        mapping["QUESTIONS"] = render_questions(scene.get("questions") or [], str(scene.get("id") or f"s{index + 1}"))
    elif layout == "summary":
        body, note = render_summary(blocks, plan_dir, workspace_root)
        mapping["BODY"] = body or render_blocks(blocks, plan_dir, workspace_root)
        mapping["SUMMARY_NOTE"] = render_inline(note)
    else:
        mapping["BODY"] = render_blocks(blocks, plan_dir, workspace_root)

    template = read_text(TEMPLATE_DIR / "layouts" / f"{layout}.html")
    for key, value in mapping.items():
        template = template.replace("{{" + key + "}}", value)
    notes_html = f'<div class="cw-notes-src" hidden>{esc(notes)}</div>'
    return (
        f'<section class="cw-scene cw-layout-{esc(layout)}" data-index="{index}" data-layout="{esc(layout)}" '
        f'data-scene-id="{esc(scene.get("id", ""))}">{template}{notes_html}</section>'
    )


def inline_katex_css(css: str) -> str:
    fonts_dir = ASSETS_DIR / "katex" / "fonts"

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        path = fonts_dir / name
        if not path.is_file():
            return match.group(0)
        return f"url({data_uri(path)})"

    css = re.sub(r"url\((?:[\"']?)fonts/([^\"')]+)(?:[\"']?)\)", repl, css)
    # Only woff2 files are vendored; drop remaining woff/ttf fallbacks so a
    # single-file export never points at a missing relative font.
    css = re.sub(
        r"url\((?:[\"']?)fonts/[^\"')]+(?:[\"']?)\)(?:\s*format\([^)]*\))?\s*,?",
        "",
        css,
    )
    return css


def build_meta(plan: dict[str, Any], out_path: Path, workspace_uuid: str) -> dict[str, Any]:
    return {
        "workspace_uuid": workspace_uuid,
        "courseware": str((plan.get("meta") or {}).get("title") or out_path.stem),
        "courseware_path": str(out_path),
        "record_url": "/api/courseware/quiz-result",
    }


def validate_plan(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if plan.get("schema_version") != "1.0":
        errors.append("schema_version 必须为 1.0")
    if plan.get("template") != "parchment-v1":
        errors.append("template 必须为 parchment-v1")
    if plan.get("mode") not in ("chapter", "error_type"):
        errors.append("mode 必须为 chapter 或 error_type")
    meta = plan.get("meta") or {}
    for key in ("title", "subject", "grade"):
        if not meta.get(key):
            errors.append(f"meta.{key} 必填")
    scenes = plan.get("scenes") or []
    if not isinstance(scenes, list) or not scenes:
        errors.append("scenes 不能为空")
        return errors
    ids: set[str] = set()
    for i, scene in enumerate(scenes):
        prefix = f"scenes[{i}]"
        if not isinstance(scene, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        sid = str(scene.get("id") or "")
        if not sid:
            errors.append(f"{prefix}.id 必填")
        elif sid in ids:
            errors.append(f"{prefix}.id 重复：{sid}")
        ids.add(sid)
        layout = scene.get("layout")
        if layout not in LAYOUTS:
            errors.append(f"{prefix}.layout 非法：{layout}")
        if layout != "cover" and not scene.get("title"):
            errors.append(f"{prefix}.title 必填")
        if layout == "diagram" and not isinstance(scene.get("diagram"), dict):
            errors.append(f"{prefix}.diagram 必填")
        if layout == "quiz":
            questions = scene.get("questions") or []
            if not questions:
                errors.append(f"{prefix}.questions 不能为空")
            qids: set[str] = set()
            for j, q in enumerate(questions):
                qp = f"{prefix}.questions[{j}]"
                qid = str(q.get("id") or "")
                if not qid:
                    errors.append(f"{qp}.id 必填")
                elif qid in qids:
                    errors.append(f"{qp}.id 重复：{qid}")
                qids.add(qid)
                qtype = q.get("type")
                if qtype not in ("single", "multiple", "fill"):
                    errors.append(f"{qp}.type 非法")
                if not q.get("stem"):
                    errors.append(f"{qp}.stem 必填")
                if qtype in ("single", "multiple"):
                    options = q.get("options") or []
                    if len(options) < 2:
                        errors.append(f"{qp}.options 至少 2 项")
                    answer = q.get("answer")
                    answers = answer if isinstance(answer, list) else [answer]
                    for a in answers:
                        if not isinstance(a, str) or len(a) != 1 or not ("A" <= a <= "Z"):
                            errors.append(f"{qp}.answer 必须是选项字母")
                            break
        for j, block in enumerate(scene.get("blocks") or []):
            bp = f"{prefix}.blocks[{j}]"
            if not isinstance(block, dict):
                errors.append(f"{bp} 必须是对象")
                continue
            btype = block.get("type")
            if btype not in BLOCK_TYPES:
                errors.append(f"{bp}.type 非法：{btype}")
            if btype == "image" and not block.get("path"):
                errors.append(f"{bp}.path 必填")
            if btype in ("diagram", "illustration") and not block.get("kind"):
                errors.append(f"{bp}.kind 必填")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan")
    parser.add_argument("out")
    parser.add_argument("--workspace-root", default="")
    parser.add_argument("--workspace-uuid", default="")
    args = parser.parse_args()

    plan_path = Path(args.plan).resolve()
    out_path = Path(args.out).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    errors = validate_plan(plan)
    if errors:
        print(json.dumps({"success": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 2

    workspace_root = Path(args.workspace_root).resolve() if args.workspace_root else None
    plan_dir = plan_path.parent
    scenes = plan.get("scenes") or []
    body = "".join(
        render_scene(scene, i, len(scenes), plan, plan_dir, workspace_root)
        for i, scene in enumerate(scenes)
    )

    shell = read_text(TEMPLATE_DIR / "shell.html")
    shell_css = read_text(TEMPLATE_DIR / "shell.css")
    shell_js = read_text(TEMPLATE_DIR / "shell.js")
    katex_css = inline_katex_css(read_text(ASSETS_DIR / "katex" / "katex.min.css"))
    katex_js = read_text(ASSETS_DIR / "katex" / "katex.min.js")
    styles = katex_css + "\n" + shell_css
    scripts = katex_js.replace("</script", "<\\/script") + "\n" + shell_js
    meta = build_meta(plan, out_path, args.workspace_uuid)
    meta_json = json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")

    output = shell
    output = output.replace("{{TITLE}}", esc((plan.get("meta") or {}).get("title", "课件")))
    output = output.replace("{{STYLE}}", styles)
    output = output.replace("{{BODY}}", body)
    output = output.replace("{{META_JSON}}", meta_json)
    output = output.replace("{{SCRIPT}}", scripts)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(output, encoding="utf-8")
    size = out_path.stat().st_size
    warnings: list[str] = []
    if size > 15 * 1024 * 1024:
        warnings.append("文件超过 15MB，请压缩图片或拆分课件")
    elif size > 10 * 1024 * 1024:
        warnings.append("文件超过 10MB，预览可能较慢")
    print(json.dumps({
        "success": True,
        "output": str(out_path),
        "pages": len(scenes),
        "bytes": size,
        "warnings": warnings,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
