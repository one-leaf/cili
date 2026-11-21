---
name: 等轴测结构图
description: 用 Three.js 生成单页离线 HTML 的等轴测立体结构图：正交相机、四档面色调、清晰描边、卡片式取景框、平缓动效、点击拆解、明暗主题，可导出 PNG/PDF。Use when user mentions 等轴测、立体结构图、三维结构图、爆炸图、拆解图、剖视图、透视图、3D 示意图、三维示意图、机器原理图、装置插画, or asks for an interactive isometric illustration that explains how a device, system or algorithm works.
roles: [master, worker]
---

# Isometric Structure Figures

Deliver **one offline HTML page** whose figure explains an idea as a real physical object: a small machine on a bench, a cutaway house, a test rig — built from many small precise parts, drawn isometrically, with a card frame and calm motion.

## What "good" looks like

- **The object is the explanation.** Don't draw labelled boxes and arrows. Invent a real, well-made thing whose working *is* the concept (a pin-tumbler lock for a cache key, a chest of drawers for render targets, a beam carriage for a shared resource).
- **Many small parts, all resting on each other.** Base plate with corner studs, posts, rails, carriage, knob, indicator. 15–40 parts for a Figure, 40–80 for a dense Hero.
- **One accent.** Exactly one part is `lit` (ink-black in the light theme, white in dark) — the part the reader should watch.
- **Calm.** Fixed camera, slow damped motion, one interaction (click to explode/toggle). No auto-spin, no popping.

## Assets

| File | Purpose |
|---|---|
| `$SKILL_DIR/assets/three.min.js` | Three.js r149 UMD (classic script; works from `file://`, no build) |
| `$SKILL_DIR/assets/iso-kit.js` | IsoKit: orthographic iso camera, four-tone shader, edges, parts, fit, damp, orbit, pick, capture |
| `$SKILL_DIR/assets/template.html` | Working single-page card template — start from this |
| `$SKILL_DIR/examples/isometric-house.html` | Reference figure: cutaway house, click lifts roof, theme toggle |

First step every time: copy the two scripts next to the output page, then copy `template.html` to the output path and edit it.

```bash
cp "$SKILL_DIR/assets/three.min.js" "$SKILL_DIR/assets/iso-kit.js" <outdir>/
cp "$SKILL_DIR/assets/template.html" <outdir>/figure.html
```

## Workflow

| Phase | Sub-file | When |
|---|---|---|
| Craft rules, camera math, palette, part recipes | `references/craft.md` | read before writing geometry |
| Verification (browser screenshots, console, PNG/PDF) | `references/verify.md` | before delivering |

1. **Size it.** Figure (one mechanism, 15–40 parts, one interaction) — build it immediately. Hero (dense scene, 40–80 parts, several interactions) — say what it will contain, then build it.
2. **Truth first.** Collect the real numbers (sizes, counts, thresholds). Every readout and motion must be true; never invent numbers for effect.
3. **Invent the object.** One sentence: what moves, and what that motion means. Write the parts list *before* any geometry.
4. **Layout.** World units ≈ final-drawing pixels; x right, y up, z toward the viewer-left; parts axis-aligned unless a slope requires rotation. Default camera azimuth 45°, elevation 30–38°.
5. **Build.** IsoKit calls only (`box`, `cylinder`, `lathe`, `tube`, `sphere`), parts snapped onto each other, then `fig.fit()`. No floating parts — every part sits on another, or is deliberately part of an exploded group.
6. **Shade.** `lo` base, `mid` structure, `hi` subject, exactly one `lit`. Let the kit quantize face tones; never set per-face colors by hand.
7. **Frame it.** Card header: `Fig n` + short title. Card footer: interaction hint + live readout. Words stay out of the drawing.
8. **Make it live.** `fig.enableOrbit(handler)` for drag-rotate + click; `fig.moveBy()` for damped explode. Respect `fig.prefersReducedMotion` (no idle animation).
9. **Verify.** Open with the browser tool, check console, screenshot at 1280 and 390 width, dark theme, exploded state. Iterate until it reads as a real object. See `references/verify.md`.

## Non-negotiables

1. Offline single page: no CDN, no npm, no build step. Only the vendored files.
2. Solids with edges — never wireframes, dashed hidden lines or floating labels.
3. Every part rests on another part; nothing floats except an exploded group, and that only after a click.
4. Exactly one `lit` accent per figure.
5. Calm, user-triggered motion. No idle spin. Reduced motion → still frame.
6. Console must be clean and the page must read at 390 px wide before you deliver.

## Gotchas

| Gotcha | Solution |
|---|---|
| External ES modules are CORS-blocked on `file://` | Use the vendored UMD `three.min.js` via classic `<script>`, never `type="module"` |
| Coplanar faces z-fight (panels, inlays, ticks) | Offset by ≥ 0.5 world unit; the kit's materials already carry polygonOffset so edges stay on top |
| `fig.pick()` returns the outermost object under the scene | Attach knobs/plates as **children** of the part they belong to, so they move together and pick as one |
| `fig.moveBy()` is a relative offset, not absolute | Toggle back with the negated offset (see the house example's `roofOffsets`) |
| `fig.fit()` measures the scene at rest | Leave the default margin; a large explode can still clip — verify the open state |
| Lathe profiles are `[[radius, y], ...]` | Ascending or descending y is fine; radius 0 closes the end |
| Cylinders are y-axis by default | Rotate `[Math.PI / 2, 0, 0]` for a knob facing +z |
| Sloped roofs/wings | `rotation.x = angle`, center at mid-run between ridge and eave (worked example in `references/craft.md`) |
| Chinese UI text garbles | `<meta charset="utf-8">` in the page head |
| Headless screenshots need software WebGL | Use the cili browser tool; it launches with WebGL enabled |
