# P1 Task 4 Report — frontend chart-types (transforms.ts)

**Date:** 2026-08-26
**Task:** P1 Task 4 — backend-driven chart-types + shellbar switch
**Status:** DONE

## What was delivered
- `frontend/src/transforms.ts` — self-contained transform tier module:
  - `TRANSFORMS` list of the 6 backend transforms (id, name, kind, params), matching the backend `TRANSFORM_SPECS`.
  - `computeTransform(id, params, bars)` — POSTs the visible window to `/api/charts/transforms/{id}` and returns `Bar[]`. No transform math in JS.
  - `transformById(id)` helper.
- `frontend/src/main.ts` — shellbar `<select>` (None + 6 transforms) + `applyTransform()`/`resetToRaw()` series-swap logic mirroring the existing `loadHistory()`/`ensureSeries()` replace pattern.
- `frontend/index.html` — `.field--tf` select styling (dark theme), plus the module loads via `main.ts`.

## Renderer wiring decision — side-effect + explicit, verified
I used **both**: a bare `import "openalgo-charts/transform"` side-effect at the top of `transforms.ts`, **and** a call to the tier's exported `registerTransformChartTypes()`. Verified against the built bundle:
- `openalgo-charts/package.json` marks the tier as a side effect (`"sideEffects": ["**/transform/**", ..., "./dist/openalgo-charts.transform.mjs"]`), so Vite keeps the bare import.
- Post-build inspection of `frontend/dist/assets/index-*.js` shows **both** the base registry guard (`new Set(["point-figure","kagi"])` with the error "needs the transform tier") and the inlined tier registration (`registerChartType("point-figure", ...)` / `registerChartType("kagi", ...)`). So `chart.addSeries("point-figure"/"kagi")` resolves at runtime.
- Calling `registerTransformChartTypes()` explicitly is idempotent (`_registered` guard) and guarantees registration even if a future bundler tree-shakes the bare import — a cheap belt-and-suspenders against exactly the failure the brief warned about.

## How point-figure / kagi series were added
- The tier (not my module) does the `registerChartType` calls — re-registering from `transforms.ts` would double-register, so I did **not**.
- `SeriesType` in the library already includes `'point-figure' | 'kagi'` (index.d.ts:652), so `chart.addSeries(t.kind)` typechecks with **no cast** once `t.kind` is narrowed out of `"candlestick"`.
- In `applyTransform()`: for `kind === "candlestick"` (heikin-ashi/renko/range-bars/line-break) I reuse the existing `priceSeries` with `setData(transformedBars)` (volume derived from the same transformed bars). For `point-figure`/`kagi` I hide `priceSeries` + `volumeSeries` via `applyOptions({ visible: false })` and add a dedicated `transformSeries = chart.addSeries(t.kind)` fed the transformed bars. `SeriesStyle.visible` exists (index.d.ts:543) so no cast needed there.
- `resetToRaw()` restores raw candles: removes `transformSeries`, un-hides price/volume, and re-sets the cached `lastRawBars` (cached in `loadHistory`). Interval/symbol changes reset the select to "None".

## Typecheck / build output
```
> tsc --noEmit
(exit 0, no output)

> vite build
vite v6.4.3 building for production...
✓ 19 modules transformed.
dist/index.html                 24.52 kB │ gzip:   5.84 kB
dist/assets/index-By_9ivDs.js  408.16 kB │ gzip: 108.94 kB
✓ built in 692ms
```
Both `npm run typecheck` and `npm run build` pass (exit 0).

## Library-API surprises
- `SeriesType` already includes `'point-figure' | 'kagi'`, so no `as never` was needed for `addSeries` — the type union is permissive here.
- The base bundle **does not** ship the point-figure/kagi renderers — it only lists them in a "needs the transform tier" guard set. The renderers arrive exclusively via the `openalgo-charts/transform` tier import. This confirms the brief's warning was real.
- `SeriesStyle.visible` is a first-class field, so hiding series for the custom chart-types is a clean `applyOptions` call rather than a remove/re-add dance.
- `computeTransform` uses `encodeURIComponent(id)` in the URL path (safe for the hyphenated ids).

## Constraints respected
- No backend files touched. No transform math in JS.
- Only `frontend/src/transforms.ts`, `frontend/src/main.ts`, `frontend/index.html` modified/added.
- `feed.ts`, `backend-indicators.ts`, `trade-feed.ts`, and all `shell/*` modules untouched.

## Fix round 1

Two review findings fixed in `frontend/src/main.ts` (commit `a4d249d`):

1. **Stale transform on failed switch** — the transform `<select>` change handler's catch block now calls `resetToRaw()` (the same path the "none" option uses) instead of only setting `transformSel.value = "none"`, so raw candles are restored when a transform switch fails (e.g. a renko/point-figure series left over in `priceSeries`/`transformSeries`).
2. **Ordering fragility** — chose the forward-declare hardening over the comment: `let transformSel: HTMLSelectElement;` now sits with the other `let` declarations near the top of module scope, and the `const transformSel = document.createElement(...)` became a plain assignment. All `resetToRaw()` call sites (`loadHistory`, `applyTransform`) remain post-init.

Commands:
```
npm run typecheck   # exit 0 (tsc --noEmit, no output)
npm run build       # exit 0 — vite v6.4.3, 19 modules, dist/index.html 24.52 kB, dist/assets/index-C1REprqP.js 408.15 kB (gzip 108.94 kB), 659ms
```

Commit hash: `a4d249dc32373fbb04fd1ee3ae94c233f345dff2`
