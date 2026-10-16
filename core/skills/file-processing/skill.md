---
name: File Processing & Document Parsing
description: 各类文件格式（PDF/DOCX/XLSX/PPTX/CSV/HTML/XML/图片/压缩包）的解析与处理选库指南，优先解析质量与首选库。Use when user mentions 文件处理、文档解析、解析 PDF、读取文件、文件格式、file parsing。
roles: [master, worker, lite]
---

# File Processing & Document Parsing Skill

## 1. Purpose

You have access to a Python execution environment and may need to read, analyze, transform, or extract content from files such as:

* PDF
* Microsoft Word (`.doc`, `.docx`)
* Microsoft Excel (`.xls`, `.xlsx`, `.xlsb`)
* PowerPoint (`.ppt`, `.pptx`)
* CSV / TSV
* TXT / Markdown
* HTML / XML
* ZIP and other archive formats
* Images

When processing files, **prioritize parsing quality and correctness over avoiding dependency installation**.

Do not arbitrarily switch to a less capable Python package merely because the preferred package is not currently installed.

> **End-to-end Office workflows live in dedicated skills**: this skill only sets the parsing-library baseline ("which library to parse with"). For the full create / edit / convert / verify workflow and domain conventions, read the `word` (docx), `excel` (xlsx), `powerpoint` (pptx), and `pdf` skills. Legacy `.doc` / `.xls` / `.xlsb` / `.ppt`: if Office / WPS is installed locally, **convert via COM to the new format first** (see `references/office.md`); do not parse with a read-only engine directly.

---

# 2. Core Rules

## Rule 1 — Use the preferred library first

For each file type, use the recommended library defined in this skill.

Do not randomly select a replacement library simply because another package happens to be installed.

Preferred libraries are selected based on:

* parsing accuracy
* format compatibility
* preservation of document structure
* table extraction quality
* performance
* maintenance and ecosystem maturity
* ability to handle real-world files

---

## Rule 2 — Missing dependency → install it

If the preferred library is not installed:

1. Check whether the package is available.
2. If missing, install it using `pip`.
3. Verify that the installation succeeded.
4. Import the package again.
5. Continue processing with the preferred library.

For example:

```python
try:
    import fitz
except ImportError:
    import subprocess
    import sys

    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "PyMuPDF"
    ])

    import fitz
```

Do NOT immediately switch to another library just because `PyMuPDF` is not installed.

---

## Rule 3 — Installation is part of normal file processing

Installing a missing Python dependency is considered a normal operation when it is necessary to perform the requested task.

The agent should not treat package installation as an exceptional failure.

Preferred workflow:

```text
identify file
    ↓
identify required parsing capability
    ↓
select preferred library
    ↓
check whether installed
    ↓
missing?
 ┌──┴──┐
yes    no
 ↓      ↓
install  import
 ↓      ↓
verify   process
 └──┬────┘
    ↓
process file
```

---

# 3. Decision Table

| File        | Primary library       | Typical use       |
| ----------- | --------------------- | ----------------- |
| PDF         | PyMuPDF               | text/pages/images |
| PDF table   | pdfplumber / Camelot  | table extraction  |
| Scanned PDF | PyMuPDF + OCR         | OCR               |
| DOCX        | python-docx           | Word documents    |
| DOC         | Office COM → DOCX     | legacy Word (no COM: LibreOffice/parsers) |
| XLSX        | openpyxl              | workbook/cells    |
| XLSX data   | pandas + openpyxl     | data analysis     |
| XLS         | Office COM → XLSX     | legacy Excel (no COM: xlrd) |
| XLSB        | Office COM → XLSX     | binary Excel (no COM: pyxlsb) |
| CSV         | pandas / csv          | tabular data      |
| PPTX        | python-pptx           | PowerPoint        |
| PPT         | Office COM → PPTX     | legacy PowerPoint (no COM: LibreOffice) |
| HTML        | BeautifulSoup4 / lxml | HTML              |
| XML         | ElementTree / lxml    | XML               |
| JSON        | json                  | JSON              |
| Image       | Pillow                | image processing  |
| ZIP         | zipfile               | archives          |

---

# 4. Fallback Strategy

Fallback libraries are allowed, but only **after the preferred approach has failed**.

Correct:

```text
Preferred library
      ↓
not installed
      ↓
install
      ↓
verify
      ↓
process
      ↓
processing failure?
      ↓
investigate
      ↓
specialized fallback
```

Incorrect:

```text
Preferred library missing
      ↓
randomly choose installed library
```

For example, if `PyMuPDF` is missing:

```text
BAD:
PyMuPDF missing → use PyPDF2 immediately

GOOD:
PyMuPDF missing → pip install PyMuPDF → use PyMuPDF
```

---

# 5. Match the Tool to the Actual Requirement

The preferred library depends not only on the file extension but also on the requested operation.

For example:

### PDF text extraction

```text
PyMuPDF
```

### PDF table extraction

```text
pdfplumber / Camelot
```

### Scanned PDF

```text
PyMuPDF + OCR
```

### DOCX text extraction

```text
python-docx
```

### XLSX data analysis

```text
pandas + openpyxl
```

### XLSX workbook structure manipulation

```text
openpyxl
```

### PPTX text extraction

```text
python-pptx
```

Do not assume that one library is optimal for every operation on the same file format.

---

# 6. General Principle

The goal is not:

> "Use whatever package happens to already be installed."

The goal is:

> **"Use the most appropriate mature library for the requested file-processing task, and install the required dependency when it is missing."**

Dependency installation is preferable to silently degrading parsing quality.

When multiple libraries are suitable, choose the one that provides the best combination of:

1. correctness
2. structure preservation
3. extraction quality
4. compatibility
5. performance
6. maintainability

Only use a fallback when the preferred solution genuinely cannot be used.

---

# Phase map

Detailed procedures live in sub-files — read the one the current task needs:

| Sub-file | When to read |
|---|---|
| `references/pdf.md` | PDF extraction / tables / scanned OCR / long-doc-to-memory / PDF→Word |
| `references/office.md` | Word/Excel/PowerPoint parsing, legacy COM conversion, Office→PDF |
| `references/formats.md` | CSV/HTML/XML/JSON/images/archives, large-file chunking |
| `references/installation.md` | install commands, import-vs-pip names, install failure handling |
| `references/report-latex.md` | report/document generation (LaTeX → PDF preferred) |
