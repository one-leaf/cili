---
name: Word 文档处理
description: Word / .docx / .doc 文档的读取、创建、编辑、排版与转换。Use when user mentions Word 文档、docx、doc、合同、公文、信函、简历，或要求生成可编辑的 Word 交付物。
roles: [master, worker, lite]
---

# Word Document Processing

## Mental Model

A `.docx` file is a ZIP archive of OOXML (Office Open XML) files. Pick a layer by task complexity:

```
python-docx high-level API  → 80% of tasks: read, edit, create (paragraphs/headings/tables/images/styles)
unpack → edit XML → repack   → precise structural ops: revisions, comments, merged cells, image refs, custom XML
pwsh + Word COM              → lossless conversion: .doc→.docx, .docx→PDF (Office installed; LibreOffice unavailable)
pdf2markdown tool            → complex tables/formulas/scanned-doc parsing (see file-processing skill)
```

**Key structure**: after unzipping, `word/document.xml` is the body, `word/media/` holds images, `[Content_Types].xml` declares media types, and `word/_rels/document.xml.rels` records resource relationships such as images.

**Local environment**: `python-docx` installed; Word COM available; LibreOffice / pandoc not on PATH. Parsing-library selection details follow the `file-processing` skill.

## Task → Approach

| Task | Approach |
|------|----------|
| Read full text / structure | `python` tool + `python-docx` (paragraphs, headings, tables, headers/footers) |
| Quick plain-text extraction | `markitdown` (`python -m markitdown file.docx`) |
| Complex tables/formulas/scanned docs | `pdf2markdown` tool |
| Create a new document | `python-docx`: paragraphs, built-in Heading styles, tables, images |
| Edit in place | open with `python-docx` → edit → save |
| Revisions/comments/merged cells/deep XML | unpack → edit XML → repack |
| Legacy `.doc` | Word COM → `.docx` first, then python-docx (never just rename) |
| To PDF | Word COM (`SaveAs` format 17) |
| Formal reports/papers | `latex` tool preferred; generate editable Word only when the user explicitly asks |

## Reading

```python
from docx import Document
doc = Document(path)
for p in doc.paragraphs: print(p.text)
for t in doc.tables:
    for row in t.rows: print([c.text for c in row.cells])
```

- TOC/heading levels: check `p.style.name` (`Heading 1`/`Heading 2`…).
- Long documents: avoid printing everything at once; read in segments, and for oversized content write to a temp file and read it in chunks (see file-processing skill).

## Creating

Append piece by piece with the core API:

```python
from docx import Document
doc = Document()
doc.add_heading('标题', level=1)      # use built-in Heading styles so TOC/outline work
doc.add_paragraph('正文', style='List Bullet')  # use list styles, not hand-typed "•"
t = doc.add_table(rows=2, cols=3, style='Table Grid')
t.cell(0, 0).text = 'A1'
doc.save('out.docx')
```

**Points**:
- Titles always use built-in Heading styles (never fake them with custom styles), otherwise the TOC breaks.
- Lists use `List Bullet` / `List Number` styles; never hand-type bullet characters.
- Headers/footers: `doc.sections[0].header / .footer`; page numbers use fields (`add_page_number` requires constructing a fldSimple).
- Images: `doc.add_picture('img.png', width=Inches(2.5))`.
- Page breaks: insert before a paragraph rather than hard newlines.
- Chinese fonts: after setting `run.font.name`, also set `qn('w:eastAsia')` to cover East Asian fonts.

## Editing Existing Documents

**Routine text/style edits**: open with python-docx → iterate paragraphs/tables and change `run.text` / `cell.text` → save. A paragraph may be split across multiple `run`s, so full-text find/replace must assemble across runs.

**Precise structural operations** (revisions, comments, merged cells, images, deep formatting):
1. Unzip: `python` tool with `zipfile` into a temp directory.
2. Edit `word/document.xml` (images also require updating `word/media/` + `_rels` + `[Content_Types].xml`, all three).
3. Repack into `.docx` (`zipfile`; ensure `[Content_Types].xml` is the first entry).
4. Verify it opens and the content is correct with python-docx or `read`.

**Revision marks**: `<w:ins>` (insert) / `<w:del>` (delete) wrap `<w:r>`; a deleted paragraph needs `<w:del/>` added to `<w:pPr><w:rPr>` or an empty paragraph remains. Comments use `<w:commentRangeStart/>` / `<w:commentRangeEnd/>` + `<w:commentReference/>`.

## Legacy .doc: Convert to .docx First

**On `.doc`: first check whether Office is installed locally; if so, convert to `.docx` via Word COM before the steps below** — `.doc` is a legacy binary format that `python-docx` cannot read directly.

```
pwsh tool: New-Object Word.Application → Documents.Open(input.doc) → SaveAs2(output.docx, 16=wdFormatXMLDocument) → Close → Quit → ReleaseComObject
```

Verify it opens with `python-docx` after conversion. **Never rename `.doc` to `.docx`** (content corruption). When Office is not installed, fall back to LibreOffice conversion (not installed here) or a dedicated `.doc` parser, and honestly report the conversion quality loss.

## Conversion

**`.docx` → PDF** (Word COM, format 17=wdFormatPDF): same structure, `SaveAs2(out.pdf, 17)`.

**Formal reports prefer the `latex` tool** (typesetting quality far above docx; supports formulas, TOC, references, and Chinese via ctex); generate docx only when the user explicitly wants an editable Word file.

## Common Pitfalls

- Renaming `.doc` to `.docx` → won't open. Convert first.
- Text in one paragraph scattered across multiple `run`s; replacement misses half.
- Forgetting to set the East Asian font; Chinese renders with a wrong fallback.
- Editing XML then forgetting `[Content_Types].xml` or `_rels` → Word reports a corrupt file.
