# PDF (file-processing)

## PDF content extraction

### Recommended: pdf2markdown Tool

For high-quality PDF content extraction (especially documents with tables, formulas, complex layouts), **use the `pdf2markdown` tool first**:

```
pdf2markdown(file_path="document.pdf")
```

Advantages:
- Uses MinerU OCR API, supports table and formula recognition
- Automatically handles scanned and image-based PDFs
- Outputs structured Markdown format
- Supports PDF, images, DOCX, PPTX, XLSX, HTML

Two API modes:
- Agent API: Free, no key required, ≤10MB/≤20 pages
- Precision API: Requires `system.mineru_api_key`, ≤200MB/≤200 pages

### Fallback: PyMuPDF

When `pdf2markdown` is unavailable or local processing is needed, use PyMuPDF:

```python
import fitz
```

Use PyMuPDF as the default library for:

* extracting PDF text
* reading pages
* extracting images
* inspecting page dimensions
* rendering PDF pages
* basic PDF structure analysis
* PDF metadata
* searching text
* page-level processing

Example:

```python
import fitz

doc = fitz.open(pdf_path)

for page in doc:
    text = page.get_text()
```

### Why PyMuPDF

PyMuPDF is generally the preferred first choice because it is:

* fast
* mature
* feature-rich
* suitable for large PDFs
* good at page-level processing
* capable of extracting text, images, and document metadata

---

## PDF Tables

For PDFs containing structured tables, use a specialized table extraction library when necessary.

Preferred order:

1. `pdfplumber`
2. `camelot`
3. `tabula-py` when appropriate

Use:

```text
pdfplumber
```

when the PDF contains selectable text and table boundaries can be inferred from the PDF structure.

Use:

```text
camelot
```

when the PDF contains well-structured tables and Camelot's extraction modes are appropriate.

Use:

```text
tabula-py
```

when Java-based Tabula extraction is specifically useful.

Do not replace PyMuPDF globally just because a PDF contains tables.

A good workflow is:

```text
PyMuPDF → understand/extract document
              ↓
        table detected?
          ↓ yes
      pdfplumber/Camelot
```

---

## Scanned PDF / Image-only PDF

PyMuPDF alone cannot reliably extract text from image-only PDFs.

Detect whether pages contain meaningful text.

If a PDF is scanned/image-only:

1. Render pages as images using PyMuPDF.
2. Perform OCR.
3. Prefer `pytesseract` if Tesseract is available.
4. If another OCR engine is already available and substantially better suited to the document, use it.

Example:

```python
import fitz
import pytesseract
from PIL import Image
```

Do not repeatedly attempt normal PDF text extraction from a scanned document when it clearly contains no text layer.

---

## Long Document to Memory (Complete Content Storage)

When the user requests storing document/file content into memory (knowledge base), ensure the content is complete — never store truncated portions.

### Recommended Workflow: Use pdf2markdown Tool

For PDF, image, DOCX, PPTX, XLSX and other document types, **use the `pdf2markdown` tool to convert to Markdown first**:

```
1. pdf2markdown(file_path="document.pdf")  → outputs document.md
2. read(file_path="document.md")            → read Markdown content
3. memory(action='store', content=full)     → store into knowledge base
```

Advantages:
- Automatically handles tables, formulas, complex layouts
- OCR recognition for scanned documents
- Structured Markdown output for easy retrieval and reuse

### Problem

When documents exceed tool output limits (read tool: ~10,000 tokens, python/bash: ~30,000 characters), output is truncated. Storing truncated content directly into memory results in an incomplete knowledge base.

### Correct Workflow

```
User requests "store this file in memory"
    ↓
1. Read file with read tool first
    ↓
2. Check if output was truncated?
   - See "... more lines" → truncated
   - See "… N tokens truncated …" → truncated
    ↓
   Yes → 3. Read in segments (see method below)
   No  → 4. Direct memory(action='store')
    ↓
3. Concatenate all segments
    ↓
4. Single memory(action='store', content=complete)
```

### Segmented Reading Method

```python
# First read: first 2000 lines
read(file_path="document.pdf", offset=1, limit=2000)
# → See "(2000 more lines). Use offset=2001 to continue reading."

# Second read: continue
read(file_path="document.pdf", offset=2001, limit=2000)
# → See "(2000 more lines). Use offset=4001 to continue reading."

# Repeat until all lines are read
```

For documents parsed with python tool (e.g., PDF):
1. First use python to extract full text and save as temp file
2. Then use read tool to read the temp file in segments

```python
# python tool: extract full text
import fitz, os
doc = fitz.open("long_document.pdf")
full_text = ""
for page in doc:
    full_text += page.get_text()
tmp_path = os.path.join(os.environ["TEMP"], "extracted.txt")
with open(tmp_path, "w", encoding="utf-8") as f:
    f.write(full_text)
print(f"Extraction complete, {len(full_text)} characters")
```

Then read the temp file (path printed by Python) in segments.

### Handling Very Long Content

If concatenated content exceeds 50,000 characters, consider:
- Splitting into multiple memory files by chapter/topic
- Using different `topic` and `filename` values to distinguish

---

## PDF to Word Workflow

When the user requests converting PDF to Word (DOCX) format, use the following workflow:

### Recommended Approach

```
PDF
 ↓
pdf2markdown(file_path="document.pdf")  → outputs document.md
 ↓
read(file_path="document.md")           → read Markdown content
 ↓
python tool: convert Markdown to DOCX
```

### Implementation Example

```python
from docx import Document
import re

# Read Markdown content
with open("document.md", "r", encoding="utf-8") as f:
    md_content = f.read()

# Create Word document
doc = Document()

# Simple Markdown → DOCX conversion
lines = md_content.split('\n')
for line in lines:
    if line.startswith('# '):
        doc.add_heading(line[2:], level=1)
    elif line.startswith('## '):
        doc.add_heading(line[3:], level=2)
    elif line.startswith('### '):
        doc.add_heading(line[4:], level=3)
    elif line.strip():
        doc.add_paragraph(line)

doc.save("output.docx")
```

### Notes

- Tables in Markdown can be converted using `python-docx`'s `add_table()` method
- Images need separate handling (download/extract first, then insert into Word)
- Complex formatting (e.g., formulas) may require additional processing
