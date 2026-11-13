"""Courseware QA: static checks + per-page DOM overflow + screenshots.

Usage:
    python qa_courseware.py --plan plan.json --html out.html --out .cili/tmp/courseware/slug

Exit code 1 when blockers are found. The report is written to <out>/report.json.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

MAX_BYTES = 15 * 1024 * 1024
EXTERNAL_RE = re.compile(r"""(?:src|href)\s*=\s*["']https?://|url\(\s*["']?https?://""", re.I)


def static_checks(html_text: str, size: int) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    if size > MAX_BYTES:
        blockers.append(f"文件过大：{size / 1024 / 1024:.1f}MB（上限 15MB）")
    elif size > 10 * 1024 * 1024:
        warnings.append(f"文件偏大：{size / 1024 / 1024:.1f}MB（预览可能较慢）")
    if EXTERNAL_RE.search(html_text):
        blockers.append("存在外部 http(s) 资源引用，违反离线要求")
    if "{{" in html_text:
        # Template leftovers are only a blocker when a known slot is still present.
        leftovers = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", html_text)))
        if leftovers:
            blockers.append("模板槽位未替换：" + ", ".join(leftovers[:8]))
    if 'class="cw-scene' not in html_text:
        blockers.append("没有找到课件页面（cw-scene）")
    if "data-tex=" in html_text and "katex" not in html_text.lower():
        warnings.append("存在公式但没有检测到 KaTeX")
    return blockers, warnings


def run_browser(html_path: Path, out_dir: Path) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    blockers: list[str] = []
    warnings: list[str] = []
    pages_report: list[dict[str, Any]] = []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return blockers, [f"Playwright 不可用，跳过浏览器检查：{exc}"], pages_report

    out_dir.mkdir(parents=True, exist_ok=True)
    uri = html_path.resolve().as_uri()
    try:
        with sync_playwright() as p:
            browser = None
            for kwargs in ({"channel": "chrome"}, {"args": ["--allow-file-access-from-files"]}):
                try:
                    browser = p.chromium.launch(headless=True, **kwargs)
                    break
                except Exception:
                    continue
            if browser is None:
                browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
            page.goto(uri, wait_until="load")
            page.wait_for_function("window.cwReady === true", timeout=15000)
            total = page.evaluate("window.cwState.total")
            for i in range(int(total)):
                page.evaluate("(i) => window.cwGo(i)", i)
                page.wait_for_timeout(120)
                info = page.evaluate(
                    """() => {
                      const scene = document.querySelector('.cw-scene.is-active');
                      if (!scene) return {error: 'no active scene'};
                      const slide = scene.querySelector('.cw-slide');
                      const body = scene.querySelector('.cw-scene-body');
                      const sr = slide.getBoundingClientRect();
                      const br = body ? body.getBoundingClientRect() : sr;
                      const selectors = '.cw-text,.cw-bullets,.cw-callout,.cw-mistake,.cw-table-wrap,.cw-figure,.cw-quiz,.cw-columns,.cw-summary';
                      const overflows = [];
                      document.querySelectorAll('.cw-scene.is-active ' + selectors).forEach((el) => {
                        const r = el.getBoundingClientRect();
                        if (!r.width || !r.height) return;
                        if (r.bottom > br.bottom + 8 || r.right > sr.right - 34 || r.left < sr.left + 34) {
                          overflows.push((el.className || el.tagName) + ' @' + Math.round(r.left) + ',' + Math.round(r.top));
                        }
                      });
                      const broken = Array.from(scene.querySelectorAll('img')).filter(img => img.naturalWidth === 0).length;
                      const bodyOverflow = body ? body.scrollHeight > body.clientHeight + 4 : false;
                      const slideOverflow = slide ? slide.scrollHeight > slide.clientHeight + 4 : false;
                      const text = (scene.innerText || '').trim();
                      return {
                        index: scene.getAttribute('data-index'),
                        layout: scene.getAttribute('data-layout'),
                        bodyOverflow, slideOverflow, overflows, brokenImages: broken,
                        textChars: text.length
                      };
                    }"""
                )
                if info.get("error"):
                    blockers.append(f"第 {i + 1} 页无法检查：{info['error']}")
                    continue
                info["page"] = i + 1
                pages_report.append(info)
                if info["bodyOverflow"] or info["slideOverflow"]:
                    blockers.append(f"第 {i + 1} 页内容溢出（{info['layout']}）")
                if info["overflows"]:
                    warnings.append(f"第 {i + 1} 页元素越界：" + "; ".join(info["overflows"][:3]))
                if info["brokenImages"]:
                    blockers.append(f"第 {i + 1} 页有 {info['brokenImages']} 张图片加载失败")
                if info["textChars"] < 20 and info["layout"] not in ("section",):
                    warnings.append(f"第 {i + 1} 页内容偏少（{info['textChars']} 字）")
                shot = out_dir / f"page-{i + 1:02d}.png"
                page.screenshot(path=str(shot), full_page=False)
            browser.close()
    except Exception as exc:
        blockers.append(f"浏览器质检失败：{exc}")
    return blockers, warnings, pages_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", default="")
    parser.add_argument("--html", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--skip-browser", action="store_true")
    args = parser.parse_args()

    html_path = Path(args.html).resolve()
    out_dir = Path(args.out).resolve()
    if not html_path.is_file():
        print(json.dumps({"ok": False, "blockers": [f"HTML 不存在：{html_path}"]}, ensure_ascii=False))
        return 1
    html_text = html_path.read_text(encoding="utf-8", errors="replace")
    size = html_path.stat().st_size
    blockers, warnings = static_checks(html_text, size)
    pages: list[dict[str, Any]] = []
    if not args.skip_browser:
        b, w, pages = run_browser(html_path, out_dir)
        blockers.extend(b)
        warnings.extend(w)

    report = {
        "ok": not blockers,
        "html": str(html_path),
        "bytes": size,
        "pages": pages,
        "blockers": blockers,
        "warnings": warnings,
        "screenshots": sorted(str(p) for p in out_dir.glob("page-*.png")),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
