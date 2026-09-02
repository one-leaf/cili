---
name: File Processing & Document Parsing
description: Guidelines for parsing and processing various file formats (PDF, DOCX, XLSX, PPTX, CSV, HTML, XML, images, archives). Prioritizes parsing quality and using preferred libraries.
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

# 3. File Type → Preferred Python Libraries

## 3.1 PDF

### 推荐方案：pdf2markdown 工具

对于需要高质量提取 PDF 内容（尤其是包含表格、公式、复杂排版的文档），**优先使用 `pdf2markdown` 工具**：

```
pdf2markdown(file_path="document.pdf")
```

优势：
- 使用 MinerU OCR API，支持表格、公式识别
- 自动处理扫描件和图片型 PDF
- 输出结构化的 Markdown 格式
- 支持 PDF、图片、DOCX、PPTX、XLSX、HTML

两种 API 模式：
- Agent API：免费无需密钥，≤10MB/≤20页
- Precision API：需配置 `system.mineru_api_key`，≤200MB/≤200页

### 备用方案：PyMuPDF

当 `pdf2markdown` 不可用或需要本地处理时，使用 PyMuPDF：

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

## 3.2 PDF Tables

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

## 3.3 Scanned PDF / Image-only PDF

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

# 4. Microsoft Word

## 4.1 DOCX

### Primary library: python-docx

Package:

```text
python-docx
```

Import:

```python
from docx import Document
```

Use for:

* paragraphs
* headings
* tables
* runs
* basic document structure
* styles
* headers/footers
* document metadata

Example:

```python
from docx import Document

doc = Document(path)

for paragraph in doc.paragraphs:
    print(paragraph.text)

for table in doc.tables:
    for row in table.rows:
        print([cell.text for cell in row.cells])
```

If `python-docx` is missing, install it.

Do not switch immediately to ZIP/XML parsing merely to avoid installing `python-docx`.

---

## 4.2 Legacy DOC

`.doc` is the old binary Microsoft Word format and is fundamentally different from `.docx`.

`python-docx` does NOT directly support `.doc`.

Preferred strategy:

```text
.doc
 ↓
LibreOffice conversion to .docx
 ↓
python-docx
```

If LibreOffice is available, convert:

```text
DOC → DOCX
```

and then process the resulting DOCX using `python-docx`.

If LibreOffice is unavailable, investigate whether an appropriate legacy DOC parser is installed or install an appropriate conversion/parser dependency.

Do not simply rename:

```text
file.doc → file.docx
```

This is invalid.

---

# 5. Microsoft Excel

## 5.1 XLSX

### Primary library: openpyxl

Package:

```text
openpyxl
```

Import:

```python
import openpyxl
```

Use for:

* worksheets
* cells
* formulas
* styles
* merged cells
* dimensions
* workbook structure

Example:

```python
import openpyxl

wb = openpyxl.load_workbook(path, data_only=False)

for ws in wb.worksheets:
    for row in ws.iter_rows(values_only=True):
        print(row)
```

---

## 5.2 Excel Data Analysis

When the primary goal is:

* reading tabular data
* filtering
* aggregation
* statistics
* transformation
* DataFrame-based analysis

prefer:

```text
pandas
```

with:

```python
pd.read_excel(...)
```

Use the appropriate engine:

```text
.xlsx → openpyxl
.xls  → xlrd
.xlsb → pyxlsb
```

Example:

```python
import pandas as pd

df = pd.read_excel(path, engine="openpyxl")
```

If `pandas` or the required engine is missing, install it.

---

## 5.3 XLS

Legacy `.xls` files should use:

```text
xlrd
```

Do not attempt to use `openpyxl` for `.xls`.

Example:

```python
import pandas as pd

df = pd.read_excel(path, engine="xlrd")
```

If `xlrd` is missing:

```bash
pip install xlrd
```

---

## 5.4 XLSB

For Excel Binary Workbook:

```text
.xlsb
```

prefer:

```text
pyxlsb
```

Example:

```python
import pandas as pd

df = pd.read_excel(path, engine="pyxlsb")
```

---

# 6. CSV / TSV

Use:

```text
pandas
```

for structured/tabular CSV processing.

```python
import pandas as pd

df = pd.read_csv(path)
```

For very large CSV files, do not load the entire file into memory.

Use chunked processing:

```python
for chunk in pd.read_csv(path, chunksize=100000):
    process(chunk)
```

For simple line-oriented processing where pandas is unnecessary, Python's built-in `csv` module is sufficient.

---

# 7. PowerPoint

## PPTX

Preferred library:

```text
python-pptx
```

Import:

```python
from pptx import Presentation
```

Use for:

* slides
* text
* tables
* shapes
* images
* slide structure

If missing, install:

```bash
pip install python-pptx
```

---

## Legacy PPT

`.ppt` is a legacy binary PowerPoint format.

Preferred strategy:

```text
PPT
 ↓
LibreOffice conversion
 ↓
PPTX
 ↓
python-pptx
```

---

# 8. HTML

For HTML parsing:

### Primary

```text
BeautifulSoup4
```

```python
from bs4 import BeautifulSoup
```

For complex HTML extraction, consider:

```text
lxml
```

Do not use regular expressions as the primary HTML parser.

---

# 9. XML

Prefer:

### Standard XML

Python's built-in:

```python
import xml.etree.ElementTree as ET
```

For more advanced XML processing:

```text
lxml
```

Use `lxml` when:

* XPath is required
* namespaces are complex
* performance matters
* advanced XML processing is needed

---

# 10. JSON

Use Python's built-in:

```python
import json
```

Do not install a third-party package unnecessarily.

---

# 11. Images

For image metadata and basic image processing:

```text
Pillow
```

Import:

```python
from PIL import Image
```

Use Pillow for:

* opening images
* resizing
* format conversion
* metadata
* basic image processing

For OCR, use an OCR-specific library rather than Pillow alone.

---

# 12. ZIP / Archive

Use Python's standard library whenever possible:

```python
import zipfile
```

Do not install third-party packages unless the requested archive format requires them.

---

# 13. Dependency Installation Policy

## 13.1 Installation command

Use the current Python interpreter:

```python
import subprocess
import sys

subprocess.check_call([
    sys.executable,
    "-m",
    "pip",
    "install",
    "PACKAGE_NAME"
])
```

This is preferred over:

```bash
pip install PACKAGE_NAME
```

because it guarantees that the package is installed into the Python environment being used by the tool.

---

## 13.2 Install only what is needed

Do not blindly install a large collection of packages.

Install the dependency required by the current task.

For example:

```text
PDF → PyMuPDF
DOCX → python-docx
XLSX → openpyxl
PPTX → python-pptx
CSV → pandas
OCR → pytesseract + OCR engine
```

---

## 13.3 Verify installation

After installation, immediately verify the import:

```python
try:
    import fitz
except ImportError as e:
    raise RuntimeError(
        "PyMuPDF installation failed or is unavailable."
    ) from e
```

Do not assume `pip` returning successfully means the package can necessarily be imported.

---

# 14. Fallback Strategy

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

# 15. Match the Tool to the Actual Requirement

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

# 16. Large File Processing

When processing large files, do not unnecessarily load the entire file into memory.

## PDF

Process page-by-page:

```python
for page in doc:
    process(page)
```

## Excel

For large XLSX files, consider:

```python
openpyxl.load_workbook(
    path,
    read_only=True,
    data_only=True
)
```

For large tabular data, prefer:

```python
pd.read_csv(
    path,
    chunksize=100000
)
```

## DOCX

Process paragraphs/tables incrementally where practical.

---

# 17. Do Not Use Weak Substitutes Just to Avoid Dependencies

Avoid these patterns:

### Bad

```text
Need DOCX
↓
python-docx unavailable
↓
unzip DOCX manually
↓
parse XML manually
```

unless there is a specific reason that direct XML processing is required.

### Bad

```text
Need PDF
↓
PyMuPDF unavailable
↓
use an unrelated lightweight PDF library
```

### Bad

```text
Need XLSX
↓
openpyxl unavailable
↓
manually parse ZIP/XML
```

### Good

```text
Need DOCX
↓
python-docx missing
↓
install python-docx
↓
use python-docx
```

Manual parsing should be considered a specialized fallback, not the default solution.

---

# 18. Environment and Package Discovery

Before installing, you may check whether a suitable package is already available:

```python
import importlib.util

if importlib.util.find_spec("fitz") is None:
    # install PyMuPDF
    ...
```

Remember that **import name and pip package name may differ**.

Examples:

| Function | pip package      | Python import |
| -------- | ---------------- | ------------- |
| PDF      | `PyMuPDF`        | `fitz`        |
| DOCX     | `python-docx`    | `docx`        |
| XLSX     | `openpyxl`       | `openpyxl`    |
| XLS      | `xlrd`           | `xlrd`        |
| XLSB     | `pyxlsb`         | `pyxlsb`      |
| PPTX     | `python-pptx`    | `pptx`        |
| HTML     | `beautifulsoup4` | `bs4`         |
| Image    | `Pillow`         | `PIL`         |

Never assume the pip package name is identical to the import name.

---

# 19. Package Installation Failure

If installation fails:

1. Capture the actual installation error.
2. Determine whether:

   * network access is unavailable
   * the package requires a system dependency
   * the Python version is incompatible
   * the package name is incorrect
   * the environment does not permit installation
3. Try a compatible installation strategy.
4. Only then consider a fallback library.

Do not silently switch libraries and produce a lower-quality result.

If no viable solution exists, clearly report:

```text
Required dependency:
X

Installation result:
Y

Fallback attempted:
Z

Remaining limitation:
...
```

---

# 20. File Processing Decision Table

| File        | Primary library       | Typical use       |
| ----------- | --------------------- | ----------------- |
| PDF         | PyMuPDF               | text/pages/images |
| PDF table   | pdfplumber / Camelot  | table extraction  |
| Scanned PDF | PyMuPDF + OCR         | OCR               |
| DOCX        | python-docx           | Word documents    |
| DOC         | LibreOffice → DOCX    | legacy Word       |
| XLSX        | openpyxl              | workbook/cells    |
| XLSX data   | pandas + openpyxl     | data analysis     |
| XLS         | xlrd                  | legacy Excel      |
| XLSB        | pyxlsb                | binary Excel      |
| CSV         | pandas / csv          | tabular data      |
| PPTX        | python-pptx           | PowerPoint        |
| PPT         | LibreOffice → PPTX    | legacy PowerPoint |
| HTML        | BeautifulSoup4 / lxml | HTML              |
| XML         | ElementTree / lxml    | XML               |
| JSON        | json                  | JSON              |
| Image       | Pillow                | image processing  |
| ZIP         | zipfile               | archives          |

---

# 21. General Principle

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

# 22. 长文档转 Memory（完整内容存储）

当用户要求将文档/文件内容存入 memory（知识库）时，必须确保内容完整，不能只存储截断的部分。

## 推荐流程：使用 pdf2markdown 工具

对于 PDF、图片、DOCX、PPTX、XLSX 等文档，**优先使用 `pdf2markdown` 工具转换为 Markdown**：

```
1. pdf2markdown(file_path="document.pdf")  → 输出 document.md
2. read(file_path="document.md")            → 读取 Markdown 内容
3. memory(action='store', content=完整内容) → 存入知识库
```

优势：
- 自动处理表格、公式、复杂排版
- 扫描件也能 OCR 识别
- 输出结构化的 Markdown，便于后续检索和使用

## 问题

文档超过工具输出限制（read 工具约 10,000 tokens，python/bash 工具约 30,000 字符）时，工具会截断输出。如果直接将截断的内容存入 memory，会导致知识库不完整。

## 正确流程

```
用户要求"把这个文件存入 memory"
    ↓
1. 先用 read 工具读取文件
    ↓
2. 检查输出是否被截断？
   - 看到 "... more lines" → 被截断
   - 看到 "… N tokens truncated …" → 被截断
    ↓
   是 → 3. 分多次读取（见下方方法）
   否 → 4. 直接 memory(action='store')
    ↓
3. 拼接所有分段内容
    ↓
4. 一次性 memory(action='store', content=完整内容)
```

## 分次读取方法

```python
# 第一次：读前 2000 行
read(file_path="文档.pdf", offset=1, limit=2000)
# → 看到 "(2000 more lines). Use offset=2001 to continue reading."

# 第二次：继续读
read(file_path="文档.pdf", offset=2001, limit=2000)
# → 看到 "(2000 more lines). Use offset=4001 to continue reading."

# 重复直到读完所有行
```

对于 python 工具解析的文档（如 PDF），可以：
1. 先用 python 提取完整文本并保存为临时文件
2. 然后用 read 工具分次读取该临时文件

```python
# python 工具：提取完整文本
import fitz
doc = fitz.open("长文档.pdf")
full_text = ""
for page in doc:
    full_text += page.get_text()
with open("/tmp/extracted.txt", "w", encoding="utf-8") as f:
    f.write(full_text)
print(f"提取完成，共 {len(full_text)} 字符")
```

然后分次读取 `/tmp/extracted.txt`。

## 超长内容的处理

如果拼接后内容超过 50,000 字符，考虑：
- 按章节/主题分成多个 memory 文件
- 使用不同的 `topic` 和 `filename` 区分

---

# 23. PDF 转 Word 工作流

当用户要求将 PDF 转换为 Word（DOCX）格式时，推荐以下流程：

## 推荐方案

```
PDF
 ↓
pdf2markdown(file_path="document.pdf")  → 输出 document.md
 ↓
read(file_path="document.md")           → 读取 Markdown 内容
 ↓
python 工具：将 Markdown 转为 DOCX
```

## 实现示例

```python
from docx import Document
import re

# 读取 Markdown 内容
with open("document.md", "r", encoding="utf-8") as f:
    md_content = f.read()

# 创建 Word 文档
doc = Document()

# 简单的 Markdown → DOCX 转换
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

## 注意事项

- Markdown 中的表格可以用 `python-docx` 的 `add_table()` 方法转换
- 图片需要单独处理（先下载/提取，再插入 Word）
- 复杂格式（如公式）可能需要额外处理
