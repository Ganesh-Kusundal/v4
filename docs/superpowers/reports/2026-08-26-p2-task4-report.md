# P2 Task 4 Report — frontend profiles + seasonality primitives

**Status:** DONE
**Date:** 2026-08-26
**Commit:** `HEAD` (see final commit hash in the task return)

## Summary

Added the frontend profile tier: `frontend/src/profiles.ts` (4 backend-driven
profile primitives), `frontend/src/seasonality.ts` (seasonality table
primitive), a shellbar "No Profile / Volume Profile / TPO / Market Profile /
Footprint / Seasonality" selector in `main.ts`, and a `.field--profile` style
in `index.html`. No profile/seasonality math in JS — the chart POSTs its
visible window (`lastRawBars`) to `POST /api/charts/profiles/{id}` and draws
the returned cells.

## Reference primitive per renderer

- **volume-profile** → `openalgo-charts-master/src/profile/volume-profile-primitive.ts`
  (`VolumeProfile`). Ported the total-mode histogram geometry (right-anchored
  bars, `width` = 90px, opacity 0.85, VA highlight fill, POC/VAH/VAL lines +
  labels via `rc.priceScale.format`). Our backend returns a single profile (no
  `sessions`/`options`), so the histogram is anchored to the plot's right edge
  and `tick` is inferred from the smallest bucket gap instead of read off
  `result.options.tickSize`.
- **tpo** → `market-profile-primitive.ts` (the TPO letter grid). The backend
  `{buckets: [{price, count}], poc, vah, val, ib}` is drawn as `count`
  letter-columns per row packed left from the right edge, with letters
  `chr(65 + col % 26)`, plus the IB box (bracketed line at the left edge) and
  POC/VAH/VAL lines. `ib` box: `result.ib.{high,low}`.
- **market-profile** → `market-profile-primitive.ts` (`MarketProfile` /
  `_drawSession`). The backend `{sessions, options}` maps field-for-field onto
  the reference's `MarketProfileResult`, so this is a near-verbatim port of
  `_drawSession`: `rc.dataLayer.timeToIndex(s.startTime/endTime)` →
  `rc.timeScale.indexToX`, row height from `options.tickSize * rowTicks`,
  period-colour TPO blocks with `letters[j]` per period, the `auto` letter→brick
  fade, VA fill, single prints, IB box, POC/VAH/VAL lines (POC thickness 2), and
  the guarded session label.
- **footprint** → `footprint-primitive.ts` (`Footprint` / `_drawColumn`). The
  backend returns ONE aggregated bar (`{time, cells: [{price, bidVol, askVol}],
  delta}`), so one column is drawn: bid (left, down-colour) / ask (right,
  up-colour) cells with `compactVolume` numbers, heat = `value/peak` via
  `globalAlpha`, POC tick, range line behind, plus a compact Volume/Δ stats
  strip at the bottom. `autoscaleInfo` reports the cell price extent (as the
  reference does).

## Seasonality table rendering

`createTier2Indicator` does not forward a `table` hook, so seasonality is a
pane primitive (`SeasonalityPrimitive implements IPrimitive`), not an
indicator. It wraps the library's exported `ChartTable` (the same primitive the
reference uses — `primitives/table.ts`). `setData(result)` forwards the
backend's `rows` straight into `ChartTable.setRows(rows as TableCell[][])` and
maps `options` (`position`, `cellWidth` (13 columns: `[52, 46×12]`),
`cellHeight`, `rowWeights`, `widthPercent`, `heightPercent`, `fontSize`,
`margin`) onto `ChartTable.setOptions`. Cell `bgColor`s (hex with alpha byte,
e.g. `#787b8633` for headers, green/red heat via `#089981`/`#F23745`+alpha)
render directly; `ChartTable` picks contrasting ink via `contrastText`. Verified
the backend output shape against the library's `ChartTableOptions`/`TableCell`
contracts before wiring.

## Shellbar wiring (`main.ts` + `index.html`)

- `applyProfile(id)` mirrors the transform switch pattern: it reads
  `lastRawBars` (the same source `applyTransform` uses), POSTs to the backend,
  `chart.addPrimitive(primitive, 0)` on the price pane, and sets
  `activeProfile`. "None" and re-selection call `clearProfile()`, which
  `chart.removePrimitive()`s the previous primitive (no leak).
- Footprint detail: the backend's `compute_footprint` consumes classified
  trades, and the route contract makes `body.bars` the trades list — so the
  frontend shapes bars into deterministic bid/ask prints via `barsToTrades`
  (one ask at high for volume/2, one bid at low for volume/2, the golden rule)
  and sends `params.time = bars[0].time`. Data shaping only, no profile math.
- The selector is built in JS (the shellbar is JS-populated) with options
  None + 4 profiles + Seasonality; `index.html` only gained a
  `.shellbar .field--profile` style (brass tint to distinguish it from the
  transform select).
- Profiles auto-detach on `loadHistory`, `resetToRaw`, and `applyTransform`
  (stale-data guard).

## Verify

```
cd frontend
npm run typecheck   # exit 0
npm run build       # vite build succeeds (21 modules, dist built)
```

Both green. Backend contract re-verified against the running analytics
modules: volume-profile returns 138 buckets + poc/vah/val, tpo returns buckets
+ ib, market-profile returns 1 session with 138 levels + options, footprint
over the shaped trades returns 71 cells + delta + time, seasonality returns a
6×13 `{rows, options}` table matching `ChartTableOptions`.

## Library-API surprises

- `ChartTable` is exported from the base bundle (not just the reference tree),
  which let the seasonality table reuse the library's own primitive instead of
  re-porting layout code.
- `createChart().addPrimitive(primitive, paneIndex)` / `removePrimitive` are
  the chart-level surface; `IPrimitive`/`PrimitiveHost`/`PrimitiveRenderContext`
  match the reference contract exactly.
- `rc.dataLayer.timeToIndex` + `rc.timeScale.indexToX` are the correct
  time→x mapping (market-profile sessions anchor to real bar times).
- `compactVolume` is exported for the footprint numbers.
- Minor: pane-plot canvas is exactly `plotWidth` media-px wide, so the
  reference's "label at `x1 + 3px`" only works because a session edge leaves a
  right gutter; the full-window volume/TPO primitives anchor at the plot edge
  and draw their labels right-aligned just inside it.