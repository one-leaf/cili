# Verification with the cili browser tool

A figure is done when it survives the browser, not when the file is written. Use the `browser` tool (WebGL is enabled in its Chrome).

## 1. Open the page

```
browser(action="navigate", url="file:///<absolute path>/figure.html")
browser(action="console")           # must be empty (no errors, no warnings)
```

Classic `<script>` tags work from `file://`; ES modules do not — if the console shows a CORS/module error, you edited the script tags away from the vendored UMD build.

## 2. Screenshot the states

```
browser(action="screenshot", screenshot_path="<outdir>/shot_default.png")
```

Then exercise the states:

- Desktop (default viewport) — default state.
- **Exploded / open state** — `browser(action="execute", script="document.getElementById('iso-canvas').click()")`, wait ~1 s for the damped motion to settle, screenshot again.
- **Dark theme** — `browser(action="execute", script="document.getElementById('iso-theme-btn').click()")`, screenshot.
- **Narrow** — resize the browser window and reload, or use the `snapshot` + `click` path with a narrow window; the minimum bar is that the card must not overflow at 390 px and the figure must still read.

## 3. Read the screenshots as a critic

- Console clean, no black canvas (black = WebGL context failure or a JS error).
- Every part rests on another; nothing floats except the exploded group.
- Exactly one `lit` accent is visible; if the accent is hidden behind the subject, move the accent or the camera elevation.
- Card labels fit; the footer readout shows real numbers.

## 4. Export (only when the user asks)

**PNG** (run via `browser(action="execute", script="...")`):

```js
const a = document.createElement('a');
a.download = 'figure.png';
a.href = fig.capture(3);   // 3× pixel density
a.click();
```

**PDF** — print the page at A4 landscape/portrait via the browser tool:

```
browser(action="save_pdf", pdf_path="<outdir>/figure.pdf")
```

The page prints the card as-is; set the card to fill the printed page with a print stylesheet (`@media print`) if the user wants a poster-sized sheet.

## 5. Delivery checklist

- [ ] `three.min.js` and `iso-kit.js` sit next to the HTML (the page must work offline, double-clicked).
- [ ] Console empty; both themes render; exploded state stays inside the frame.
- [ ] Readout/hint text is accurate (part count, state).
- [ ] Reduced motion: no idle animation was added, or it is gated on `fig.prefersReducedMotion`.
- [ ] Mention the PNG/PDF path in the reply when you produced one.
