# P4 Task 3 Report — chrome primitives (primitives.ts)

**Date:** 2026-08-26
**Status:** DONE
**Files:** `frontend/src/primitives.ts` (new), `frontend/src/main.ts` (wired)

## Summary

Added a `Chrome` manager (`frontend/src/primitives.ts`) that owns the seven reference primitives — `LogoWatermark`, `SeriesMarkers`, `PriceLevels`, `PaneLegend`, `EventMarkers`, `BuySellButtons`, `ChartTable` — and wired it into `main.ts`: constructed after the chart exists, `setContext` fed from `loadHistory`, and a shellbar "Chrome" toggle (default ON, mirroring the Link toggle). Pure chrome; no backend touched.

## Per-primitive mounting (all via `chart.addPrimitive(p, 0)`)

| Primitive | Mount | Notes |
|---|---|---|
| `LogoWatermark` | `new LogoWatermark({ src, position: "bottom-left", opacity: 0.7, label: "TradeX v4" })` | Library has **no text-only mode** — it needs an image. Built a tiny inline SVG "T" glyph data-URI as the mark; `label` unrolls on hover. |
| `SeriesMarkers` | `series.createMarkers()` on the price series (`SeriesApi`) | `createMarkers()` is the SeriesApi path: it resolves the series' numeric `SeriesId` at call time and attaches the primitive to the series' pane itself (`chart.ts` `createMarkers` → `new SeriesMarkers(dataId)` + `_addPrimitive`). Fed nothing (empty until a signal feed exists). |
| `PriceLevels` | `new PriceLevels({ timezone: "Asia/Kolkata", levels: { previousClose, sessionHigh, sessionLow all on } })` | The meaningful one — see feeding section. |
| `PaneLegend` | `new PaneLegend({ id: "chrome-symbol", title: "EXCH:SYMBOL", actions: [] })` | `actions: []` → pure text row, no buttons. `status` fed symbol ticker + last-day change % via `setContext`. |
| `EventMarkers` | `new EventMarkers()` | Empty; no corporate-actions calendar in the shell. Renderer ships regardless. |
| `BuySellButtons` | `new BuySellButtons({ id: "chrome", position: "bottom-right", buyLabel: "BUY", sellLabel: "SELL", qty: 10 })` | Click routing below. |
| `ChartTable` | **NOT mounted** | See ChartTable-vs-seasonality section. |

## BuySellButtons click-routing decision (shared subscribeClick slot)

The chart's `subscribeClick` is a **single-slot** API — `chart.ts` stores `this._clickCb = cb`, overwriting any prior handler. The `TradingController` (built in `trade.ts:createTradingHost`, P3) claims that slot in its constructor (`trading-controller.ts:229` `host.subscribeClick(...)`). So wiring BuySellButtons through `subscribeClick` would steal the trade controller's slot.

**Decision:** BuySellButtons clicks are routed through the chart's unified **multi-listener `on('click')` bus** instead. The chart emits `'click'` with `{ id: hit?.externalId, price, time, paneIndex, point }` on every clean click (including primitive hits) — `chart.ts:2880`. The `Chrome` manager subscribes `host.on("click", ...)`, matches `id === "chrome:buy"` / `"chrome:sell"`, and calls the shell's `orderAction` — `main.ts` wires it to the existing `placeOrder(side)` (same path as the shellbar BUY/SELL buttons; the on-chart panel is a duplicate control). No slot steal, no double placement (TradingController's `_onClick` ignores non-`ord:`/`pos:` ids).

## PriceLevels feeding

**Library surprise:** `PriceLevels` does **not** accept fed prices. Its `draw` computes every level's value itself every frame from the bars in view (`price-levels.ts` `draw` → `computePriceLevels({ bars, anchorTime, timezone, ... })`). The `levels`/`setLevel` options only control **style** (line/label toggle, color, text). There is no `setPrice` API.

So `setContext` cannot (and should not) inject the shell's prevClose/sessionHigh/sessionLow as static numbers — that would freeze the axis tags while the lines keep moving with the live bar values, and the library's own session-in-view semantics (right-edge session) are more accurate than a whole-window max/min. `setContext` therefore:
- switches the opt-in `sessionHigh`/`sessionLow` levels **on** (they're off in library defaults) and re-asserts `previousClose` on;
- feeds the live `lastPrice` mark to `BuySellButtons.setMark`;
- feeds symbol + change-% (from `ctx.lastPrice` vs `ctx.prevClose`) into the `PaneLegend` status line.

The shell still computes the ctx numbers in `loadHistory` as the plan specifies (prevClose = first loaded bar's close, sessionHigh/Low = window extremes) — they feed the legend change reading and future consumers, and `setContext` is re-called on every symbol/interval change.

## ChartTable vs seasonality

`ChartTable` is **not mounted** by `Chrome`. Reasons:
- An empty table draws nothing (`table.ts` `draw` early-returns on zero rows) — mounting one is a silent no-op.
- Seasonality already owns the table surface: `seasonality.ts` wraps its own `ChartTable` instance inside `SeasonalityPrimitive`. Double-mounting a second empty table would add a dead primitive with zero benefit.

If a future chrome consumer wants a real table (e.g. a signal scoreboard), `Chrome` can attach its own `ChartTable` then.

## Verify output

```
$ cd frontend
$ npm run typecheck
> tsc --noEmit            # exit 0

$ npm run build
> vite build
✓ 26 modules transformed.
dist/index.html                 25.21 kB
dist/assets/index-CRE8OHJS.js  453.97 kB
✓ built in 653ms                # succeeds
```

## Library-API surprises

1. `subscribeClick` is single-slot and owned by `TradingController` → BuySellButtons routed via `on('click')` bus (documented above).
2. `PriceLevels` self-feeds from bars; `setLevel` is style-only, no price setter → context feeding is enable + mark/legend, not number injection.
3. `LogoWatermark` requires an image (`src`/`image`); there is no text-only option → inline SVG data-URI glyph used.
4. `SeriesMarkers(seriesId)` needs a numeric `SeriesId`; `SeriesApi.createMarkers()` binds + attaches it for you → used that instead of hand-managing the id.
5. `createMarkers()` auto-attaches to the chart, so `Chrome` tracks it for `disable()` but does not call `addPrimitive` on it again (avoid double-add).

## Constraints honored

- Only `frontend/src/primitives.ts` + `frontend/src/main.ts` changed; no backend, no other shell modules, no plan/spec/ledger edits.
- Library primitives used as-is; no reimplementation.