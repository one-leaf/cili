# Report generation — LaTeX preferred (file-processing)

When the user requests generating a report, document, or publication, **prioritize LaTeX output**.

## Priority Order

1. **LaTeX (.tex) → PDF** — Professional typesetting, math formulas, academic quality
2. **HTML → PDF** (via browser) — For web-styled reports
3. **DOCX** — Only if user explicitly requests editable format

## Why LaTeX First?

- **Professional typography**: Proper kerning, ligatures, hyphenation
- **Math formulas**: Native support for complex mathematical notation
- **Table of contents**: Automatic generation with page numbers
- **Cross-references**: Figures, tables, sections, bibliography
- **Bibliography management**: BibTeX integration for academic papers
- **Consistent styling**: Document-wide style consistency
- **High-quality PDF output**: Print-ready, vector graphics support

## When to Use LaTeX?

**Use LaTeX when:**
- User requests a formal report, paper, or publication
- Document contains mathematical formulas
- Need professional typesetting (theses, papers, technical docs)
- User mentions "报告", "论文", "文档", "publication"
- Need table of contents, bibliography, cross-references

**Skip LaTeX when:**
- User explicitly requests DOCX/Word format
- Simple text document with no structure
- User needs easy post-editing capability

## Workflow: LaTeX → PDF

```
1. python tool: Generate .tex file with proper structure
2. latex tool: Compile .tex → PDF
3. Handle errors (missing packages, compilation issues)
4. Output: report.pdf
```

## LaTeX Report Template

```python
# python tool: Generate LaTeX report
tex_content = r"""\documentclass[12pt,a4paper]{article}

% Packages
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{amsmath,amssymb}  % Math formulas
\usepackage{graphicx}          % Images
\usepackage{hyperref}          % Hyperlinks
\usepackage{booktabs}          % Professional tables
\usepackage{geometry}          % Page layout
\usepackage{listings}          % Code listings
\usepackage{xcolor}            % Colors

% Page layout
\geometry{margin=2.5cm}

% Code listing style
\lstset{
    basicstyle=\ttfamily\small,
    keywordstyle=\color{blue},
    commentstyle=\color{green!60!black},
    stringstyle=\color{red!70!black},
    breaklines=true,
    frame=single
}

% Document info
\title{报告标题}
\author{作者姓名}
\date{\today}

\begin{document}

\maketitle

\begin{abstract}
简要描述报告内容和主要结论。
\end{abstract}

\tableofcontents
\newpage

\section{引言}
介绍背景和目的。

\section{方法}
描述使用的方法和技术。

\section{结果}
展示主要发现和数据分析。

\subsection{数据分析}
包含图表和统计数据。

\begin{table}[h]
\centering
\caption{数据汇总表}
\begin{tabular}{lcc}
\toprule
指标 & 数值 & 变化率 \\
\midrule
指标A & 100 & +10\% \\
指标B & 200 & -5\% \\
\bottomrule
\end{tabular}
\end{table}

\section{数学公式示例}
行内公式：$E = mc^2$

独立公式：
\begin{equation}
\int_{-\infty}^{\infty} e^{-x^2} dx = \sqrt{\pi}
\end{equation}

\section{代码示例}
\begin{lstlisting}[language=Python]
def hello_world():
    print("Hello, World!")
\end{lstlisting}

\section{结论}
总结主要发现和建议。

% 参考文献（可选）
\begin{thebibliography}{9}
\bibitem{ref1} 作者, 标题, 出版社, 年份.
\end{thebibliography}

\end{document}
"""

with open('report.tex', 'w', encoding='utf-8') as f:
    f.write(tex_content)

print("LaTeX 文件已生成：report.tex")
```

```bash
# latex tool: Compile to PDF
latex(action='compile', tex_file='report.tex')
```

## Chinese Document Support

For Chinese documents, use `ctexart` document class or `ctex` package:

```latex
% Option 1: ctexart (recommended for Chinese)
\documentclass[12pt,a4paper]{ctexart}

% Option 2: article + ctex package
\documentclass[12pt,a4paper]{article}
\usepackage{ctex}
```

## LaTeX with Bibliography (BibTeX)

```python
# Generate .bib file
bib_content = """
@article{smith2020,
  author  = {Smith, John},
  title   = {Example Article},
  journal = {Journal Name},
  year    = {2020},
  volume  = {10},
  pages   = {1-10}
}
"""

with open('references.bib', 'w', encoding='utf-8') as f:
    f.write(bib_content)

# Add to .tex file:
# \bibliographystyle{plain}
# \bibliography{references}
```

Compile sequence for BibTeX:
```bash
latex(action='compile', tex_file='report.tex')
latex(action='compile', tex_file='report.tex')  # Second pass for references
latex(action='compile', tex_file='report.tex')  # Third pass for TOC
```

## Decision Flow

```
User requests "generate report/document"
    ↓
Is it formal/professional/academic?
    ↓
   Yes → Does user explicitly want DOCX?
           ↓
          No  → Use LaTeX → PDF (best quality)
           ↓
          Yes → Use python-docx (editable)
    ↓
   No  → Is it simple text?
           ↓
          Yes → Use plain text or Markdown
           ↓
          No  → Use HTML or LaTeX based on complexity
```

## Important Notes

- **Use `latex` tool** for compilation — it handles errors and missing packages
- **UTF-8 encoding** — always specify `encoding='utf-8'` when writing .tex files
- **Chinese support** — use `ctexart` or `ctex` package for CJK characters
- **Multiple compilation passes** — needed for TOC, cross-references, bibliography
- **Image paths** — use relative paths or absolute paths in `\includegraphics`
- **Error handling** — check LaTeX output for compilation errors, fix and retry
