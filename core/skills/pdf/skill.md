---
name: PDF 文档处理
description: PDF 文件的读取、提取、创建、合并、拆分、旋转、加密、水印与 OCR。Use when user mentions PDF、pdf、扫描件、合并/拆分 PDF、提取 PDF 内容、PDF 表单、PDF 转 Word。
roles: [master, worker, lite]
---

# PDF Document Processing

## Mental Model

A PDF is a "display-final" format, not an editing format — **read by extraction, modify by rebuilding**. Pick a path by need:

```
pdf2markdown tool     → first choice for content extraction: tables/formulas/scanned docs, online MinerU (free/Precision tiers, auto-fallback)
fitz (PyMuPDF)        → local text/pages/images/metadata/rendering, fast and powerful
pdfplumber            → local table extraction (PDFs with a text layer)
PyPDF2                → merge/split/rotate/encrypt/watermark/page-level rebuilding
latex tool            → create formal PDFs (best typesetting; formulas, TOC, Chinese)
reportlab             → programmatic simple PDFs (headers/multi-page/tables)
texlive poppler CLI   → pdftotext/pdftoppm/pdfimages/pdfunite/pdfseparate (on PATH)
Office COM (pwsh)     → convert Word/Excel/PPT to PDF (see word/excel/powerpoint skills)
```

**Local environment**: `pdf2markdown` (online), `fitz`/`pdfplumber`/`PyPDF2`/`reportlab`/`pypdfium2` installed; `latex` tool available (xelatex/pdflatex/tectonic); texlive poppler tools on PATH. Parsing-library selection details follow the `file-processing` skill.

## Task → Approach

| Task | Approach |
|------|----------|
| Extract body text/tables/formulas/scanned | `pdf2markdown` tool (first choice; outputs .md) |
| Local text/pages/images/metadata | `fitz` (`page.get_text()`, page by page) |
| Extract tables from a text-layer PDF | `pdfplumber` (`page.extract_tables()`) |
| Merge / split / rotate | `PyPDF2` (or CLI `pdfunite`/`pdfseparate`) |
| Encrypt / decrypt / watermark | `PyPDF2` |
| Extract embedded images | CLI `pdfimages -j` or `fitz` |
| OCR scanned docs into searchable text | `pdf2markdown` tool (online); or `fitz` render + `pytesseract` |
| Create formal PDFs | `latex` tool (reports/papers/formulas); `reportlab` (simple programmatic) |
| Fill PDF forms | `PyPDF2` write field values |
| PDF to Word | pdf2markdown → .md → python-docx (see file-processing §23) |

## Reading & Text Extraction

```python
import fitz
doc = fitz.open('f.pdf')
for page in doc:                      # process page by page to avoid loading everything into memory
    print(page.get_text())
```

- Metadata/page count: `doc.page_count`, `doc.metadata`.
- Long documents: write the full text to a temp file, then read it in segments (see file-processing §22).
- Quick plain text: CLI `pdftotext -layout f.pdf out.txt`.
- Render pages to images: `fitz` `page.get_pixmap()` or CLI `pdftoppm -jpeg -r 150 f.pdf page`.

## Table Extraction

Tables in a text-layer PDF use `pdfplumber`:

```python
import pdfplumber
with pdfplumber.open('f.pdf') as pdf:
    for page in pdf.pages:
        for table in page.extract_tables():
            ...
```

For complex tables or no text layer, hand off to the `pdf2markdown` tool (stronger table recognition).

## Scanned / Image-only PDFs

`pdf2markdown` tool OCRs automatically (first choice). Local fallback: `fitz` render each page → `pytesseract` OCR. First check whether the PDF has a text layer (`page.get_text()` empty?); only OCR when there is none — don't blindly OCR normal PDFs.

## Creating PDFs

**Formal reports/papers/formulas → `latex` tool**: write a `.tex` (Chinese uses `ctexart`; compile with `xelatex`) → `latex(action='compile', file='report.tex')`. TOC/references need multiple compile passes. See the template in file-processing §25.

**Programmatic simple PDFs → `reportlab`**:

```python
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet

doc = SimpleDocTemplate('out.pdf', pagesize=A4)
styles = getSampleStyleSheet()
story = [Paragraph('标题', styles['Title']), Spacer(1, 12),
         Paragraph('正文…', styles['Normal'])]
doc.build(story)
```

**Gotcha**: reportlab's built-in fonts lack Unicode subscript/superscript glyphs (they render as black boxes) — use Paragraph's `<sub>`/`<super>` XML tags; Chinese needs a registered CJK font or the latex path.

## Merge / Split / Rotate / Encrypt / Watermark

```python
from PyPDF2 import PdfReader, PdfWriter
# merge
w = PdfWriter()
for f in ['a.pdf', 'b.pdf']:
    for p in PdfReader(f).pages: w.add_page(p)
with open('merged.pdf', 'wb') as f: w.write(f)
# split
r = PdfReader('in.pdf')
w = PdfWriter(); w.add_page(r.pages[0])
with open('page1.pdf', 'wb') as f: w.write(f)
# rotate
r.pages[0].rotate(90)
# encrypt
w.encrypt('userpw', 'ownerpw')
# watermark: merge_page the watermark page onto every page
```

CLI alternatives: `pdfunite a.pdf b.pdf merged.pdf`, `pdfseparate in.pdf page-%d.pdf`. Note this machine has `PyPDF2` (old name) — `import PyPDF2`; the newer project is renamed `pypdf`, same API.

## Extracting Images

CLI `pdfimages -j in.pdf prefix` (produces `prefix-000.jpg`…) or extract by page/region with `fitz`.

## PDF Forms

Filling: read field names with `PyPDF2` `reader.get_fields()`, write with `writer.update_page_form_field_values`. Requires the PDF to have form fields (AcroForm); scanned forms can't be filled directly — do layout analysis or convert to Word/image first.

## Common Pitfalls

- Treating a PDF as an editable format and editing in place — extract then rebuild.
- `get_text()` on a scanned doc returns empty; calling that "extraction failed" without probing the text layer — probe first, then decide on OCR.
- `pdftotext` out of order / missing layout — use `-layout`, or switch to pdfplumber/fitz.
- Password-protected PDFs: `PdfReader` needs `password`; decrypt with `w.decrypt(pw)` and save again.
- Loading a huge PDF fully into memory — process page by page.
