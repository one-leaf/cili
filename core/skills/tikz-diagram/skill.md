---
name: TikZ 科学绘图
description: 用 TikZ/PGFplots 生成出版级科学图表：数据可视化、神经网络图、立体几何图。Use when user mentions TikZ、LaTeX 图、PGFplots、科学绘图、出版级图表、论文配图、矢量图、立体几何图、神经网络图。
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

## Pattern Library

Six complete compilable examples — flowchart with decision, neural network, PGFplots loss curve, 3D geometry, layered architecture, timeline/pipeline — plus publication-quality guidelines, compiling and main-document integration live in `references/patterns.md`. Read it when the requested diagram matches one of them.
