---
name: Excel 表格处理
description: Excel / .xlsx / .xls / .xlsb / .csv 表格的读取、分析、创建、公式、格式化与转换。Use when user mentions Excel、表格、xlsx、xls、数据表、财务报表、统计、公式、透视。
roles: [master, worker, lite]
---

# Excel Spreadsheet Processing

## Mental Model

Pick a layer by goal — don't rely on a single toolkit:

```
pandas             → data analysis: read tables, filter, aggregate, statistics, transform
openpyxl           → structural ops: cells, formulas, styles, merges, column widths, multiple sheets
Formulas first     → anything computable in the deliverable is an Excel formula (=SUM etc.), never hardcoded results
Excel COM (pwsh)   → recalculate formulas into cached values, export to PDF, legacy .xls compat (Office installed; LibreOffice unavailable)
pdf2markdown tool  → parsing table/formula-dense documents (see file-processing skill)
```

**Core principle — formulas first**: a delivered `.xlsx` should be "alive" — totals, shares, growth rates, and averages are all written as formulas referencing source cells, so Excel recalculates automatically when source data changes. Hardcoding computed results is wrong.

**Local environment**: `openpyxl` / `pandas` / `xlrd` installed; `pyxlsb` not installed (pip install it before using `.xlsb`); Excel COM available; LibreOffice unavailable → formula recalculation and PDF export go through COM. Parsing-library selection details follow the `file-processing` skill.

## Task → Approach

| Task | Approach |
|------|----------|
| Read/analyze/aggregate/statistics | `pandas` (`read_excel`) |
| Create workbook, write formulas, style | `openpyxl` |
| Edit existing workbook (preserve formulas/format) | `openpyxl` `load_workbook` |
| Multiple worksheets | `wb.sheetnames` / `wb[sheet]` |
| Legacy `.xls` | Excel COM → `.xlsx` first (see below); `xlrd` only without Office |
| `.xlsb` binary | Excel COM → `.xlsx` first; `pyxlsb` only without Office |
| `.csv` / `.tsv` | `pandas` `read_csv` (large files: `chunksize`) |
| Recalculate / materialize cached values | Excel COM open → save |
| To PDF | Excel COM `ExportAsFixedFormat` |

## Reading & Analysis

```python
import pandas as pd
df = pd.read_excel('f.xlsx')                 # first sheet by default
sheets = pd.read_excel('f.xlsx', sheet_name=None)  # all sheets → dict
df.head(); df.info(); df.describe()
```

- Be explicit about types: `dtype={'id': str}`, dates `parse_dates=['col']`, to avoid leading zeros/dates being inferred wrong.
- Large files: `usecols` to read only needed columns, or `read_only=True`.
- Very large CSVs use `chunksize=100000` chunked processing.

## Creating & Editing

```python
from openpyxl import Workbook, load_workbook
wb = Workbook(); ws = wb.active
ws['A1'] = '标题'
ws.append(['行', '数据'])
ws['B10'] = '=SUM(B2:B9)'          # formulas first
ws.column_dimensions['A'].width = 20
wb.save('out.xlsx')

wb2 = load_workbook('existing.xlsx')   # editing existing: formulas, styles, merges all preserved
ws2 = wb2['Sheet1']; ws2['A1'] = '新值'
ws2.insert_rows(2); ws2.delete_cols(3)
wb2.create_sheet('NewSheet')
wb2.save('modified.xlsx')
```

**Points**:
- Write data once; every derivable column is a formula (totals, subtotals, shares, growth rates, averages).
- Assumptions (growth rate, margin, multiple) go in their own cells and formulas reference them instead of inlining values: `=B5*(1+$B$6)` rather than `=B5*1.05`.
- Headers state the unit ("Revenue (万元)"), and years/IDs use text format so they don't become numbers.
- Set borders, fills, freeze panes, merged cells explicitly as needed.

## Formula Recalculation (required before delivery)

`openpyxl` writes formula strings but does not calculate them nor store cached values — another program reading with `data_only=True` gets `None`. Before delivery, open the file once with Excel COM to recalculate and save:

```
pwsh tool: New-Object Excel.Application → Workbooks.Open → CalculateFull → Save → Close → Quit → ReleaseComObject
```

(LibreOffice is unavailable; the only reliable recalculation engine here is Excel itself.)

## Number Formats & Visual Conventions

- Currency: `#,##0`, unit stated in the header.
- Percent: `0.0%`; zeros display as `-`: `0.0%;-0.0%;"-"`.
- Negatives in parentheses `(123)` rather than `-123`: `#,##0;(#,##0)`.
- Financial model color coding (when no template): blue text = editable input assumption, black = formula-calculated, green = cross-sheet reference, red = external link, yellow fill = assumption needing attention.
- Respect existing templates: if the user supplied a template, follow its formatting strictly; don't impose new conventions.

## Pitfalls

- **Index offset**: Excel is 1-based (row=1,col=1 = A1); pandas DataFrames are 0-based.
- `data_only=True` reads cached values, but saving with it open drops formulas — keep data_only off when you need to preserve formulas.
- Column names carry past Z (column 64 is BL, not BK); use `openpyxl.utils.get_column_letter` for conversions.
- Formula validation before delivery: check for `#REF!`/`#DIV/0!`/`#VALUE!`/`#NAME?`, especially cross-sheet references (`Sheet1!A1`) and division by zero.

## Legacy .xls / .xlsb: Convert to .xlsx First

**On `.xls` / `.xlsb`: first check whether Office is installed locally; if so, convert to `.xlsx` via Excel COM before the steps above** — conversion preserves formulas, styles, merges, and multiple sheets, so openpyxl can read/write losslessly.

```
pwsh tool: New-Object Excel.Application → Workbooks.Open(input.xls) → SaveAs(output.xlsx, 51=xlOpenXMLWorkbook) → Close → Quit → ReleaseComObject
```

Verify the result opens in openpyxl. Only without Office do you fall back to the read-only engines `xlrd` (`.xls`) / `pyxlsb` (`.xlsb`), which cannot preserve all formatting. **Never change the extension to fake `.xlsx`**.

## Conversion

**To PDF** (Excel COM, `ExportAsFixedFormat(0, out.pdf)`); export specific sheets only by selecting them first (`Select()`). Paths must be absolute, `Visible=false`, `DisplayAlerts=false`, and release COM objects afterward to keep Excel processes from hanging.
