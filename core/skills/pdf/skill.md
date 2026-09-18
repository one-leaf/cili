---
name: PDF 文档处理
description: PDF 文件的读取、提取、创建、合并、拆分、旋转、加密、水印与 OCR。Use when user mentions PDF、pdf、扫描件、合并/拆分 PDF、提取 PDF 内容、PDF 表单、PDF 转 Word。
roles: [master, worker, lite]
---

# PDF 文档处理

## 处理思路（心智模型）

PDF 是"显示定稿"格式，不是编辑格式——**读取靠抽取，修改靠重建**。按需求选路径：

```
pdf2markdown 工具     → 首选内容抽取：表格/公式/扫描件，在线 MinerU（免费/Precision 两级，自动降级）
fitz (PyMuPDF)        → 本地文本/页面/图片/元数据/渲染，快且强
pdfplumber            → 本地表格抽取（有文本层的 PDF）
PyPDF2                → 合并/拆分/旋转/加密/水印/页级重建
latex 工具            → 创建正式 PDF（排版质量最高，支持公式/目录/中文）
reportlab             → 程序化创建简单 PDF（页眉/多页/表格）
texlive poppler CLI   → pdftotext/pdftoppm/pdfimages/pdfunite/pdfseparate（本机在 PATH）
Office COM (pwsh)     → 把 Word/Excel/PPT 转成 PDF（见 word/excel/powerpoint 技能）
```

**本机环境**：`pdf2markdown`（在线）、`fitz`/`pdfplumber`/`PyPDF2`/`reportlab`/`pypdfium2` 已装；`latex` 工具可用（xelatex/pdflatex/tectonic）；texlive 的 poppler 工具在 PATH。解析库选型细节遵循 `file-processing` 技能。

## 任务 → 方法

| 需求 | 做法 |
|------|------|
| 抽取正文/表格/公式/扫描件 | `pdf2markdown` 工具（首选，输出 .md） |
| 本地抽文本/页面/图片/元数据 | `fitz`（`page.get_text()`，分页处理） |
| 抽取有文本层的表格 | `pdfplumber`（`page.extract_tables()`） |
| 合并 / 拆分 / 旋转 | `PyPDF2`（或 CLI `pdfunite`/`pdfseparate`） |
| 加密 / 解密 / 水印 | `PyPDF2` |
| 提取内嵌图片 | CLI `pdfimages -j` 或 `fitz` |
| 扫描件 OCR 成可搜索文本 | `pdf2markdown` 工具（在线）；或 `fitz` 渲图 + `pytesseract` |
| 创建正式 PDF | `latex` 工具（报告/论文/公式）；`reportlab`（简单程序化生成） |
| PDF 表单填写 | `PyPDF2` 写入字段 |
| PDF 转 Word | pdf2markdown → .md → python-docx（见 file-processing §23） |

## 读取与文本提取

```python
import fitz
doc = fitz.open('f.pdf')
for page in doc:                      # 分页处理，避免整篇占内存
    print(page.get_text())
```

- 元数据/页数：`doc.page_count`、`doc.metadata`。
- 长文档：全文写到临时文件，再用 `read` 工具分段读（见 file-processing §22）。
- 快速纯文本：CLI `pdftotext -layout f.pdf out.txt`。
- 渲染页面为图片：`fitz` `page.get_pixmap()` 或 CLI `pdftoppm -jpeg -r 150 f.pdf page`。

## 表格提取

有文本层的表格用 `pdfplumber`：

```python
import pdfplumber
with pdfplumber.open('f.pdf') as pdf:
    for page in pdf.pages:
        for table in page.extract_tables():
            ...
```

表格复杂/无文本层时，直接交给 `pdf2markdown` 工具（表格识别更强）。

## 扫描件 / 图片 PDF

`pdf2markdown` 工具自动 OCR（首选）。本地备选：`fitz` 逐页渲图 → `pytesseract` OCR。先判断 PDF 是否有文本层（`page.get_text()` 是否为空），空再走 OCR，不要对正常 PDF 盲目 OCR。

## 创建 PDF

**正式报告/论文/公式 → `latex` 工具**：写 `.tex`（中文用 `ctexart`，编译选 `xelatex`）→ `latex(action='compile', file='report.tex')`。目录/参考文献要多遍编译。详见 file-processing §25 的模板。

**程序化生成简单 PDF → `reportlab`**：

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

**坑**：reportlab 内置字体没有 Unicode 下标/上标字形（会渲染成黑块），要用 Paragraph 的 `<sub>`/`<super>` XML 标签；中文需注册中文字体或走 latex。

## 合并 / 拆分 / 旋转 / 加密 / 水印

```python
from PyPDF2 import PdfReader, PdfWriter
# 合并
w = PdfWriter()
for f in ['a.pdf', 'b.pdf']:
    for p in PdfReader(f).pages: w.add_page(p)
with open('merged.pdf', 'wb') as f: w.write(f)
# 拆分
r = PdfReader('in.pdf')
w = PdfWriter(); w.add_page(r.pages[0])
with open('page1.pdf', 'wb') as f: w.write(f)
# 旋转
r.pages[0].rotate(90)
# 加密
w.encrypt('userpw', 'ownerpw')
# 水印：把水印页 merge_page 到每页
```

CLI 备选：`pdfunite a.pdf b.pdf merged.pdf`、`pdfseparate in.pdf page-%d.pdf`。注意本机是 `PyPDF2`（旧名），`import PyPDF2`；新版项目更名 `pypdf`，API 相同。

## 提取图片

CLI `pdfimages -j in.pdf prefix`（得到 `prefix-000.jpg`…）或 `fitz` 按页面/区域抽取。

## PDF 表单

填写：`PyPDF2` 读 `reader.get_fields()` 找字段名，`writer.update_page_form_field_values` 写入。前提是 PDF 有表单域（AcroForm）；扫描件表单无法直接填，需另做布局分析或转 Word/图片再填。

## 常见坑

- 把 PDF 当可编辑格式原地改——只能抽取后重建。
- 扫描件直接 `get_text()` 得到空串，不判断文本层就"提取失败"；先探测再决定是否 OCR。
- `pdftotext` 乱序/缺布局——用 `-layout`，或换 pdfplumber/fitz。
- 密码保护的 PDF：`PdfReader` 需传 `password`；解密用 `w.decrypt(pw)` 再另存。
- 大 PDF 整篇读入内存——分页处理。
