---
name: Word 文档处理
description: Word / .docx / .doc 文档的读取、创建、编辑、排版与转换。Use when user mentions Word 文档、docx、doc、合同、公文、信函、简历，或要求生成可编辑的 Word 交付物。
roles: [master, worker, lite]
---

# Word 文档处理

## 处理思路（心智模型）

`.docx` 本质是一个 ZIP 压缩包，里面是 OOXML（Office Open XML）文件。按任务复杂度选层：

```
python-docx 高层 API        → 80% 任务：读、改、建（段落/标题/表格/图片/样式）
unpack→改 XML→repack        → 精确结构操作：修订、批注、合并单元格、图片引用、自定义 XML
pwsh + Word COM             → 保真转换：.doc→.docx、.docx→PDF（本机已装 Office，LibreOffice 不可用）
pdf2markdown 工具           → 复杂表格/公式/扫描件的解析（另见 file-processing 技能）
```

**关键结构**：解压后 `word/document.xml` 是正文，`word/media/` 存图片，`[Content_Types].xml` 声明媒体类型，`word/_rels/document.xml.rels` 记录图片等资源的关系。

**本机环境**：`python-docx` 已安装；Word COM 可用；LibreOffice / pandoc 不在 PATH。解析库选型细节遵循 `file-processing` 技能。

## 任务 → 方法

| 需求 | 做法 |
|------|------|
| 读全文/结构 | `python` 工具 + `python-docx`（段落、标题、表格、页眉页脚） |
| 快速提取纯文本 | `markitdown`（`python -m markitdown file.docx`） |
| 复杂表格/公式/扫描件解析 | `pdf2markdown` 工具 |
| 创建新文档 | `python-docx`：段落、内置 Heading 样式、表格、图片 |
| 就地修改内容 | `python-docx` 打开→改→保存 |
| 修订/批注/合并单元格/深层 XML | 解压→编辑 XML→重新打包 |
| `.doc` 旧格式 | Word COM 转 `.docx` 再用 python-docx（不要直接改后缀） |
| 转 PDF | Word COM（`SaveAs` 格式 17） |
| 正式报告/论文 | `latex` 工具优先；用户明确要可编辑 Word 才用 python-docx |

## 读取

```python
from docx import Document
doc = Document(path)
for p in doc.paragraphs: print(p.text)
for t in doc.tables:
    for row in t.rows: print([c.text for c in row.cells])
```

- 目录/标题层级：看 `p.style.name`（`Heading 1`/`Heading 2`…）。
- 长文档避免一次性打印全部：分段读取，超限内容用临时文件 + `read` 工具分段读（见 file-processing 技能）。

## 创建

核心 API 一段段追加：

```python
from docx import Document
doc = Document()
doc.add_heading('标题', level=1)      # 用内置 Heading 样式，目录/大纲才能识别
doc.add_paragraph('正文', style='List Bullet')  # 列表用样式，不要手插 "•"
t = doc.add_table(rows=2, cols=3, style='Table Grid')
t.cell(0, 0).text = 'A1'
doc.save('out.docx')
```

**要点**：
- 标题一律用内置 Heading 样式（不要自定义样式冒充），否则目录失效。
- 列表用 `List Bullet` / `List Number` 样式，禁止手打项目符号字符。
- 页眉页脚：`doc.sections[0].header / .footer`；页码用 `field`（`add_page_number` 需构造 fldSimple）。
- 图片：`doc.add_picture('img.png', width=Inches(2.5))`。
- 分页：在段落前插入分页符而非硬换行。
- 中文字体：设置 `run.font.name` 后还需设 `qn('w:eastAsia')` 才能覆盖东亚字体。

## 编辑现有文档

**常规文本/样式修改**：python-docx 打开→遍历段落/表格改 `run.text` / `cell.text`→保存。注意一个段落可能拆成多个 `run`，全文查找替换要逐 run 拼。

**精确结构操作**（修订、批注、合并单元格、图片、深层格式）：
1. 解压：`python` 工具 `zipfile` 解到临时目录。
2. 编辑 `word/document.xml`（图片同时要改 `word/media/` + `_rels` + `[Content_Types].xml` 三处）。
3. 重新打包成 `.docx`（`zipfile` 写回，保证 `[Content_Types].xml` 是第一个条目）。
4. 用 python-docx 或 `read` 验证能打开、内容正确。

**修订标记**：`<w:ins>`（插入）/`<w:del>`（删除）包住 `<w:r>`；删除段落要在 `<w:pPr><w:rPr>` 加 `<w:del/>` 否则留空段。批注用 `<w:commentRangeStart/>`/`<w:commentRangeEnd/>` + `<w:commentReference/>`。

## 旧版 .doc 优先转 .docx

**遇到 `.doc`：先检测本机是否装了 Office，装了就先经 Word COM 转成 `.docx`，再继续下面的处理**——`.doc` 是二进制旧格式，`python-docx` 不支持直接读。

```
pwsh 工具：New-Object Word.Application → Documents.Open(input.doc) → SaveAs2(output.docx, 16=wdFormatXMLDocument) → Close → Quit → ReleaseComObject
```

转换后用 `python-docx` 验证能打开。**绝不把 `.doc` 直接改名成 `.docx`**（内容损坏）。本机未装 Office 时，退回 LibreOffice 转换（本机未装）或专用 `.doc` 解析库，并如实报告转换质量损失。

## 转换

**`.docx` → PDF**（Word COM，格式 17=wdFormatPDF）：同结构，`SaveAs2(out.pdf, 17)`。

**正式报告优先 `latex` 工具**（排版质量远高于 docx，支持公式/目录/参考文献/中文 ctex）；仅当用户明确要可编辑 Word 才生成 docx。

## 常见坑

- 把 `.doc` 改名成 `.docx` → 打不开。必须先转换。
- 一个段落的文本分散在多个 `run`，替换时漏掉一半。
- 忘记设东亚字体，中文显示成宋体以外的乱排。
- 改 XML 后漏更新 `[Content_Types].xml` 或 `_rels`，Word 报文件损坏。
