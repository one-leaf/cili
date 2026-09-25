---
name: TikZ 科学绘图
description: 用 TikZ/PGFplots 生成出版级科学图表：流程图、神经网络、架构图、数据可视化。Use when user mentions TikZ、LaTeX 图、流程图、架构图、神经网络图、科学绘图、PGFplots、矢量图、立体几何图。
roles: [master, worker, lite]
---

# TikZ Scientific Diagrams

Generate publication-quality scientific diagrams in LaTeX using TikZ. Output is compilable `.tex` code that produces vector PDF graphics.

## Core Principle: Layout by Construction, Not by Coordinate

**Never write `\node at (5.2, 3.1)` for layout.** Use relative positioning so the layout engine calculates positions. This prevents collisions and makes editing safe.

## 5 Must-Follow Idioms

### ① Define Spacing Once, Reference Everywhere

```latex
\newlength{\stagew}\setlength{\stagew}{4.5cm}   % Change once, all follow
\def\gap{0.5cm}
\tikzset{node distance=\gap}
```

### ② Use Relative Positioning, Not Absolute Coordinates

```latex
\node[zone] (s1) at (0,0) {};
\node[zone, right=of s1] (s2) {};      % ✅ Gap from node distance
\node[zone, right=of s2] (s3) {};      % Widen s2 → s3 auto-shifts
% ❌ \node[zone] (s2) at (5,0) {};      % Hard-coded 5 = break on edit
```

### ③ Connect via Node Anchors, Never Re-copy Boundary Coordinates

```latex
\draw[arrow] (s1.east) -- (s2.west);   % ✅ Always correct after reflow
% ❌ \draw[arrow] (4.5,5) -- (5.0,5);   % Re-copies boundary = breaks on edit
```

### ④ Use `fit` for Containers, `text width` for Labels

```latex
\node[fit=(a)(b)(c), draw, rounded corners] {};         % Auto-wrap content
\node[box, text width=3.2cm, align=center] {Long label wraps, no overflow};
% ❌ minimum width=3.2cm → Long label stretches box, overflows neighbor
```

### ⑤ Center with `calc` Midpoint, Not Hand-Calculated

```latex
\coordinate (c) at ($(first.north west)!0.5!(last.north east)$);
\node[anchor=south] at ($(c)+(0,0.5)$) {Title};   % Widens → title auto-centers
```

## 8 Hard Constraints (Violations Cause Compilation Failure)

1. **CJK `rotate=90`** → Renders as unreadable color blocks. Keep Chinese labels horizontal.
2. **`\texttt{…中文…}`** → Error. `\texttt` is for ASCII code only.
3. **xelatex silent font failure** → After compile: `grep "Missing character" *.log`. Non-zero = font not loaded (often CJK).
4. **`rounded corners` on single segment `--`** → Ghost arcs at endpoints. Use only on ≥2 segment paths / `|-`.
5. **Curved paths without `\usetikzlibrary{bending}`** → Arrow tips misalign on curves.
6. **`(A.south) |- (B.west)` when A.x is within B.x range** → Horizontal line pierces B. Use waypoints or different anchors.
7. **Overlapping nodes from absolute coordinates** → Use `fit` or relative positioning.
8. **Arrow tip too large for short arrows** → Use `shorten >=<` for <1.5cm arrows.

## Essential Libraries

| Library | Purpose |
|---------|---------|
| `positioning` | Relative placement (`right=of`, `below=of`) |
| `arrows.meta` | Modern arrow tip styles |
| `shapes.geometric` | Diamond, trapezium, ellipse nodes |
| `calc` | Coordinate calculations |
| `fit` | Fit node around other nodes |
| `decorations.pathreplacing` | Braces, snakes, zigzag lines |
| `backgrounds` | Draw behind other elements |
| `matrix` | Grid-based node layouts |
| `chains` | Sequential node chains |
| `bending` | Arrow tips follow curved paths |

## Output Rules

1. Always produce complete compilable `.tex` files
2. Use `standalone` document class for exportable figures
3. Include all required `\usetikzlibrary` commands
4. Use Chinese labels when user writes in Chinese
5. Add `%` comments to explain diagram sections
6. Keep node names short and meaningful
7. Test-compile mentally before output — check for:
   - Missing library imports
   - Unclosed environments
   - Wrong anchor references
   - CJK in forbidden contexts (`\texttt`, rotated nodes)

## Basic Template

```latex
\documentclass[tikz, border=2mm]{standalone}
\usetikzlibrary{arrows.meta, positioning, shapes.geometric, calc, fit}

\begin{document}
\begin{tikzpicture}[
    % Define common styles
    block/.style={rectangle, draw, fill=blue!10, text width=5em,
                  text centered, rounded corners, minimum height=3em},
    line/.style={draw, -Stealth, thick}
]
  % Your diagram here
\end{tikzpicture}
\end{document}
```

---

# Diagram Pattern Library

Common patterns with complete example code.

## 1. Flowchart with Decision

```latex
\documentclass[tikz, border=2mm]{standalone}
\usetikzlibrary{arrows.meta, positioning, shapes.geometric}

\begin{document}
\begin{tikzpicture}[
    block/.style={rectangle, draw, fill=blue!10, text width=5em,
                  text centered, rounded corners, minimum height=3em},
    decision/.style={diamond, draw, fill=green!10, text width=4em,
                     text centered, aspect=2},
    line/.style={draw, -Stealth, thick}
]
  \node[block] (data) {Collect Data};
  \node[block, below=1cm of data] (clean) {Clean \& Preprocess};
  \node[decision, below=1cm of clean] (valid) {Valid?};
  \node[block, below=1cm of valid] (analyze) {Analyze};
  \node[block, right=2cm of valid] (fix) {Fix Issues};

  \path[line] (data) -- (clean);
  \path[line] (clean) -- (valid);
  \path[line] (valid) -- node[right] {Yes} (analyze);
  \path[line] (valid) -- node[above] {No} (fix);
  \path[line] (fix) |- (clean);
\end{tikzpicture}
\end{document}
```

## 2. Neural Network Architecture

```latex
\documentclass[tikz, border=2mm]{standalone}
\usetikzlibrary{arrows.meta, positioning}

\begin{document}
\begin{tikzpicture}[
    neuron/.style={circle, draw, minimum size=0.8cm, fill=orange!20},
    layer/.style={rectangle, draw, dashed, inner sep=0.3cm}
]
  % Input layer
  \foreach \i in {1,2,3,4} {
    \node[neuron] (I\i) at (0, -\i*1.2) {};
  }

  % Hidden layer
  \foreach \j in {1,2,3} {
    \node[neuron, fill=blue!20] (H\j) at (3, -\j*1.2 - 0.6) {};
  }

  % Output layer
  \foreach \k in {1,2} {
    \node[neuron, fill=green!20] (O\k) at (6, -\k*1.2 - 1.2) {};
  }

  % Connections
  \foreach \i in {1,2,3,4} {
    \foreach \j in {1,2,3} {
      \draw[->] (I\i) -- (H\j);
    }
  }
  \foreach \j in {1,2,3} {
    \foreach \k in {1,2} {
      \draw[->] (H\j) -- (O\k);
    }
  }

  % Labels
  \node[above=0.5cm of I1] {Input};
  \node[above=0.5cm of H1] {Hidden};
  \node[above=0.5cm of O1] {Output};
\end{tikzpicture}
\end{document}
```

## 3. PGFplots: Training/Validation Loss Curve

```latex
\documentclass[tikz, border=2mm]{standalone}
\usepackage{pgfplots}
\pgfplotsset{compat=1.18}

\begin{document}
\begin{tikzpicture}
\begin{axis}[
    xlabel={Epoch},
    ylabel={Loss},
    legend pos=north east,
    grid=major,
    width=8cm, height=6cm
]
  \addplot[blue, thick, mark=none] table {
    1  0.95
    5  0.72
    10 0.45
    20 0.22
    30 0.15
    50 0.08
  };
  \addlegendentry{Training}

  \addplot[red, thick, dashed, mark=none] table {
    1  0.98
    5  0.75
    10 0.52
    20 0.35
    30 0.30
    50 0.28
  };
  \addlegendentry{Validation}
\end{axis}
\end{tikzpicture}
\end{document}
```

## 4. 3D Geometry (Solid Geometry Proof)

```latex
\documentclass[tikz, border=2mm]{standalone}
\usetikzlibrary{calc}

\begin{document}
\begin{tikzpicture}[
    xyz coords/.style={x={(1cm,0cm)}, y={(0cm,1cm)}, z={(0.5cm,0.5cm)}}
]
  % Define vertices
  \coordinate (A) at (0,0,0);
  \coordinate (B) at (2,0,0);
  \coordinate (C) at (2,2,0);
  \coordinate (P) at (0,0,2);

  % Draw edges
  \draw[thick] (A) -- (B) -- (C) -- cycle;        % Base triangle
  \draw[thick] (P) -- (A) (P) -- (B) (P) -- (C);  % Pyramid edges

  % Mark right angle
  \draw (0.3,0,0) -- (0.3,0.3,0) -- (0,0.3,0);

  % Labels
  \node[below left] at (A) {$A$};
  \node[below right] at (B) {$B$};
  \node[below] at (C) {$C$};
  \node[above] at (P) {$P$};

  % Annotations
  \node[left] at (0,0,1) {$PA=2$};
  \node[below] at (1,0,0) {$AB=2$};
\end{tikzpicture}
\end{document}
```

## 5. Layered Architecture Diagram

```latex
\documentclass[tikz, border=2mm]{standalone}
\usetikzlibrary{arrows.meta, positioning, fit, calc}

\begin{document}
\begin{tikzpicture}[
    layer/.style={rectangle, draw, rounded corners, minimum height=1.5cm,
                  text width=12cm, align=center, font=\bfseries},
    module/.style={rectangle, draw, fill=white, minimum height=1cm,
                   text width=3cm, align=center, font=\small}
]
  % Layers (relative positioning)
  \node[layer, fill=blue!20] (ui) {User Interface Layer};
  \node[layer, fill=green!20, below=0.5cm of ui] (logic) {Business Logic Layer};
  \node[layer, fill=orange!20, below=0.5cm of logic] (data) {Data Access Layer};

  % Modules in logic layer
  \node[module, right=0.5cm of logic.west, anchor=west] (auth) {Authentication};
  \node[module, right=0.3cm of auth] (order) {Order Processing};
  \node[module, right=0.3cm of order] (payment) {Payment};

  % Connections
  \draw[-Stealth, thick] (ui.south) -- (logic.north);
  \draw[-Stealth, thick] (logic.south) -- (data.north);
\end{tikzpicture}
\end{document}
```

## 6. Timeline / Pipeline

```latex
\documentclass[tikz, border=2mm]{standalone}
\usetikzlibrary{arrows.meta, chains, calc}

\begin{document}
\begin{tikzpicture}[
    stage/.style={rectangle, draw, fill=blue!10, minimum height=2cm,
                  text width=3cm, align=center, rounded corners},
    arrow/.style={-Stealth, thick}
]
  % Pipeline stages (using chains)
  \begin{scope}[start chain=going right, node distance=1cm]
    \node[stage, on chain] (s1) {Data Collection};
    \node[stage, on chain] (s2) {Preprocessing};
    \node[stage, on chain] (s3) {Model Training};
    \node[stage, on chain] (s4) {Evaluation};
    \node[stage, on chain] (s5) {Deployment};
  \end{scope}

  % Arrows
  \draw[arrow] (s1.east) -- (s2.west);
  \draw[arrow] (s2.east) -- (s3.west);
  \draw[arrow] (s3.east) -- (s4.west);
  \draw[arrow] (s4.east) -- (s5.west);

  % Timeline label
  \coordinate (mid) at ($(s1.north)!0.5!(s5.north)$);
  \node[above=0.5cm of mid] {\textbf{ML Pipeline}};
\end{tikzpicture}
\end{document}
```

## Publication Quality Guidelines

1. **Font consistency**: Match document body font; minimum 8pt for labels
2. **Color**: Use colorblind-friendly palettes; ensure grayscale readability
3. **Size**: Set width to match column (single/double); consistent across figures
4. **Labels**: Label all axes with units; use (a)(b)(c) for sub-figures
5. **Vector output**: TikZ produces PDF — always sharp at any zoom

## Compiling

```bash
# Compile to PDF
xelatex figure.tex

# For CJK content, use xelatex with ctex
xelatex -shell-escape figure.tex
```

## Integration with Main Document

```latex
% In main.tex
\includegraphics{standalone_figure.pdf}

% Or compile inline
\input{tikz_figure.tex}
```
