"""Cartoon SVG illustrations for cili courseware.

Declarative kinds only. The model supplies kind + params, never SVG coordinates.
"""

from __future__ import annotations

import html
from typing import Any

INK = "#4a3423"
LINE = "#8b6b4a"
PAPER = "#fff6df"
ORANGE = "#f2812f"
CORAL = "#e4572e"
TEAL = "#2fa8a0"
SUN = "#f7c548"
GOOD = "#3e9b63"
BLUE = "#4a90c2"
PINK = "#f2a0a8"

FONT = "Microsoft YaHei, PingFang SC, sans-serif"


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _svg(width: int, height: int, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" style="width:100%;height:auto;max-height:380px">'
        f'<rect x="2" y="2" width="{width - 4}" height="{height - 4}" rx="24" '
        f'fill="{PAPER}" stroke="{LINE}" stroke-width="4"/>'
        f"{body}</svg>"
    )


def _text(x: float, y: float, value: Any, size: int = 20, color: str = INK,
          anchor: str = "middle", weight: str = "400") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
        f'font-weight="{weight}" fill="{color}" text-anchor="{anchor}">{_e(value)}</text>'
    )


def _face(cx: float, cy: float, r: float = 26) -> str:
    return (
        f'<circle cx="{cx - 9}" cy="{cy - 4}" r="4" fill="{INK}"/>'
        f'<circle cx="{cx + 9}" cy="{cy - 4}" r="4" fill="{INK}"/>'
        f'<path d="M{cx - 9} {cy + 8} Q{cx} {cy + 18} {cx + 9} {cy + 8}" fill="none" '
        f'stroke="{INK}" stroke-width="3" stroke-linecap="round"/>'
    )


def _sparkles() -> str:
    pts = [(820, 70), (860, 110), (790, 130), (880, 170)]
    out = []
    for x, y in pts:
        out.append(f'<path d="M{x} {y - 9} L{x + 3} {y - 3} L{x + 9} {y} L{x + 3} {y + 3} L{x} {y + 9} L{x - 3} {y + 3} L{x - 9} {y} L{x - 3} {y - 3} Z" fill="{SUN}" stroke="{LINE}" stroke-width="2"/>')
    return "".join(out)


def _cake(params: dict[str, Any]) -> str:
    parts = max(1, int(params.get("parts", 4)))
    filled = max(0, min(parts, int(params.get("filled", 1))))
    w, h = 900, 460
    body = [_sparkles()]
    body.append(f'<ellipse cx="450" cy="390" rx="250" ry="34" fill="{SUN}" stroke="{INK}" stroke-width="4"/>')
    body.append(f'<rect x="270" y="220" width="360" height="150" rx="18" fill="{PINK}" stroke="{INK}" stroke-width="5"/>')
    body.append(f'<path d="M270 250 Q300 292 330 250 Q360 292 390 250 Q420 292 450 250 Q480 292 510 250 Q540 292 570 250 Q600 292 630 250 L630 220 L270 220 Z" fill="{PAPER}" stroke="{INK}" stroke-width="4"/>')
    seg = 360 / parts
    for i in range(1, parts):
        x = 270 + seg * i
        body.append(f'<line x1="{x:.1f}" y1="220" x2="{x:.1f}" y2="370" stroke="{INK}" stroke-width="3" stroke-dasharray="7 7" opacity="0.65"/>')
    for i in range(min(filled, parts)):
        x = 270 + seg * i
        body.append(f'<rect x="{x:.1f}" y="220" width="{seg:.1f}" height="150" fill="{ORANGE}" opacity="0.20"/>')
    body.append(f'<rect x="360" y="150" width="10" height="70" rx="4" fill="{TEAL}" stroke="{INK}" stroke-width="3"/>')
    body.append(f'<rect x="500" y="150" width="10" height="70" rx="4" fill="{TEAL}" stroke="{INK}" stroke-width="3"/>')
    body.append(_text(450, 430, f"平均分成 {parts} 份，取了 {filled} 份", 22, INK, weight="700"))
    return _svg(w, h, "".join(body))


def _rope(params: dict[str, Any]) -> str:
    parts = max(1, int(params.get("parts", 5)))
    filled = max(0, min(parts, int(params.get("filled", 1))))
    w, h = 900, 380
    left, right, y = 90, 810, 190
    body = []
    body.append(f'<path d="M{left} {y} C{left + 90} {y - 60}, {left + 170} {y + 60}, {left + 260} {y} '
                f'S{left + 430} {y - 60}, {left + 520} {y} S{left + 690} {y + 60}, {right} {y}" '
                f'fill="none" stroke="{INK}" stroke-width="12" stroke-linecap="round"/>')
    seg = (right - left) / parts
    for i in range(parts + 1):
        x = left + seg * i
        body.append(f'<circle cx="{x:.1f}" cy="{y}" r="12" fill="{PAPER}" stroke="{LINE}" stroke-width="4"/>')
    if filled:
        body.append(f'<path d="M{left} {y} L{left + seg * filled:.1f} {y}" stroke="{ORANGE}" stroke-width="20" stroke-linecap="round" opacity="0.45"/>')
    body.append(_text(450, 300, f"每段是全长的 1/{parts}", 24, INK, weight="700"))
    body.append(_text(450, 338, f"取了 {filled} 段", 19, "#7a6149"))
    return _svg(w, h, "".join(body))


def _water(params: dict[str, Any]) -> str:
    parts = max(2, int(params.get("parts", 4)))
    filled = max(0, min(parts, int(params.get("filled", 1))))
    w, h = 900, 440
    body = []
    top, bottom, left, right = 110, 360, 320, 580
    body.append(f'<path d="M{left} {top} L{left} {bottom - 12} Q{left} {bottom} {left + 12} {bottom} '
                f'L{right - 12} {bottom} Q{right} {bottom} {right} {bottom - 12} L{right} {top}" '
                f'fill="none" stroke="{INK}" stroke-width="5" stroke-linejoin="round"/>')
    height = bottom - top
    level = height * filled / parts
    body.append(f'<rect x="{left + 4}" y="{bottom - level:.1f}" width="{right - left - 8}" height="{max(0, level - 4):.1f}" fill="{BLUE}" opacity="0.45"/>')
    for i in range(1, parts + 1):
        yy = bottom - height * i / parts
        body.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{left + 24}" y2="{yy:.1f}" stroke="{INK}" stroke-width="3"/>')
        body.append(_text(left + 42, yy + 6, f"{i}/{parts}", 15, "#7a6149", anchor="start"))
    body.append(_face(450, 230))
    body.append(_text(450, 410, f"水深是全杯的 {filled}/{parts}", 21, INK, weight="700"))
    return _svg(w, h, "".join(body))


def _money(params: dict[str, Any]) -> str:
    w, h = 900, 420
    body = []
    for i, (x, y, c) in enumerate([(240, 210, SUN), (300, 180, ORANGE), (360, 210, SUN)]):
        body.append(f'<ellipse cx="{x}" cy="{y + 70}" rx="86" ry="22" fill="{PAPER}" stroke="{LINE}" stroke-width="3"/>')
        body.append(f'<ellipse cx="{x}" cy="{y}" rx="80" ry="44" fill="{c}" stroke="{INK}" stroke-width="4"/>')
        body.append(_text(x, y + 9, "¥", 30, INK, weight="700"))
    body.append(f'<rect x="500" y="150" width="250" height="120" rx="14" fill="{TEAL}" fill-opacity="0.30" stroke="{INK}" stroke-width="4" transform="rotate(-6 625 210)"/>')
    body.append(_text(625, 220, "¥ 1 0 0", 26, INK, weight="700"))
    body.append(_text(450, 350, "生活中的数量与分配", 21, "#7a6149"))
    return _svg(w, h, "".join(body))


def _book(params: dict[str, Any]) -> str:
    w, h = 900, 420
    body = []
    body.append(f'<path d="M450 120 Q330 92 190 130 L190 330 Q330 292 450 330 Z" fill="{SUN}" stroke="{INK}" stroke-width="5" stroke-linejoin="round"/>')
    body.append(f'<path d="M450 120 Q570 92 710 130 L710 330 Q570 292 450 330 Z" fill="{ORANGE}" fill-opacity="0.35" stroke="{INK}" stroke-width="5" stroke-linejoin="round"/>')
    body.append(f'<line x1="450" y1="120" x2="450" y2="330" stroke="{INK}" stroke-width="4"/>')
    for i in range(3):
        y = 175 + i * 36
        body.append(f'<line x1="230" y1="{y}" x2="410" y2="{y - 10}" stroke="{LINE}" stroke-width="5" stroke-linecap="round"/>')
        body.append(f'<line x1="490" y1="{y - 10}" x2="670" y2="{y}" stroke="{LINE}" stroke-width="5" stroke-linecap="round"/>')
    body.append(_text(450, 378, "读书 · 学习 · 思考", 20, "#7a6149"))
    return _svg(w, h, "".join(body))


def _life_scene(params: dict[str, Any]) -> str:
    topic = str(params.get("topic", "cake"))
    if topic in ("cake", "pizza", "pie"):
        return _cake(params)
    if topic in ("rope", "string", "ribbon"):
        return _rope(params)
    if topic in ("water", "cup", "measure"):
        return _water(params)
    if topic in ("money", "coin"):
        return _money(params)
    if topic in ("book", "read"):
        return _book(params)
    return _cake(params)


def _beaker(params: dict[str, Any]) -> str:
    w, h = 900, 440
    body = []
    body.append(f'<path d="M300 100 L300 330 Q300 370 340 370 L560 370 Q600 370 600 330 L600 100" fill="none" stroke="{INK}" stroke-width="5"/>')
    body.append(f'<path d="M320 300 L580 300 L580 330 Q580 355 555 355 L345 355 Q320 355 320 330 Z" fill="{TEAL}" opacity="0.45"/>')
    for i in range(3):
        y = 180 + i * 46
        body.append(f'<line x1="300" y1="{y}" x2="330" y2="{y}" stroke="{INK}" stroke-width="3"/>')
        body.append(_text(278, y + 6, f"{i + 1}00", 14, "#7a6149", anchor="end"))
    body.append(f'<path d="M600 130 L650 130" stroke="{INK}" stroke-width="5" stroke-linecap="round"/>')
    body.append(_face(450, 245, 22))
    body.append(_text(450, 410, "观察刻度，读出液面高度", 20, INK, weight="700"))
    return _svg(w, h, "".join(body))


def _flask(params: dict[str, Any]) -> str:
    w, h = 900, 440
    body = []
    body.append(f'<path d="M410 90 L410 190 L300 340 Q280 375 320 375 L580 375 Q620 375 600 340 L490 190 L490 90" fill="none" stroke="{INK}" stroke-width="5" stroke-linejoin="round"/>')
    body.append(f'<path d="M330 320 Q450 290 570 320 L580 345 Q600 370 570 370 L330 370 Q300 370 320 345 Z" fill="{ORANGE}" opacity="0.40"/>')
    body.append(f'<rect x="395" y="76" width="110" height="20" rx="6" fill="{SUN}" stroke="{INK}" stroke-width="4"/>')
    body.append(f'<circle cx="430" cy="285" r="10" fill="{PAPER}" stroke="{LINE}" stroke-width="3"/>')
    body.append(f'<circle cx="480" cy="250" r="7" fill="{PAPER}" stroke="{LINE}" stroke-width="3"/>')
    body.append(_text(450, 410, "实验装置：观察变化", 20, INK, weight="700"))
    return _svg(w, h, "".join(body))


def _circuit(params: dict[str, Any]) -> str:
    w, h = 900, 420
    body = []
    body.append(f'<rect x="150" y="90" width="600" height="240" rx="26" fill="none" stroke="{LINE}" stroke-width="6"/>')
    body.append(f'<rect x="360" y="60" width="120" height="60" rx="10" fill="{SUN}" stroke="{INK}" stroke-width="4"/>')
    body.append(_text(420, 98, "+  −", 22, INK, weight="700"))
    body.append(f'<circle cx="170" cy="210" r="28" fill="{PAPER}" stroke="{INK}" stroke-width="4"/>')
    body.append(_text(170, 219, "灯", 20, INK, weight="700"))
    body.append(f'<rect x="600" y="180" width="90" height="60" rx="10" fill="{CORAL}" fill-opacity="0.35" stroke="{INK}" stroke-width="4"/>')
    body.append(_text(645, 217, "电阻", 18, INK, weight="700"))
    body.append(_text(450, 380, "电路示意", 20, "#7a6149"))
    return _svg(w, h, "".join(body))


def _scale(params: dict[str, Any]) -> str:
    w, h = 900, 440
    body = []
    body.append(f'<line x1="450" y1="100" x2="450" y2="330" stroke="{INK}" stroke-width="6" stroke-linecap="round"/>')
    body.append(f'<line x1="230" y1="150" x2="670" y2="150" stroke="{INK}" stroke-width="6" stroke-linecap="round"/>')
    body.append(f'<path d="M230 150 L180 260 Q230 300 280 260 Z" fill="{SUN}" fill-opacity="0.5" stroke="{INK}" stroke-width="4"/>')
    body.append(f'<path d="M670 150 L620 260 Q670 300 720 260 Z" fill="{TEAL}" fill-opacity="0.4" stroke="{INK}" stroke-width="4"/>')
    body.append(f'<path d="M300 340 L600 340 L560 390 L340 390 Z" fill="{PAPER}" stroke="{INK}" stroke-width="4"/>')
    body.append(_text(450, 425, "天平：比较轻重", 20, INK, weight="700"))
    return _svg(w, h, "".join(body))


def _apparatus(params: dict[str, Any]) -> str:
    kind = str(params.get("kind", "beaker"))
    if kind == "flask":
        return _flask(params)
    if kind == "circuit":
        return _circuit(params)
    if kind == "scale":
        return _scale(params)
    return _beaker(params)


def _object(params: dict[str, Any]) -> str:
    shape = str(params.get("shape", "cube"))
    w, h = 900, 420
    body = []
    if shape == "cube":
        body.append(f'<path d="M300 170 L450 110 L600 170 L450 230 Z" fill="{SUN}" stroke="{INK}" stroke-width="4"/>')
        body.append(f'<path d="M300 170 L300 300 L450 360 L450 230 Z" fill="{ORANGE}" fill-opacity="0.45" stroke="{INK}" stroke-width="4"/>')
        body.append(f'<path d="M600 170 L600 300 L450 360 L450 230 Z" fill="{TEAL}" fill-opacity="0.35" stroke="{INK}" stroke-width="4"/>')
    elif shape == "cylinder":
        body.append(f'<rect x="330" y="140" width="240" height="200" fill="{SUN}" fill-opacity="0.55" stroke="{INK}" stroke-width="4"/>')
        body.append(f'<ellipse cx="450" cy="140" rx="120" ry="42" fill="{PAPER}" stroke="{INK}" stroke-width="4"/>')
        body.append(f'<ellipse cx="450" cy="340" rx="120" ry="42" fill="{ORANGE}" fill-opacity="0.25" stroke="{INK}" stroke-width="4"/>')
    elif shape == "cone":
        body.append(f'<path d="M450 100 L620 340 L280 340 Z" fill="{ORANGE}" fill-opacity="0.4" stroke="{INK}" stroke-width="4"/>')
        body.append(f'<ellipse cx="450" cy="340" rx="170" ry="52" fill="{PAPER}" stroke="{INK}" stroke-width="4"/>')
    elif shape == "sphere":
        body.append(f'<circle cx="450" cy="235" r="140" fill="{TEAL}" fill-opacity="0.35" stroke="{INK}" stroke-width="4"/>')
        body.append(f'<ellipse cx="450" cy="235" rx="140" ry="46" fill="none" stroke="{INK}" stroke-width="3" stroke-dasharray="8 7"/>')
    else:
        body.append(f'<polygon points="450,90 650,350 250,350" fill="{SUN}" fill-opacity="0.45" stroke="{INK}" stroke-width="4"/>')
    labels = params.get("labels") or []
    if labels:
        body.append(_text(450, 400, labels[0], 20, INK, weight="700"))
    return _svg(w, h, "".join(body))


_ICONS = {
    "star": f'<path d="M0 -18 L5 -5 L19 -5 L8 4 L12 18 L0 10 L-12 18 L-8 4 L-19 -5 L-5 -5 Z" fill="{SUN}" stroke="{INK}" stroke-width="3"/>',
    "book": f'<rect x="-16" y="-14" width="32" height="28" rx="4" fill="{TEAL}" fill-opacity="0.5" stroke="{INK}" stroke-width="3"/><line x1="0" y1="-14" x2="0" y2="14" stroke="{INK}" stroke-width="3"/>',
    "pencil": f'<path d="M-14 14 L-10 -4 L8 -18 L16 -10 L-2 8 Z" fill="{ORANGE}" stroke="{INK}" stroke-width="3"/>',
    "check": f'<circle r="18" fill="{GOOD}" fill-opacity="0.3" stroke="{INK}" stroke-width="3"/><path d="M-8 0 L-2 7 L10 -8" fill="none" stroke="{INK}" stroke-width="4" stroke-linecap="round"/>',
    "heart": f'<path d="M0 14 C-22 -2 -14 -18 0 -8 C14 -18 22 -2 0 14 Z" fill="{PINK}" stroke="{INK}" stroke-width="3"/>',
    "bulb": f'<circle cy="-4" r="12" fill="{SUN}" stroke="{INK}" stroke-width="3"/><path d="M-5 10 L5 10 M-4 16 L4 16" stroke="{INK}" stroke-width="3" stroke-linecap="round"/>',
}


def _icon_row(params: dict[str, Any]) -> str:
    items = list(params.get("items") or [])
    if not items:
        return _svg(900, 260, _text(450, 140, "暂无要点", 22))
    w = 900
    h = 120 + max(1, len(items)) * 62
    body = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            icon = str(item.get("icon", "star"))
            text = str(item.get("text", ""))
        else:
            icon, text = "star", str(item)
        y = 82 + i * 62
        body.append(f'<g transform="translate(150 {y})">{_ICONS.get(icon, _ICONS["star"])}</g>')
        body.append(_text(196, y + 8, text, 21, INK, anchor="start"))
    return _svg(w, h, "".join(body))


RENDERERS = {
    "life_scene": _life_scene,
    "apparatus": _apparatus,
    "object": _object,
    "icon_row": _icon_row,
}


def render(kind: str, params: dict[str, Any] | None = None) -> str:
    fn = RENDERERS.get(str(kind))
    if fn is None:
        return _svg(900, 300, _text(450, 150, f"暂不支持的插图：{kind}", 22))
    try:
        return fn(params or {})
    except Exception as exc:
        return _svg(900, 300, _text(450, 140, "插图参数有误", 22) + _text(450, 178, str(exc)[:80], 15, "#7a6149"))
