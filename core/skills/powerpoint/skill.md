---
name: PowerPoint 演示文稿处理
description: PowerPoint / .pptx / .ppt 演示文稿的读取、创建、编辑与视觉检查。Use when user mentions PPT、演示文稿、幻灯片、pptx、路演、汇报、幻灯片。
roles: [master, worker, lite]
---

# PowerPoint 演示文稿处理

## 处理思路（心智模型）

演示文稿的价值在**视觉质量**，不止于文字正确。流程是"做出来 → 渲染成图 → 自查 → 修复 → 复验"，至少一轮修复-验证才交付：

```
python-pptx        → 读取、创建、编辑（文本/形状/表格/图片/备注）
markitdown         → 快速提取全文（含占位符残留检查）
模板驱动 vs 从零   → 有模板/参考就严格沿用其版式与风格；没有才从零设计
pptx→PDF→图片      → PowerPoint COM 导出 PDF，pdftoppm 渲成图片，read_image 自查 + 子代理复核
```

**本机环境**：`python-pptx` / `markitdown` 已装；PowerPoint COM 可用；LibreOffice 不可用 → 转 PDF 走 COM。解析库选型细节遵循 `file-processing` 技能。

## 任务 → 方法

| 需求 | 做法 |
|------|------|
| 提取全文/演讲者备注 | `python -m markitdown file.pptx` |
| 读取结构（形状/文本/表格/图片） | `python-pptx` |
| 从零创建 | `python-pptx` + 下方设计规范 |
| 基于模板/参考改 | `python-pptx` 改文本与内容，**保持原版式与风格** |
| 拆分/合并/深层 XML | `zipfile` 解压→改 `ppt/slides/slideN.xml`→重打包 |
| `.ppt` 旧格式 | 先 PowerPoint COM 转 `.pptx`（见下方），再处理 |
| 转 PDF | PowerPoint COM `ExportAsFixedFormat` |
| 视觉自查 | 渲图 + `read_image` + 子代理 fresh-eyes 复核 |

## 读取

```python
from pptx import Presentation
prs = Presentation('f.pptx')
for slide in prs.slides:
    for shape in slide.shapes:
        if shape.has_text_frame:
            print(shape.text)
        if shape.has_table:
            for row in shape.table.rows: print([c.text for c in row.cells])
```

演讲者备注：`slide.notes_slide.notes_text_frame.text`。图表是图形对象，文字在 `shape.chart` 需用 `chart.plots` 读数据。

## 创建与编辑

```python
from pptx import Presentation
prs = Presentation()                 # 默认 4:3；prs.slide_width/height 可调 16:9
layout = prs.slide_layouts[1]        # 选版式（标题+内容）
slide = prs.slides.add_slide(layout)
slide.shapes.title.text = '标题'
box = slide.shapes.add_textbox(..., top=...); box.text_frame.text = '正文'
slide.shapes.add_table(rows, cols, l, t, w, h)
slide.shapes.add_picture('img.png', left, top, width=...)
prs.save('out.pptx')
```

**要点**：
- 坐标/尺寸单位是 EMU（914400 = 1 英寸）；框过大或重叠是视觉检查必抓项。
- 文本框内边距导致文字与形状边缘不对齐 → 设 `margin: 0` 或偏移形状补偿。
- 基于模板改：逐形状定位（按 `shape.name` 或文本锚点），只改目标文本/图片，不重建版式。
- 别在 python-pptx 里手拼复杂版式；多张幻灯片用同版式保证统一。

## 设计规范（从零设计时）

**配色**：选与主题相关的配色，不要默认通用蓝。一种主色占 60-70% 视觉权重，配 1-2 个辅助色 + 1 个点缀色；颜色要专属于主题（换个主题就不适用才说明选对了）。

**明暗**：标题页/结尾页深色背景、内容页浅色（"三明治"），或全程深色高端风。

**每张都有视觉元素**：图片、图表、图标或形状。纯文字页不合格。布局可选：两栏（左字右图）、图标+文字行、2x2 网格、半出血图+内容叠加、大数字突出（60-72pt 数字+小标签）。

**字体**：标题字体（如 Georgia/Arial Black）配正文（如 Calibri），标题 36-44pt、章节 20-24pt、正文 14-16pt、说明 10-12pt。

**间距**：距边缘 ≥0.5"，内容块间 0.3-0.5"，全篇间距一致。

**避免**：同一布局重复到底；正文居中（段落左对齐，仅标题居中）；标题下加装饰横线（AI 生成典型标志）；低对比度文字/图标；浅背景上浅文字。

## 视觉检查循环（交付前必做）

1. **转图**：PowerPoint COM 导出 PDF → `pdftoppm -jpeg -r 150 out.pdf slide` 渲成图片。
2. **自查**：`read_image` 逐张看，检查元素重叠、文字溢出/截断、间距过小（<0.3"）、边距不足、对齐不一、低对比度、残留占位符。
3. **子代理复核**：交付前用 `agent` 委派一个 worker 做 fresh-eyes 视觉检查（提供图片路径与预期内容），子代理无先入为主更容易发现问题。
4. **修复→复验**：修完重渲受影响页再查一遍——一个修复常引发另一个问题。没有完成至少一轮修复-验证循环不算完成。

**占位符残留检查**：`python -m markitdown out.pptx | grep -iE "xxxx|lorem|ipsum|占位"`，有命中先修。

## 旧版 .ppt 优先转 .pptx

**遇到 `.ppt`：先检测本机是否装了 Office，装了就先经 PowerPoint COM 转成 `.pptx`，再继续上面的处理**——`python-pptx` 不支持二进制 `.ppt`。

```
pwsh 工具：New-Object PowerPoint.Application → Presentations.Open(input.ppt) → SaveAs(output.pptx, 24=ppSaveAsOpenXMLPresentation) → Close → Quit → ReleaseComObject
```

转换后版式、文本、图片、备注全部保留，再用 python-pptx 处理。**绝不把 `.ppt` 改名成 `.pptx`**。本机未装 Office 时退回 LibreOffice 转换（本机未装）并如实报告质量损失。

## 转换

**转 PDF**（PowerPoint COM，`ExportAsFixedFormat(out.pdf)`）：绝对路径、`Visible=false`、用完释放 COM 对象防进程挂起。

## 常见坑

- 只查文字不查渲染——文字对但视觉崩坏（重叠/溢出）照样不合格。
- 复用同版式却忘了每页风格统一；或每页都换不同版式导致碎片化。
- 图片拉伸变形：`add_picture` 只给宽度时高度按比例；同时给 width+height 可能变形。
