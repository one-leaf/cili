# Craft: camera, palette, parts

Read this before writing geometry. Most of the visual quality is decided here, not in the code.

## Camera and world

- **World units**: about 1 final-drawing pixel. A desk machine is ~120 wide; a room is ~200–240.
- **Axes**: x right, y up, z toward the viewer-left. Keep parts axis-aligned whenever possible; slopes are the only rotation you normally need.
- **Orthographic camera**, not perspective. The kit places it at `azimuth` (around y) and `elevation`; defaults 45° / 30°. Elevation 34–38° reads better into open interiors (rooms, cabinets). True isometric is elevation `atan(1/√2) ≈ 35.26°`.
- **Fit**: `fig.fit()` computes the scene's screen-space extent from the 8 bounding-box corners and sets the ortho frustum. It measures the scene *at rest* — verify the exploded/open state too.
- Composition: the object should fill roughly 55–65% of the card. The card canvas is 4:3 (1:1 under 480 px).

```js
const fig = IsoKit.figure({ canvas, elevation: 36 });  // azimuth defaults to 45
fig.fit(1.18);                                          // optional margin
```

## Tone system

The kit's shader quantizes the world-space face normal against a light direction `(-0.45, 0.85, 0.30)`:

| Face | dot > 0.80 | > 0.50 | > 0.22 | else |
|---|---|---|---|---|
| Level | 1.00 (top) | 0.88 | 0.74 | 0.62 (backlit) |

Part base colors come in four keys — use the key, never a CSS color, so theme switching works:

| Key | Role | Light | Dark |
|---|---|---|---|
| `lo` | base, feet, guards | `#d9d3c6` | `#2c3138` |
| `mid` | structure: posts, rails, walls | `#cdc5b5` | `#3a4047` |
| `hi` | the subject: carriage, roof, furniture | `#b8ae9a` | `#4a525b` |
| `lit` | **the one accent** | `#22221f` | `#eef1f3` |

Rules:
- Base and feet `lo`, structure `mid`, the subject `hi`, exactly one `lit`.
- Grooves/inlays may use `lo` on `mid`; a fixed CSS color (e.g. `'#262d34'` window glass) is fine for a non-theme detail that must stay dark — it just won't switch with the theme.
- Do not hand-pick per-face colors; the face quantization already produces the four tones.

## Part recipes

| Want | Call |
|---|---|
| Plate, slab, beam, furniture | `IsoKit.box(w, h, d, { color, pos: [x, y, z] })` — geometry is centered, so `pos[1]` is the center height |
| Post, leg, peg, stud | `IsoKit.box(...)` or `IsoKit.cylinder(r, r, h, { segments: 16–24 })` |
| Knob, flange, bell, funnel | `IsoKit.lathe([[r, y], ...], { segments: 20–32 })` |
| Pipe, cable | `IsoKit.tube([[x, y, z], ...], radius, { tubularSegments: 48 })` — edges are off by default |
| Ball, lamp bulb | `IsoKit.sphere(r, { segments: 16 })` |

**Stacking**: each part body must land on a real face of another part. Example: base slab spans y 0–8, so posts have `h = 54`, centre `y = 8 + 27 = 35`, giving y 8–62; the beam spans 62–68; a hanging carriage spans 48–62.

**Knobs and attached details**: create them relative to their parent and `parent.add(child)` — `pos` is then parent-local. This keeps `fig.pick()` and `fig.moveBy()` treating the assembly as one part.

**Panel insets** (windows, screens, doors on a wall): place the panel 0.5–1 unit proud of the wall face, e.g. wall inner face at `z = -67`, frame at `-67`, glass at `-65.8`.

**Corner studs**: `cylinder(1.8, 1.8, 1.2)` sitting on the plate top — small details that make the base read as a machined part.

## Slopes (roofs, wings, ramps)

A roof slope from ridge `(y0, z=0)` to eave `(y1, z=R)`:

```js
const angle = Math.atan2(y0 - y1, R);        // rotation.x = +angle tilts the slab toward +z
const len   = Math.hypot(y0 - y1, R);        // slab depth along the slope
const midY  = (y0 + y1) / 2;
const midZ  = R / 2;
IsoKit.box(length, thickness, len, { pos: [0, midY, midZ], rot: [angle, 0, 0] });
```

Mirror with `rot: [-angle, 0, 0]` and `pos z = -midZ`. A **cutaway** (剖切) figure keeps only the far slope (`-z`), so the interior stays visible from the default camera — this is the recommended default for rooms, cabinets and vehicles; keep the near side open.

## Motion

- `IsoKit.damp(current, target, halfLife, dt)` — frame-rate-independent exponential follow. Half-life 0.15–0.3 s feels calm.
- `fig.moveBy(obj, [dx, dy, dz], halfLife)` — damped relative move to a target; repeated calls retarget instead of stacking.
- Explode pattern: keep a `Map` of part → offset (as in the house example) and toggle with the negated offset.
- Idle motion is off by default. If you add one, gate it on `!fig.prefersReducedMotion`.

## Interaction

```js
fig.enableOrbit((event) => {           // drag-rotate; fires only on real clicks (< 6 px movement)
  const hit = fig.pick(event);         // outermost scene child with a baseKey
  if (hit === target) { /* toggle */ }
});
```

- Orbit limits are elevation 12–78° inside the kit.
- Readout: `fig.setReadout('零件 36 · 屋顶：合上')`; hint: `fig.setHint('拖动旋转 · 点击掀开屋顶')`.
- Theme: `fig.setTheme('dark' | 'light')` re-tints every registered part, the background and the edges.

## Composition checklist

- At 1×: instantly a real object, many small parts, nothing floating, every edge ends on another surface.
- At 4×: no bare empty corner; each region has a true, purposeful detail.
- At 390 px wide: the silhouette still reads; the card footer text doesn't wrap into the canvas.
