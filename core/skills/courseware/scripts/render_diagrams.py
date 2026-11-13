"""Deterministic cartoon SVG diagrams for cili courseware.

Only declarative params are accepted; no model-supplied SVG or coordinates.
"""

from __future__ import annotations

import html
import math
from typing import Any

INK = "#4a3423"
LINE = "#8b6b4a"
PAPER = "#fff6df"
ORANGE = "#f2812f"
CORAL = "#e4572e"
TEAL = "#2fa8a0"
SUN = "#f7c548"
GOOD = "#3e9b63"

FONT = "Microsoft YaHei, PingFang SC, sans-serif"


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _svg(width: int, height: int, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" style="width:100%;height:auto;max-height:430px">'
        f'<rect x="2" y="2" width="{width - 4}" height="{height - 4}" rx="22" '
        f'fill="{PAPER}" stroke="{LINE}" stroke-width="4"/>'
        f"{body}</svg>"
    )


def _text(x: float, y: float, value: Any, size: int = 20, color: str = INK,
          anchor: str = "middle", weight: str = "400") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
        f'font-weight="{weight}" fill="{color}" text-anchor="{anchor}">{_e(value)}</text>'
    )


def _tick(x: float, y: float) -> str:
    return f'<line x1="{x:.1f}" y1="{y}" x2="{x:.1f}" y2="{y + 14}" stroke="{INK}" stroke-width="3" stroke-linecap="round"/>'


def number_line(params: dict[str, Any]) -> str:
    lo = float(params.get("min", 0))
    hi = float(params.get("max", 10))
    if hi <= lo:
        hi = lo + 10
    marks = params.get("marks") or []
    highlight = params.get("highlight")
    w, h = 1000, 260
    left, right, y = 70, 930, 150
    body = []
    body.append(f'<line x1="{left}" y1="{y}" x2="{right}" y2="{y}" stroke="{INK}" stroke-width="5" stroke-linecap="round"/>')
    body.append(f'<path d="M{right - 18} {y - 12} L{right} {y} L{right - 18} {y + 12}" fill="none" stroke="{INK}" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>')
    span = hi - lo
    step = 1 if span <= 12 else max(1, round(span / 10))
    value = math.ceil(lo / step) * step
    while value <= hi + 1e-9:
        x = left + (value - lo) / span * (right - left)
        body.append(_tick(x, y))
        body.append(_text(x, y + 44, int(value) if float(value).is_integer() else round(value, 2), 18))
        value += step
    if isinstance(highlight, dict):
        a = float(highlight.get("from", lo))
        b = float(highlight.get("to", hi))
        x1 = left + (a - lo) / span * (right - left)
        x2 = left + (b - lo) / span * (right - left)
        body.insert(1, f'<line x1="{x1:.1f}" y1="{y}" x2="{x2:.1f}" y2="{y}" stroke="{ORANGE}" stroke-width="16" stroke-linecap="round" opacity="0.55"/>')
    for mark in marks:
        try:
            v = float(mark.get("value"))
        except (TypeError, ValueError):
            continue
        x = left + (v - lo) / span * (right - left)
        body.append(f'<circle cx="{x:.1f}" cy="{y}" r="11" fill="{SUN}" stroke="{INK}" stroke-width="4"/>')
        if mark.get("label") is not None:
            body.append(_text(x, y - 26, mark.get("label"), 19, INK, weight="700"))
    return _svg(w, h, "".join(body))


def fraction_bar(params: dict[str, Any]) -> str:
    parts = max(1, int(params.get("parts", 4)))
    filled = max(0, min(parts, int(params.get("filled", 0))))
    shape = params.get("shape", "bar")
    w, h = 1000, 300
    body = []
    if shape == "circle":
        cx, cy, r = 500, 155, 112
        for i in range(parts):
            a0 = -math.pi / 2 + i * 2 * math.pi / parts
            a1 = -math.pi / 2 + (i + 1) * 2 * math.pi / parts
            x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
            x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
            large = 1 if (a1 - a0) > math.pi else 0
            fill = ORANGE if i < filled else PAPER
            body.append(f'<path d="M{cx} {cy} L{x0:.1f} {y0:.1f} A{r} {r} 0 {large} 1 {x1:.1f} {y1:.1f} Z" fill="{fill}" stroke="{INK}" stroke-width="4"/>')
        body.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{INK}" stroke-width="4"/>')
        body.append(_text(cx, cy + r + 42, f"{filled} / {parts}", 24, INK, weight="700"))
    else:
        left, top, width, height = 80, 100, 840, 110
        seg = width / parts
        for i in range(parts):
            x = left + i * seg
            fill = ORANGE if i < filled else PAPER
            body.append(f'<rect x="{x:.1f}" y="{top}" width="{seg:.1f}" height="{height}" fill="{fill}" stroke="{INK}" stroke-width="4"/>')
        body.append(f'<rect x="{left}" y="{top}" width="{width}" height="{height}" fill="none" stroke="{INK}" stroke-width="5" rx="4"/>')
        body.append(_text(left + width / 2, top + height + 48, f"{filled} / {parts}", 24, INK, weight="700"))
    return _svg(w, h, "".join(body))


def bar_chart(params: dict[str, Any]) -> str:
    labels = list(params.get("labels") or [])
    values = list(params.get("values") or [])
    unit = str(params.get("unit") or "")
    n = min(len(labels), len(values))
    if n == 0:
        return _svg(900, 360, _text(450, 190, "暂无数据", 24))
    w, h = 1000, 400
    left, right, top, bottom = 90, 930, 50, 320
    max_v = max(float(values[i]) for i in range(n)) or 1.0
    slot = (right - left) / n
    bar_w = min(96, slot * 0.56)
    body = [f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="{INK}" stroke-width="4" stroke-linecap="round"/>']
    colors = [ORANGE, TEAL, SUN, CORAL, "#8f6bd8", GOOD]
    for i in range(n):
        v = float(values[i])
        bh = max(4, (bottom - top) * v / max_v)
        x = left + slot * i + (slot - bar_w) / 2
        y = bottom - bh
        body.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" rx="10" fill="{colors[i % len(colors)]}" stroke="{INK}" stroke-width="4"/>')
        body.append(_text(x + bar_w / 2, y - 12, f"{v:g}{unit}", 19, INK, weight="700"))
        body.append(_text(x + bar_w / 2, bottom + 34, labels[i], 18))
    return _svg(w, h, "".join(body))


def geometry(params: dict[str, Any]) -> str:
    shape = params.get("shape", "triangle")
    labels = list(params.get("labels") or [])
    lengths = list(params.get("lengths") or [])
    w, h = 1000, 420
    body = []
    if shape == "rectangle":
        x, y, rw, rh = 260, 100, 480, 240
        body.append(f'<rect x="{x}" y="{y}" width="{rw}" height="{rh}" fill="{SUN}" stroke="{INK}" stroke-width="5" rx="6"/>')
        body.append(_text(x + rw / 2, y + rh / 2 + 8, labels[0] if labels else "长方形", 24, INK, weight="700"))
        if len(lengths) >= 2:
            body.append(_text(x + rw / 2, y - 12, lengths[0], 20, INK))
            body.append(_text(x + rw + 16, y + rh / 2, lengths[1], 20, INK, anchor="start"))
    elif shape == "circle":
        cx, cy, r = 500, 210, 130
        body.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{TEAL}" fill-opacity="0.35" stroke="{INK}" stroke-width="5"/>')
        body.append(f'<line x1="{cx}" y1="{cy}" x2="{cx + r}" y2="{cy}" stroke="{CORAL}" stroke-width="4" stroke-dasharray="8 7"/>')
        body.append(f'<circle cx="{cx}" cy="{cy}" r="5" fill="{INK}"/>')
        body.append(_text(cx + r / 2, cy - 14, lengths[0] if lengths else "r", 20, INK))
        body.append(_text(cx, cy + r + 42, labels[0] if labels else "圆", 24, INK, weight="700"))
    else:
        pts = [(500, 78), (220, 330), (780, 330)]
        d = " ".join(f"{x},{y}" for x, y in pts)
        body.append(f'<polygon points="{d}" fill="{ORANGE}" fill-opacity="0.32" stroke="{INK}" stroke-width="5" stroke-linejoin="round"/>')
        for i, (x, y) in enumerate(pts):
            body.append(f'<circle cx="{x}" cy="{y}" r="7" fill="{PAPER}" stroke="{INK}" stroke-width="4"/>')
            if i < len(labels):
                dx = -26 if i == 1 else (26 if i == 2 else 0)
                body.append(_text(x + dx, y + (-18 if i == 0 else 30), labels[i], 21, INK, weight="700"))
        for i in range(3):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % 3]
            if i < len(lengths):
                body.append(_text((x0 + x1) / 2, (y0 + y1) / 2 - 10, lengths[i], 18, INK))
    return _svg(w, h, "".join(body))


def flow(params: dict[str, Any]) -> str:
    steps = [str(s) for s in (params.get("steps") or [])]
    direction = params.get("direction", "vertical")
    if not steps:
        return _svg(900, 360, _text(450, 190, "暂无步骤", 24))
    if direction == "horizontal":
        w, h = 1100, 260
        n = len(steps)
        bw = min(200, (w - 100 - (n - 1) * 40) / n)
        total_w = n * bw + (n - 1) * 40
        x0 = (w - total_w) / 2
        body = []
        for i, s in enumerate(steps):
            x = x0 + i * (bw + 40)
            body.append(f'<rect x="{x:.1f}" y="{70 if i % 2 == 0 else 110}" width="{bw:.1f}" height="82" rx="18" fill="{SUN if i % 2 == 0 else PAPER}" stroke="{INK}" stroke-width="4"/>')
            body.append(_text(x + bw / 2, (70 if i % 2 == 0 else 110) + 50, s, 18, INK, weight="700"))
            if i < n - 1:
                y = (70 if i % 2 == 0 else 110) + 41
                body.append(f'<path d="M{x + bw + 6:.1f} {y} L{x + bw + 34:.1f} {y}" stroke="{LINE}" stroke-width="4" stroke-linecap="round"/>')
                body.append(f'<path d="M{x + bw + 28:.1f} {y - 8} L{x + bw + 36:.1f} {y} L{x + bw + 28:.1f} {y + 8}" fill="none" stroke="{LINE}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>')
        return _svg(w, h, "".join(body))
    w, h = 900, 120 + len(steps) * 92
    body = []
    for i, s in enumerate(steps):
        y = 40 + i * 92
        body.append(f'<rect x="230" y="{y}" width="440" height="62" rx="16" fill="{SUN if i % 2 == 0 else PAPER}" stroke="{INK}" stroke-width="4"/>')
        body.append(f'<circle cx="196" cy="{y + 31}" r="20" fill="{ORANGE}" stroke="{INK}" stroke-width="4"/>')
        body.append(_text(196, y + 38, i + 1, 18, INK, weight="700"))
        body.append(_text(450, y + 39, s, 20, INK, weight="700"))
        if i < len(steps) - 1:
            body.append(f'<path d="M450 {y + 66} L450 {y + 88}" stroke="{LINE}" stroke-width="4" stroke-linecap="round"/>')
            body.append(f'<path d="M441 {y + 80} L450 {y + 90} L459 {y + 80}" fill="none" stroke="{LINE}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>')
    return _svg(w, h, "".join(body))


RENDERERS = {
    "number_line": number_line,
    "fraction_bar": fraction_bar,
    "bar_chart": bar_chart,
    "geometry": geometry,
    "flow": flow,
}


def render(kind: str, params: dict[str, Any] | None = None) -> str:
    fn = RENDERERS.get(str(kind))
    if fn is None:
        return _svg(900, 300, _text(450, 160, f"暂不支持的图示：{kind}", 22))
    try:
        return fn(params or {})
    except Exception as exc:  # degrade, never break the whole courseware
        return _svg(900, 300, _text(450, 140, "图示参数有误", 22) + _text(450, 178, str(exc)[:80], 15, "#7a6149"))
