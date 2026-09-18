---
name: PowerPoint 演示文稿处理
description: PowerPoint / .pptx / .ppt 演示文稿的读取、创建、编辑与视觉检查。Use when user mentions PPT、演示文稿、幻灯片、pptx、路演、汇报、幻灯片。
roles: [master, worker, lite]
---

# PowerPoint Deck Processing

## Mental Model

A deck's value is in **visual quality**, not just correct text. The flow is "build → render to images → self-check → fix → re-verify", with at least one fix-verify round before delivery:

```
python-pptx        → read, create, edit (text/shapes/tables/images/notes)
markitdown         → quick full-text extraction (includes placeholder-residue checks)
template-driven vs from-scratch → follow an existing template/reference layout and style strictly; only design from scratch when none exists
pptx→PDF→images    → export PDF via PowerPoint COM, render with pdftoppm, self-check with read_image + subagent review
```

**Local environment**: `python-pptx` / `markitdown` installed; PowerPoint COM available; LibreOffice unavailable → PDF export goes through COM. Parsing-library selection details follow the `file-processing` skill.

## Task → Approach

| Task | Approach |
|------|----------|
| Extract full text / speaker notes | `python -m markitdown file.pptx` |
| Read structure (shapes/text/tables/images) | `python-pptx` |
| Create from scratch | `python-pptx` + the design rules below |
| Edit from a template/reference | `python-pptx` to change text and content, **keeping the original layout and style** |
| Split/merge/deep XML | `zipfile` unpack → edit `ppt/slides/slideN.xml` → repack |
| Legacy `.ppt` | convert to `.pptx` via PowerPoint COM first (see below) |
| To PDF | PowerPoint COM `ExportAsFixedFormat` |
| Visual self-check | render images + `read_image` + fresh-eyes subagent review |

## Reading

```python
from pptx import Presentation
prs = Presentation('f.pptx')
for slide in prs.slides:
    for shape in slide.shapes:
        if shape.has_text_frame:
            print(shape.text)
        if shape.has_table:
            for row in shape.table.rows: print([c.text for c in row.cells])
```

Speaker notes: `slide.notes_slide.notes_text_frame.text`. Charts are graphic objects; read data from `shape.chart.plots`.

## Creating & Editing

```python
from pptx import Presentation
prs = Presentation()                 # 4:3 by default; prs.slide_width/height adjust to 16:9
layout = prs.slide_layouts[1]        # pick a layout (title + content)
slide = prs.slides.add_slide(layout)
slide.shapes.title.text = '标题'
box = slide.shapes.add_textbox(..., top=...); box.text_frame.text = '正文'
slide.shapes.add_table(rows, cols, l, t, w, h)
slide.shapes.add_picture('img.png', left, top, width=...)
prs.save('out.pptx')
```

**Points**:
- Coordinates/sizes are in EMU (914400 = 1 inch); boxes that are too large or overlapping are top items for the visual check.
- Text-box padding misaligns text with shape edges → set `margin: 0` or offset the shape to compensate.
- Template edits: locate each shape (by `shape.name` or a text anchor), change only the target text/image, don't rebuild the layout.
- Don't hand-assemble complex layouts in python-pptx; reuse the same layout across slides for consistency.

## Design Rules (when designing from scratch)

**Color**: pick colors tied to the topic, not the default generic blue. One dominant color carries 60-70% of visual weight, plus 1-2 supporting colors and 1 accent color; colors must be topic-specific (they only feel right if swapping the topic breaks them).

**Light/dark**: title/end slides on dark backgrounds, content slides light ("sandwich"), or an all-dark premium look throughout.

**Every slide has a visual element**: image, chart, icon, or shape. A text-only slide fails. Layouts to choose from: two-column (left text right image), icon + text rows, 2x2 grid, half-bleed image + content overlay, big-number emphasis (60-72pt number + small label).

**Fonts**: title font (e.g. Georgia/Arial Black) paired with a body font (e.g. Calibri); title 36-44pt, section 20-24pt, body 14-16pt, caption 10-12pt.

**Spacing**: ≥0.5" from edges, 0.3-0.5" between content blocks, consistent spacing throughout.

**Avoid**: repeating the same layout end-to-end; centered body text (body left-aligned, only titles centered); a decorative rule under the title (a typical AI artifact); low-contrast text/icons; light text on light backgrounds.

## Visual Check Loop (required before delivery)

1. **Render**: export PDF via PowerPoint COM → `pdftoppm -jpeg -r 150 out.pdf slide` to render images.
2. **Self-check**: use `read_image` on each one, checking overlapping elements, overflow/truncated text, spacing <0.3", insufficient margins, inconsistent alignment, low contrast, residual placeholders.
3. **Subagent review**: before delivery, delegate a worker via `agent` for a fresh-eyes visual check (give it image paths and expected content); a subagent without prior assumptions spots problems more easily.
4. **Fix → re-verify**: after fixing, re-render the affected slides and check again — one fix often causes another issue. Not done until at least one fix-verify round completes.

**Placeholder-residue check**: `python -m markitdown out.pptx | grep -iE "xxxx|lorem|ipsum|占位"` — fix any hits first.

## Legacy .ppt: Convert to .pptx First

**On `.ppt`: first check whether Office is installed locally; if so, convert to `.pptx` via PowerPoint COM before the steps above** — `python-pptx` does not support binary `.ppt`.

```
pwsh tool: New-Object PowerPoint.Application → Presentations.Open(input.ppt) → SaveAs(output.pptx, 24=ppSaveAsOpenXMLPresentation) → Close → Quit → ReleaseComObject
```

Layout, text, images, and notes are all preserved after conversion; then process with python-pptx. **Never rename `.ppt` to `.pptx`**. Without Office, fall back to LibreOffice conversion (not installed here) and honestly report the quality loss.

## Conversion

**To PDF** (PowerPoint COM, `ExportAsFixedFormat(out.pdf)`): absolute paths, `Visible=false`, and release COM objects afterward to keep processes from hanging.

## Common Pitfalls

- Checking text only, never the render — text correct but visually broken (overlap/overflow) still fails.
- Reusing one layout but forgetting per-slide style consistency; or switching layouts every slide, fragmenting the deck.
- Distorted images: `add_picture` with only a width scales height proportionally; giving both width and height can distort.
