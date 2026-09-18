---
name: Excel 表格处理
description: Excel / .xlsx / .xls / .xlsb / .csv 表格的读取、分析、创建、公式、格式化与转换。Use when user mentions Excel、表格、xlsx、xls、数据表、财务报表、统计、公式、透视。
roles: [master, worker, lite]
---

# Excel 表格处理

## 处理思路（心智模型）

按目标选层，不要只用一套：

```
pandas             → 数据分析：读表、过滤、聚合、统计、转换
openpyxl           → 结构操作：单元格、公式、样式、合并、列宽、多工作表
公式优先           → 交付物里能算的都用 Excel 公式（=SUM 等），不硬编码结果值
Excel COM (pwsh)   → 公式重算落缓存值、转 PDF、旧版 .xls 兼容（本机已装 Office，LibreOffice 不可用）
pdf2markdown 工具  → 表格/公式密集型文档的解析（另见 file-processing 技能）
```

**核心原则 — 公式优先**：交给用户的 `.xlsx` 应是"活的"——总和、占比、增长率、均值全部写成公式引用源数据单元格，源数据一变 Excel 自动重算。硬编码计算结果是错的。

**本机环境**：`openpyxl` / `pandas` / `xlrd` 已装；`pyxlsb` 未装（.xlsb 用时先 `pip install`）；Excel COM 可用；LibreOffice 不可用 → 公式重算和转 PDF 走 COM。解析库选型细节遵循 `file-processing` 技能。

## 任务 → 方法

| 需求 | 做法 |
|------|------|
| 读表分析/聚合/统计 | `pandas`（`read_excel`） |
| 建表、写公式、设样式 | `openpyxl` |
| 改现有表（保留公式格式） | `openpyxl` `load_workbook` |
| 多个工作表 | `wb.sheetnames` / `wb[sheet]` |
| `.xls` 旧格式 | 先 Excel COM 转 `.xlsx`（见下方），无 Office 才 `xlrd` |
| `.xlsb` 二进制格式 | 先 Excel COM 转 `.xlsx`，无 Office 才 `pyxlsb` |
| `.csv` / `.tsv` | `pandas` `read_csv`（大文件 `chunksize`） |
| 公式重算 / 落缓存值 | Excel COM 打开→保存 |
| 转 PDF | Excel COM `ExportAsFixedFormat` |

## 读取与分析

```python
import pandas as pd
df = pd.read_excel('f.xlsx')                 # 默认第一张表
sheets = pd.read_excel('f.xlsx', sheet_name=None)  # 全部表 → dict
df.head(); df.info(); df.describe()
```

- 类型要显式：`dtype={'id': str}`，日期 `parse_dates=['col']`，避免前导零/日期被推断错。
- 大文件：`usecols` 只读需要的列，或 `read_only=True`。
- CSV 超大文件用 `chunksize=100000` 分块处理。

## 创建与编辑

```python
from openpyxl import Workbook, load_workbook
wb = Workbook(); ws = wb.active
ws['A1'] = '标题'
ws.append(['行', '数据'])
ws['B10'] = '=SUM(B2:B9)'          # 公式优先
ws.column_dimensions['A'].width = 20
wb.save('out.xlsx')

wb2 = load_workbook('existing.xlsx')   # 改现有：公式、样式、合并都会保留
ws2 = wb2['Sheet1']; ws2['A1'] = '新值'
ws2.insert_rows(2); ws2.delete_cols(3)
wb2.create_sheet('NewSheet')
wb2.save('modified.xlsx')
```

**要点**：
- 数据只写一次；能推导的列全部用公式（总计、小计、占比、增长率、均值）。
- 假设（增长率、利润率、倍数）放独立单元格，公式引用它而非写死数值：`=B5*(1+$B$6)` 而不是 `=B5*1.05`。
- 表头标注单位（"Revenue (万元)"），年份/ID 用文本格式防止变成数值。
- 需要边框、底色、冻结窗格、合并单元格时逐项设置。

## 公式重算（交付前必做）

`openpyxl` 写的是公式字符串，**不计算也不落缓存值**——别的程序用 `data_only=True` 读会拿到 `None`。交付前用 Excel COM 打开一次让 Excel 重算并保存：

```
pwsh 工具：New-Object Excel.Application → Workbooks.Open → CalculateFull → Save → Close → Quit → ReleaseComObject
```

（LibreOffice 不可用，本机唯一可靠的重算引擎就是 Excel 本身。）

## 数字格式与视觉规范

- 金额：`#,##0`，表头注明单位。
- 百分比：`0.0%`；零值显示为 `-`：`0.0%;-0.0%;"-"`。
- 负数用括号 `(123)` 而非 `-123`：`#,##0;(#,##0)`。
- 财务模型配色约定（无模板时）：蓝色文本=可改的输入假设、黑色=公式计算、绿色=跨表引用、红色=外部链接、黄底=需关注的假设。
- 保留现有模板：用户给的模板已有格式则严格沿用，不要强加新规范。

## 易错点

- **索引偏移**：Excel 从 1 开始（row=1,col=1 = A1）；pandas DataFrame 从 0 开始。
- `data_only=True` 读缓存值可以，但**用它保存会丢公式**——要保留公式就别开 data_only。
- 列名超 Z 会进位（第 64 列是 BL 不是 BK），列号换算用 `openpyxl.utils.get_column_letter`。
- 公式验证：交付前检查 `#REF!`/`#DIV/0!`/`#VALUE!`/`#NAME?`，特别是跨工作表引用（`Sheet1!A1`）与除零。

## 旧版 .xls / .xlsb 优先转 .xlsx

**遇到 `.xls` / `.xlsb`：先检测本机是否装了 Office，装了就先经 Excel COM 转成 `.xlsx`，再继续上面的处理**——转换后公式、样式、合并、多表全部保留，openpyxl 才能无损读写。

```
pwsh 工具：New-Object Excel.Application → Workbooks.Open(input.xls) → SaveAs(output.xlsx, 51=xlOpenXMLWorkbook) → Close → Quit → ReleaseComObject
```

转换后用 openpyxl 验证能打开。本机未装 Office 时才退回只读引擎 `xlrd`（.xls）/ `pyxlsb`（.xlsb），但这两个不能保留全部格式。**绝不改后缀冒充 .xlsx**。

## 转换

**转 PDF**（Excel COM，`ExportAsFixedFormat(0, out.pdf)`），可只导指定工作表（先 `Select()` 需要的表）。路径必须绝对路径，`Visible=false`、`DisplayAlerts=false`，用完释放 COM 对象防止 Excel 进程挂起。
