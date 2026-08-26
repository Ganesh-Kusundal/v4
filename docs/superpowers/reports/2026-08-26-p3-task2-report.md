# P3 Task 2 Report — frontend trade.ts (TradingController host)

**Status:** DONE
**Commit:** `1fe06dc`
**Date:** 2026-08-26

## Controller choice: `TradingController` (base bundle) + `TradeMarkersPrimitive`

Used the plan's decided choice — the base bundle's `TradingController` as the
state holder with `TradeMarkersPrimitive` for on-chart markers — rather than the
richer trade-tier `TradeController`.

- The base `TradingController` (reference `core/trading-controller.ts:217`)
  takes a `TradingHost` and renders price-line pills for orders/positions with
  `pnlText`, exactly the `/book` vocabulary. Its `syncState({orders, positions})`
  is idempotent (diff-by-id + signature), which is what the mount's
  stop→start and WS-triggered re-syncs need.
- The trade-tier `TradeController` (reference `trade/trade-controller.ts:21`)
  is **host-compatible** too — `TradeHost` is only `{addPrimitive, removePrimitive}`,
  which the chart provides — so the plan's "use only if compatible" condition is
  satisfied. It was **not** used because (a) the brief's mappers target the base
  `TradingOrder`/`TradingPosition` shapes, (b) `TradeController.reconcile` keys
  by `symbol` and needs `role`/`triggerPrice` per order, and `/api/charts/book`
  does **not** carry `role`/`parentId` today, so its `BracketGroup` path has no
  data to draw, and (c) it would duplicate the drawing role that the P2/backtest
  `chart.trading` (the chart's own internal controller) already serves for
  trade-fill markers. Upgrade path: if Task 3's bracket data adds `role`, swap
  the mapper to produce trade-tier `Order`/`Position` and call
  `TradeController.reconcile` — the host seam (`sync(book)`) is unchanged.

## How the chart is the host

The chart instance implements `TradingHost` directly — reference
`core/chart.ts:650` constructs `new TradingController(this)` inside the `trading`
getter, and the class declares `addPrimitive(p, where?)`, `removePrimitive(p)`,
`subscribeClick(cb)`, `subscribeDrag(onDrag, onDragEnd?)`, and `emit(event, payload)`.
So `createTradingHost` passes `chart` straight to `new TradingController(chart as never)`.
Verified against the installed `dist/index.d.ts` (Chart class: `addPrimitive`
line ~3170, `subscribeClick` ~3115, `subscribeDrag` ~3131, `emit` ~3154) — the
three-arg `onDrag(externalId, price, time)` is structurally compatible with
`TradingHost.subscribeDrag`'s two-arg callback. Markers attach to pane 0 via
`chart.addPrimitive(markers, 0)` — the same `chart.addPrimitive(prim, paneIndex)`
pattern the P2 profile primitives use (`main.ts` seasonality/profile).

## Verified `unrealizedPnl` / `isWorking` signatures

Both come from `openalgo-charts/trade` (NOT the base bundle):

- `unrealizedPnl(position: Position, ltp: number): number` where
  `Position = { symbol: string; netQty: number; avgPrice: number }`.
  Returns `(ltp - avgPrice) * netQty` — already signed, so shorts need no side
  argument. **The plan's guessed `(size, avgPrice, ltp, side)` signature is wrong**;
  the mapper calls it as `unrealizedPnl({ symbol, netQty, avgPrice }, ltp)`.
- `isWorking(o: Order): boolean` where `Order.status: 'pending'|'working'|'partial'`
  are working. The backend `/api/charts/book` (`routes/chart.py` `map_order_status`)
  already emits the trade-tier status vocabulary, so `isWorking(o as never)` applies
  directly — no string mapping needed.

## Shellbar wiring

- `index.html`: added a static `#tradetoggle` `.tbtn` button in `<header#shellbar>`
  (marked `hidden` so there's no pre-JS flash).
- `main.ts`: `createTradingHost(chart, tradeFeed, getLtp)` is built right after
  `ensureSeries()`; `loadHistory()` calls `tradeHost.stop(); void tradeHost.start()`
  so symbol/interval changes drop stale markers and re-sync (covers the watchlist
  path too, since it routes through `loadHistory`). The Trade pill is unhidden,
  defaulted `is-on`, and `shellbar.append(...)` moves it next to Buy/Sell; clicking
  toggles `is-on` and calls `start()`/`stop()` — mirroring the Compare/Settings
  buttons' JS-wired toggle pattern.
- `getLtp`: synchronous last-close of `lastRawBars` for the chart's symbol; other
  symbols degrade to entry price (flat PnL). No live LTP feed exists in the shell
  yet — this is the graceful-degradation path the mapper's `?? p.avgPrice` was
  designed for.

## Typecheck / build output

```
npm run typecheck   → exit 0 (tsc --noEmit, no errors)
npm run build       → success
  vite v6.4.3 building for production...
  ✓ 23 modules transformed.
  dist/index.html                  24.74 kB │ gzip:   5.93 kB
  dist/assets/index-BHBwZMKw.js   424.46 kB │ gzip: 113.51 kB
  ✓ built in 628ms
```

## Library-API surprises

1. **`TradingOrderType` has no `"market"`** — the base union is
   `'limit' | 'stop' | 'stop_limit'`, but the brief mandates `LIMIT→"limit", else
   "market"`. The controller only uses `type` as the pill's left label, so a
   working MARKET order maps through as `"market"` via one `as TradingOrder["type"]`
   cast (same cast convention the shell already uses, e.g. `as never` series data).
2. **`unrealizedPnl`'s real signature** is `(position, ltp)`, not the plan's
   guessed `(size, avgPrice, ltp, side)` — see above.
3. **`ChartBook` name collision**: `trade-feed.ts` already exports a loose
   `ChartBook { orders: unknown[]; positions: unknown[] }`. `trade.ts` imports
   that type for the host seam and casts rows to the typed `BookRow`/`BookPosition`
   at the sync boundary rather than defining a conflicting interface.
4. **`TradingController` self-manages its own trade-fill markers** (internal
   `TradeMarkersPrimitive` created on `setTrades`); our separately-attached
   `TradeMarkersPrimitive` sits alongside it harmlessly (draws nothing without
   trades) and needs an attachment guard so `stop()` → `start()` re-adds it.

## Files changed

- `frontend/src/trade.ts` (new) — mappers + `createTradingHost`
- `frontend/src/main.ts` — host construction, `loadHistory` teardown, Trade pill
- `frontend/index.html` — static `#tradetoggle` shellbar button

Constraints respected: no backend files touched; `trade-feed.ts`, `feed.ts`,
`backend-indicators.ts`, `transforms.ts` untouched; no order math invented
(`unrealizedPnl`/`isWorking` are the only PnL/work logic).