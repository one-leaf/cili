# Office formats (file-processing)

## Microsoft Word

### DOCX — python-docx

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

### Legacy DOC

`.doc` is the old binary Microsoft Word format and is fundamentally different from `.docx`.

`python-docx` does NOT directly support `.doc`.

**Preferred strategy — Office COM first** (Microsoft Office / WPS is commonly installed locally; LibreOffice is rarely used in this region):

```text
.doc
 ↓
Word COM (pwsh) → .docx
 ↓
python-docx
```

If Microsoft Office (or WPS) is installed, convert via COM — highest fidelity (formulas, styles, comments all preserved):

```text
pwsh tool: New-Object Word.Application → Documents.Open(input.doc) → SaveAs2(output.docx, 16) → Close → Quit → ReleaseComObject
```

Only when no Office COM is available, fall back to LibreOffice conversion or a legacy `.doc` parser, and report the quality loss.

Do not simply rename:

```text
file.doc → file.docx
```

This is invalid.

For the full Word workflow (create/edit/convert/verify), read the `word` skill.

---

## Microsoft Excel

### XLSX — openpyxl

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

### Excel Data Analysis

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

### Legacy XLS

Legacy `.xls` is a binary format; `openpyxl` cannot read it directly.

**Preferred strategy — Office COM first** (preserves formulas, styles, merged cells, sheets):

```text
.xls
 ↓
Excel COM (pwsh) → .xlsx
 ↓
openpyxl / pandas
```

```text
pwsh tool: New-Object Excel.Application → Workbooks.Open(input.xls) → SaveAs(output.xlsx, 51) → Close → Quit → ReleaseComObject
```

Only when no Office COM is available, fall back to the read-only engine:

```python
import pandas as pd

df = pd.read_excel(path, engine="xlrd")
```

If `xlrd` is missing:

```bash
pip install xlrd
```

For the full Excel workflow (create/edit/formulas/verify), read the `excel` skill.

### XLSB

For Excel Binary Workbook:

```text
.xlsb
```

**Preferred strategy — Office COM first**: Excel opens `.xlsb` natively; convert to `.xlsx` via COM (same as XLS above), then use `openpyxl` / `pandas`. Only when no Office COM is available, read with the binary engine `pyxlsb`:

```python
import pandas as pd

df = pd.read_excel(path, engine="pyxlsb")
```

---

## PowerPoint

### PPTX

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

### Legacy PPT

`.ppt` is a legacy binary PowerPoint format.

**Preferred strategy — Office COM first** (preserves layouts, text, images, notes):

```text
PPT
 ↓
PowerPoint COM (pwsh) → .pptx
 ↓
python-pptx
```

```text
pwsh tool: New-Object PowerPoint.Application → Presentations.Open(input.ppt) → SaveAs(output.pptx, 24) → Close → Quit → ReleaseComObject
```

Only when no Office COM is available, fall back to LibreOffice conversion and report the quality loss.

For the full PowerPoint workflow (create/edit/visual QC), read the `powerpoint` skill.

---

## Word/Excel to PDF Workflow

When the user requests converting Word (DOCX/DOC) or Excel (XLSX/XLS) to PDF format.

### Priority Order

1. **pwsh + Microsoft Office COM** — Best quality, most common
2. **pwsh + WPS COM** — Alternative if WPS Office is installed
3. **Python libraries** — Limited quality, fallback only

### Method 1: pwsh + Microsoft Office COM (Preferred)

Use the `pwsh` tool to invoke Microsoft Office via COM automation. This produces the highest quality PDF with perfect formatting preservation.

Word to PDF via Office COM:

```powershell
pwsh(command="
$word = New-Object -ComObject Word.Application
$word.Visible = $false
$doc = $word.Documents.Open('C:\\path\\to\\document.docx')
$doc.SaveAs([ref]'C:\\path\\to\\output.pdf', [ref]17)  # 17 = wdFormatPDF
$doc.Close()
$word.Quit()
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($word) | Out-Null
")
```

Excel to PDF via Office COM:

```powershell
pwsh(command="
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
$wb = $excel.Workbooks.Open('C:\\path\\to\\spreadsheet.xlsx')
$wb.ExportAsFixedFormat(0, 'C:\\path\\to\\output.pdf')  # 0 = xlTypePDF
$wb.Close($false)
$excel.Quit()
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($excel) | Out-Null
")
```

Excel: specific sheets to PDF:

```powershell
pwsh(command="
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
$wb = $excel.Workbooks.Open('C:\\path\\to\\spreadsheet.xlsx')

# Select specific sheets
$sheets = @('Sheet1', 'Sheet2')
$wb.Worksheets.Item($sheets).Select()

# Export selected sheets
$wb.ExportAsFixedFormat(0, 'C:\\path\\to\\output.pdf')
$wb.Close($false)
$excel.Quit()
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($excel) | Out-Null
")
```

Advantages of Office COM method:

- **Perfect formatting**: Uses actual Office rendering engine
- **All features supported**: Charts, PivotTables, conditional formatting, etc.
- **Macros/VBA**: If the document has macros, they execute normally
- **Fonts**: Uses installed system fonts correctly

### Method 2: pwsh + WPS Office COM (Alternative)

If WPS Office is installed instead of Microsoft Office, use WPS COM objects.

| Application | COM Object |
|-------------|------------|
| WPS 文字 (Writer) | `KWps.Application` or `WPS.Application` |
| WPS 表格 (Spreadsheet) | `KET.Application` |
| WPS 演示 (Presentation) | `KWPP.Application` |

Word to PDF via WPS COM:

```powershell
pwsh(command="
$wps = New-Object -ComObject KWps.Application
$wps.Visible = $false
$doc = $wps.Documents.Open('C:\\path\\to\\document.docx')
$doc.ExportAsFixedFormat('C:\\path\\to\\output.pdf', 17)  # 17 = wdFormatPDF
$doc.Close()
$wps.Quit()
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($wps) | Out-Null
")
```

Excel to PDF via WPS COM:

```powershell
pwsh(command="
$ket = New-Object -ComObject KET.Application
$ket.Visible = $false
$ket.DisplayAlerts = $false
$wb = $ket.Workbooks.Open('C:\\path\\to\\spreadsheet.xlsx')
$wb.ExportAsFixedFormat(0, 'C:\\path\\to\\output.pdf')  # 0 = xlTypePDF
$wb.Close($false)
$ket.Quit()
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($ket) | Out-Null
")
```

Auto-detect Office vs WPS:

```powershell
pwsh(command="
function ConvertTo-PDF {
    param([string]$InputFile, [string]$OutputFile)

    $ext = [System.IO.Path]::GetExtension($InputFile).ToLower()

    if ($ext -in '.doc', '.docx') {
        # Try Office first, then WPS
        try {
            $app = New-Object -ComObject Word.Application
            $app.Visible = $false
            $doc = $app.Documents.Open($InputFile)
            $doc.SaveAs([ref]$OutputFile, [ref]17)
            $doc.Close()
            $app.Quit()
            [System.Runtime.Interopservices.Marshal]::ReleaseComObject($app) | Out-Null
            Write-Host 'Converted using Microsoft Word'
        } catch {
            $app = New-Object -ComObject KWps.Application
            $app.Visible = $false
            $doc = $app.Documents.Open($InputFile)
            $doc.ExportAsFixedFormat($OutputFile, 17)
            $doc.Close()
            $app.Quit()
            [System.Runtime.Interopservices.Marshal]::ReleaseComObject($app) | Out-Null
            Write-Host 'Converted using WPS Writer'
        }
    }
    elseif ($ext -in '.xls', '.xlsx') {
        # Try Office first, then WPS
        try {
            $app = New-Object -ComObject Excel.Application
            $app.Visible = $false
            $app.DisplayAlerts = $false
            $wb = $app.Workbooks.Open($InputFile)
            $wb.ExportAsFixedFormat(0, $OutputFile)
            $wb.Close($false)
            $app.Quit()
            [System.Runtime.Interopservices.Marshal]::ReleaseComObject($app) | Out-Null
            Write-Host 'Converted using Microsoft Excel'
        } catch {
            $app = New-Object -ComObject KET.Application
            $app.Visible = $false
            $app.DisplayAlerts = $false
            $wb = $app.Workbooks.Open($InputFile)
            $wb.ExportAsFixedFormat(0, $OutputFile)
            $wb.Close($false)
            $app.Quit()
            [System.Runtime.Interopservices.Marshal]::ReleaseComObject($app) | Out-Null
            Write-Host 'Converted using WPS Spreadsheet'
        }
    }
}

ConvertTo-PDF -InputFile 'C:\\path\\to\\input.docx' -OutputFile 'C:\\path\\to\\output.pdf'
")
```

### Method 3: Python Libraries (Last Resort)

Only use when neither Office nor WPS is available.

DOCX to PDF:

```python
# Requires docx2pdf (which needs Office installed anyway)
from docx2pdf import convert
convert('input.docx', 'output.pdf')
```

XLSX to PDF:

```python
# Very limited - use reportlab or fpdf for simple tables
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

df = pd.read_excel('input.xlsx')
data = [df.columns.tolist()] + df.values.tolist()

doc = SimpleDocTemplate('output.pdf', pagesize=A4)
table = Table(data)
table.setStyle(TableStyle([
    ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
    ('GRID', (0, 0), (-1, -1), 1, colors.black),
]))
doc.build([table])
```

### Decision Flow

```
User requests "convert to PDF"
    ↓
Try Microsoft Office COM first
    ↓
   Success? → Done
    ↓
   Failed (Office not installed)
    ↓
Try WPS Office COM
    ↓
   Success? → Done
    ↓
   Failed (WPS not installed)
    ↓
Use Python library (limited quality)
```

### Important Notes

- **Always use `pwsh` tool for COM calls**, never bash (bash cannot invoke COM)
- **Absolute paths required** for COM — convert relative paths to absolute first
- **Release COM objects** to prevent Office/WPS processes from hanging
- **Set Visible = $false** to run Office/WPS in background
- **DisplayAlerts = $false** suppresses dialog prompts (Excel/WPS Spreadsheet)
- **WPS COM names differ**: `KWps` (Writer), `KET` (Spreadsheet), `KWPP` (Presentation)
