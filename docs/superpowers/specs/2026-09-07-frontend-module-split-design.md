# Frontend Module Split — Design Spec

**Date:** 2026-09-07
**Status:** Approved
**Scope:** `frontend/src/main.ts` → focused modules + surgical cleanups
**Tests:** 149/149 vitest passing; 7-screenshot E2E smoke test as regression guard

---

## 1. Context

`frontend/src/main.ts` is ~700 lines and owns every concern: state persistence, chart creation, resize handling, series management, history loading, live bar binding, shellbar wiring (symbol button, timeframe pills, qty input, buy/sell buttons), transform/profile/indicator application, command palette rebuild, layout save/restore, overlay panel creation (watchlist, orders, strategies, draw rail), trade host creation, replay bar DOM construction, WebSocket control socket, logging, and the boot sequence.

This grew organically as features landed (bracket orders, indicators, replay, strategies panel) and each added wiring to the same file. The consequences:

- A **duplicate `createChart` call** at module scope (line ~50) runs immediately, then `mountChart()` (called 100ms later via `setTimeout`) creates a second chart on the same DOM node, leaking the first instance and its `ResizeObserver`.
- **`createReplayBar`** in `shell/replay-bar.ts` is dead code — `main.ts` builds its own replay bar DOM inline.
- **`trade.test.ts`** reimplements `mapBookRowToOrder` without the status validation check that the real `trade.ts` export has.
- The file is too large to hold in working memory, which is why the double-chart bug survived.

The design is otherwise sound: computation stays on the backend, the chart is render-only, idempotency is correct, WS multiplexing works, and the test coverage is meaningful. This spec is a structural cleanup, not a behavior rewrite.

---

## 2. Module Boundaries

```
frontend/src/
├── main.ts              # ~150 lines: boot, state, mount, wire top-level connections
├── chart.ts             # ~200 lines: chart lifecycle, series, history, live, transforms, profiles, indicators, layout
├── trade-shell.ts       # ~120 lines: buy/sell/qty, trade host, placeOrder wiring, status dot
├── replay.ts            # ~150 lines: ControlSocket, replay bar (createReplayBar), replay lifecycle, WS control msgs, logs
├── palette-shell.ts     # ~80 lines: command palette rebuild, overlay panel creation/wiring
└── [existing files unchanged]
```

### `main.ts` — boot + state + top-level wiring

**Owns:**
- `loadState()` / `persistState()` (URL params + localStorage)
- `createTradexFeed()` (the cached data feed)
- `TradexTradeFeed` instance (the trade feed — one per page)
- `createTradingHost(chart, tradeFeed, tradeFeed, getLtp)` call
- `createShellShortcuts(...)` call
- `createOverlay` calls for watchlist/orders/strategies/draw-rail (the overlay hosts themselves move to `palette-shell.ts`)
- Boot sequence: `registerBackendIndicators()` → `rebuildPalette()` → `mountChart()`
- Top-level `state` object, `persistState()` calls from symbol/interval changes

**Does NOT own:**
- Chart creation, series management, history loading, live binding → `chart.ts`
- Buy/sell/qty button wiring, `placeOrder`, status dot → `trade-shell.ts`
- Replay bar DOM, `ControlSocket`, replay lifecycle → `replay.ts`
- Command palette rebuild, overlay content wiring → `palette-shell.ts`
- `ResizeObserver` — moves to `chart.ts`

### `chart.ts` — chart lifecycle

**Owns:**
- `chartHost` lookup (`$("#chart")`)
- `createChart(chartHost, { theme, timezone, dataFeed, shortcuts })`
- `linkChart(chart)`
- `ResizeObserver` on `chartHost` → `chart.applyOptions({ width, height })`
- `priceSeries` / `volumeSeries` / `transformSeries` / `lastRawBars` / `activeProfile` state
- `currentPrice()`, `volumeBars()`, `showLastBars()`, `ensureSeries()`
- `resetToRaw()`, `applyTransform(id)`, `clearProfile()`, `applyProfile(id)`
- `loadHistory()` — the full history load + chrome context update + indicator settings sync
- `bindLiveBars()` — `chartFeed.subscribeBars` for live updates
- `renderChips()` — indicator chip rendering
- `rebuildPalette()` indicator portion (reads `catalogueById`) — or this stays in `palette-shell.ts`; see decision below
- `saveLayout()` / `restoreLayout()`
- `chrome` creation (`new Chrome(chart)`) and `chrome.setOrderAction(...)` — or this moves to `trade-shell.ts`; see decision below
- Crosshair move handler → legend DOM update
- `timeNav` (`TimeNavigator`) mounting
- `mountChart()` function — becomes the single chart creation path, called once from `main.ts` boot

**Does NOT own:**
- Symbol button, timeframe pills, qty input, buy/sell buttons → `trade-shell.ts` (or `main.ts` top-level if the shellbar is considered "chart chrome" — decision needed, see §3)
- Trade host creation → `trade-shell.ts`
- Replay bar → `replay.ts`
- Overlay panels → `palette-shell.ts`

### `trade-shell.ts` — order entry chrome

**Owns:**
- `symBtn` click → `watchlistOverlay.toggle()` (the button is in the shellbar; the overlay toggle is trade-shell's concern because it's the entry point to the watchlist selection flow)
- `tfPills` creation + click handlers (interval change → `loadHistory()` + `bindLiveBars()`)
- `qtyInput` lookup
- `placeOrder(side)` — reads `qtyInput.value`, calls `tradeHost.orderEngine.placeOrder(...)`, logs result
- `buy-btn` / `sell-btn` click → `placeOrder("BUY")` / `placeOrder("SELL")`
- `statusDot` / `statusText` — `setStatus()` + the `setInterval` that polls `wsLive && tradeWsLive`
- `tradeHost` creation: `createTradingHost(chart, tradeFeed, tradeFeed, getLtp)`
- `chrome.setOrderAction((side) => placeOrder(side))` — the buy/sell button routing
- `chrome.enable(lastChromeCtx, priceSeries)` — depends on chart being mounted, so this is called from `chart.ts` after chart creation, OR `trade-shell.ts` exports a `wireTradeChrome(chart, chrome)` that `chart.ts` calls. The latter is cleaner: `chart.ts` owns the `Chrome` instance, `trade-shell.ts` owns the order action.

**Does NOT own:**
- Chart creation, series, history → `chart.ts`
- Replay → `replay.ts`
- Overlay content → `palette-shell.ts`

### `replay.ts` — replay bar + control socket

**Owns:**
- `createReplayBar(container, handlers)` — the replay bar DOM (replaces inline DOM in `main.ts`)
- `ControlSocket` class — WS connection to `/ws/stream` for replay control messages
- `enterBarReplay()` / `exitBarReplay()` / `startTickReplay()` / `stopTickReplay()`
- `rbPlaying`, `barReplay`, `tickReplayActive`, `tickSpeed`, `tradeVisible` state
- Replay control message handler (`controlSocket.on(...)`) — `replay_done` / `replay_stopped` / `bar`
- Log handler for WS acks (`replay_start_ack`, etc.) → `logLine`
- `logLine(s)` function (moves from `main.ts`)

**Does NOT own:**
- `ReplayController` usage — that's chart-owned, `replay.ts` calls into it via the handlers passed to `createReplayBar`
- Trade feed WS → stays in `trade-feed.ts`

### `palette-shell.ts` — command palette + overlay panels

**Owns:**
- `createCommandPalette(actions)` — the palette itself
- `rebuildPalette()` — builds the actions list from `catalogueById` + `TRANSFORMS` + `PROFILES` + view/replay/layout/compare/trade actions
- Overlay creation:
  - `watchlistOverlay` + `watchlistEl` + `createWatchlist(...)` wiring
  - `ordersOverlay` + `ordersEl` + `createBottomDock(ordersEl)` + `tradeFeed.subscribeOrders/Positions(...)` + `dock.setContent(...)`
  - `strategiesOverlay` + `strategiesEl` + `fetchStrategyCatalogue()` + `createStrategiesPanel(...)` wiring
  - `drawRailOverlay` + `railEl` + `createDrawRail(...)` wiring
- `legendEl` — or this stays in `chart.ts` since it's updated by the crosshair handler. Decision needed.

**Does NOT own:**
- Command palette action execution — the `execute` callbacks call into `chart.ts` / `trade-shell.ts` / `replay.ts` exports (e.g., `placeOrder`, `applyTransform`, `enterBarReplay`, `watchlistOverlay.toggle()`, etc.)

---

## 3. Decisions Log

### D1: Shellbar ownership

The shellbar (`#shellbar`) contains brand, symbol button, timeframe pills, qty input, buy/sell buttons, status dot, status text. Two reasonable ownership models:

- **Model A:** Shellbar is "chart chrome" → timeframe pills + symbol button live in `chart.ts` (they drive `loadHistory()`), buy/sell/qty/status live in `trade-shell.ts`.
- **Model B:** Shellbar is "top-level" → all shellbar elements stay in `main.ts`, and the modules receive callbacks/element refs from `main.ts`.

**Decision: Model A.** The shellbar is visually part of the chart stage. Timeframe pills and the symbol button are chart-navigation concerns (they drive history loads). Buy/sell/qty/status are trade-entry concerns. The only element that's genuinely top-level is the brand label, which is static. This gives each module a clear DOM footprint.

Concretely:
- `chart.ts` creates: `tfPills` entries, `symBtn` click handler
- `trade-shell.ts` creates: `qtyInput` ref, `buy-btn`/`sell-btn` handlers, `statusDot`/`statusText` update logic
- `main.ts` owns: the `state` object, `persistState()`, and the boot sequence that calls `mountChart()` (which internally wires the shellbar subdivisions)

### D2: `rebuildPalette` location

The command palette actions draw from `catalogueById` (indicators), `TRANSFORMS`, `PROFILES`, and a fixed set of view/replay/layout/compare/trade actions. Two options:

- **Option A:** `rebuildPalette` in `palette-shell.ts`, reads from module-level exports of `chart.ts` / `trade-shell.ts` / `replay.ts` (e.g., `chart.getIndicatorCount?` — no, that's adding surface for the palette's sake).
- **Option B:** `rebuildPalette` in `main.ts` (top-level), imports `catalogueById` from `backend-indicators.ts`, imports `TRANSFORMS`/`PROFILES`, and imports the action functions from the other modules.

**Decision: Option B.** The palette is a top-level navigation construct that references every other module's capabilities. It doesn't belong to any one module. It stays in `main.ts` (or a small `palette-shell.ts` that `main.ts` calls into). The action callbacks are imports from the other modules — `import { placeOrder } from "./trade-shell"`, `import { applyTransform } from "./chart"`, etc. This keeps the palette's action list in one place without scattering it.

Actually — the palette DOM build (`createCommandPalette`) belongs in `palette-shell.ts`. The action list (`rebuildPalette`) belongs in `main.ts` because it's the top-level map of capabilities. So: `palette-shell.ts` exports `createCommandPalette`, `main.ts` imports it and owns `rebuildPalette`.

### D3: `legendEl` ownership

The legend (`#legend`) is updated by the crosshair move handler, which is chart-owned. The legend DOM element is created once in `main.ts` currently.

**Decision: `legendEl` stays in `main.ts`** (it's a top-level DOM element like `#chart`), but the crosshair handler that updates it moves to `chart.ts`. `chart.ts`'s `mountChart()` receives `legendEl` as a parameter, or `main.ts` passes it after chart creation. Cleanest: `chart.ts` exports `mountChart(chartHost, legendEl)` and `main.ts` calls it with the existing `legendEl` ref.

### D4: `Chrome` instance ownership

The `Chrome` instance (watermark, price levels, legend, event markers, buy/sell buttons) is created in `mountChart()` currently. `chrome.setOrderAction()` is called from `main.ts` (routes to `placeOrder`). `chrome.enable()` is called after chart creation.

**Decision: `Chrome` stays in `chart.ts`.** `chart.ts` creates the `Chrome` instance, calls `chrome.enable(lastChromeCtx, priceSeries)`, and exports `chrome` (or a `setOrderAction` wrapper) so `trade-shell.ts` can wire the order action. Specifically:

```ts
// chart.ts
export function mountChart(chartHost: HTMLDivElement, legendEl: HTMLElement): { chrome: Chrome; /* ... */ } {
  // ...
  const chrome = new Chrome(chart);
  // chrome.enable(...) after history load
  return { chrome, /* chart, priceSeries, etc. */ };
}

// trade-shell.ts
// receives chrome from main.ts, calls chrome.setOrderAction(placeOrder)
```

### D5: Dual WS sockets — keep separate

`TradeWsHub` (in `trade-feed.ts`) owns one `/ws/stream` connection for order/fill/control messages. `ControlSocket` (currently inline in `main.ts`, moving to `replay.ts`) owns another `/ws/stream` connection for replay control messages.

**Decision: keep two sockets.** The lifecycles are independent — trade subscription starts/stops with the trade host, replay starts/stops with user action. Merging them would couple two independent lifecycles for a modest connection saving. This is a future optimization, not a blocker. If the page's WS connection count becomes a concern (it isn't today), the merge is a contained change within `replay.ts` + `trade-feed.ts`.

### D6: `createReplayBar` adoption

`createReplayBar` in `shell/replay-bar.ts` builds the replay bar DOM and exposes a `ReplayBarApi` with `setState`/`setTickActive`. `main.ts` currently builds the same DOM inline with additional elements (`rbPlay`, `rbSpeed`, `rbRange`, `rbCount`, `rbClock`, `rbTick`, `rbTickSpeed`, `rbExit`) and custom behavior.

**Decision: adopt `createReplayBar` with extensions.** The `createReplayBar` module already builds play/stop/speed/tick-toggle/tick-speed/slider/clock. `main.ts` adds a few extra behaviors on top (the WS control message handler, the `rbPlaying` state sync, the tick speed change handler that sends `replay_speed` over the WS). The cleanest path: `replay.ts` imports `createReplayBar`, uses it as the DOM base, and adds the WS control socket + message handlers + state sync on top. The `createReplayBar` handlers (`onPlay`, `onPause`, `onStop`, `onSpeed`, `onTickReplayToggle`, `onTickSpeed`, `onSeek`) map directly to `replay.ts`'s lifecycle functions.

The `createReplayBar` module's `setTickActive` has a dead `void _tickActive` line — that's cleaned up as part of adoption (or the line is harmless and left; decision: remove it, it's noise).

### D7: `trade.test.ts` duplicate mapping

`trade.test.ts` reimplements `mapBookRowToOrder` locally (without the `STATUSES` check) and tests it. `regressions.test.ts` tests the real `mapBookRowToOrder` from `trade.ts` (with the status check). The `trade.test.ts` copy is redundant and could drift.

**Decision: remove the duplicate.** `trade.test.ts` currently tests: `mapBookRowToOrder` (4 tests), which should be removed since `regressions.test.ts` covers the real function with a status-validation test. The remaining `trade.test.ts` tests (if any beyond the mapping) stay. Check what else is in `trade.test.ts` before removing — if the file is _only_ the duplicate mapping, remove the file. If it has other tests, remove only the mapping tests.

---

## 4. Surgical Cleanups (bundled with the split)

### S1: Remove duplicate `createChart` at module scope

**Current (main.ts lines ~50-55):**
```ts
const chartHost = $<HTMLDivElement>("chart");
let chart = createChart(chartHost, { ... });  // FIRST creation — leaks
linkChart(chart as never);
// ...
setTimeout(() => mountChart(), 100);  // mountChart creates SECOND chart
```

**After:**
```ts
const chartHost = $<HTMLDivElement>("chart");
// chart created once inside mountChart() — called synchronously or via setTimeout(100)
```

The `linkChart` call moves into `mountChart()` (where the chart is actually created). The `ResizeObserver` that was attached to the first chart's host moves into `chart.ts`'s `mountChart()`.

### S2: Remove dead `createReplayBar` unused code path

After adopting `createReplayBar` in `replay.ts`, verify it's actually used (it will be). No removal needed — it goes from dead to live.

### S3: Remove duplicate `mapBookRowToOrder` in `trade.test.ts`

See D7. Remove the 4 mapping tests from `trade.test.ts` (or the whole file if that's all it contains).

---

## 5. Data Flow (after split)

```
main.ts boot:
  1. loadState() → state { exchange, symbol, interval }
  2. chartFeed = createTradexFeed()
  3. tradeFeed = new TradexTradeFeed()
  4. chart + chrome = mountChart(chartHost, legendEl, chartFeed, tradeFeed)
     → chart.ts: createChart, linkChart, ResizeObserver, series, ensureSeries
     → back to main.ts: chart + chrome returned
  5. tradeHost = createTradingHost(chart, tradeFeed, tradeFeed, getLtp)
     → trade-shell.ts: wire shellbar (tfPills, symBtn, qtyInput, buy/sell, status)
     → trade-shell.ts: chrome.setOrderAction(placeOrder)
  6. replay = setupReplay(chart, priceSeries, volumeSeries, tradeFeed, tradeFeed)
     → replay.ts: createReplayBar, ControlSocket, replay lifecycle
  7. palette = setupPalette()
     → palette-shell.ts: createCommandPalette
     → main.ts: rebuildPalette(actions from all modules)
  8. overlays = setupOverlays()
     → palette-shell.ts: watchlist, orders, strategies, draw-rail overlays
  9. registerBackendIndicators() → catalogueById
  10. rebuildPalette() → palette ready
  11. void loadHistory().then(() => bindLiveBars())
```

The key invariant: **one chart creation**, in `mountChart()`, called once from `main.ts`. No module-scope `createChart` before `mountChart()`.

---

## 6. Testing Strategy

### Unit tests — existing 149 tests must stay green

The split is pure extraction — no behavior changes. The existing vitest suite (149 tests across 10 files) must pass before and after. The tests are in:
- `src/http.test.ts` — `expectJson`
- `src/trade/validation.test.ts` — quantity/price/order validation
- `src/trade/pnl.test.ts` — P&L math
- `src/trade/order-state-machine.test.ts` — state transitions
- `src/trade/bracket.test.ts` — bracket group
- `src/trade/order-engine.test.ts` — order engine
- `src/trade/regressions.test.ts` — snapshot + status validation
- `src/trade/trade-controller.test.ts` — trade controller
- `src/trade/integration.test.ts` — order engine + trade feed integration
- `src/trade.test.ts` — mapBookRowToOrder (partially redundant — see D7)

After the split, the tests continue to import from their existing paths. No test file is changed except `trade.test.ts` (D7).

### E2E smoke test — 7 screenshots + console error check

The `e2e/trade-tier.e2e.ts` test (7 scenarios, screenshot capture, console error assertion) is the regression guard for the mount path. It must pass after the split. Run it against the live server (`http://localhost:8000/ui/`).

### What NOT to test

- Internal module boundaries — the split doesn't change any public API that's tested. The modules are implementation organization, not new abstractions with new contracts.
- The `createReplayBar` adoption — the replay bar DOM is tested indirectly by the E2E replay scenarios (if any). The `createReplayBar` module itself has no unit tests currently — adding them is out of scope for this cleanup.

---

## 7. File-by-File Changes

### Create new files

1. **`frontend/src/chart.ts`** — chart lifecycle module (~200 lines extracted from `main.ts`)
2. **`frontend/src/trade-shell.ts`** — trade shellbar module (~120 lines extracted from `main.ts`)
3. **`frontend/src/replay.ts`** — replay module (~150 lines: `ControlSocket` + `createReplayBar` adoption + replay lifecycle, extracted + created)
4. **`frontend/src/palette-shell.ts`** — palette + overlay module (~80 lines extracted from `main.ts`)

### Modify existing files

5. **`frontend/src/main.ts`** — reduce from ~700 to ~150 lines; keep boot + state + top-level wiring; import from new modules
6. **`frontend/src/trade.test.ts`** — remove duplicate `mapBookRowToOrder` tests (or remove file if that's all it contains)
7. **`frontend/src/trade-feed.ts`** — no changes (TradeWsHub stays where it is)
8. **`frontend/src/shell/replay-bar.ts`** — remove dead `void _tickActive` line (minor cleanup)

### Not touched

- `frontend/src/feed.ts`, `frontend/src/trade-feed.ts`, `frontend/src/trade/*.ts`, `frontend/src/shell/*.ts` (existing), `frontend/src/backend-indicators.ts`, `frontend/src/transforms.ts`, `frontend/src/profiles.ts`, `frontend/src/seasonality.ts`, `frontend/src/primitives.ts`, `frontend/src/linking.ts`, `frontend/src/theme.ts`, `frontend/src/shortcuts.ts`, `frontend/src/palette.ts`, `frontend/src/http.ts`, `frontend/index.html`, `frontend/package.json` — no changes.

---

## 8. Implementation Order

Vertical slices, one module at a time, tests green between each:

1. **Extract `chart.ts`** — move chart lifecycle out of `main.ts`. Update `main.ts` to import and call `mountChart`. Run vitest + confirm E2E still passes (one chart created, no leak).

2. **Extract `trade-shell.ts`** — move shellbar trade wiring out of `main.ts`. Update `main.ts` to import and wire. Run vitest + E2E.

3. **Extract `replay.ts`** — move `ControlSocket` + replay DOM + lifecycle out of `main.ts`, adopt `createReplayBar`. Run vitest + E2E.

4. **Extract `palette-shell.ts`** — move overlay creation + palette DOM out of `main.ts`. Keep `rebuildPalette` in `main.ts`. Run vitest + E2E.

5. **Surgical cleanup S1** — verify no module-scope `createChart` remains (should already be gone after step 1).

6. **Surgical cleanup S3** — remove duplicate `mapBookRowToOrder` from `trade.test.ts`.

7. **Final run** — vitest (149 tests) + E2E (7 screenshots) green. Remove any `as never` casts that are no longer needed after the split (audit, not mandatory).

---

## 9. Risks

- **Chart leak regression:** The duplicate `createChart` is the most dangerous issue. After step 1, verify via the E2E Test 1 (canvas not blank, no console errors) that exactly one chart is created and paints.
- **Import circularity:** `main.ts` imports from the new modules, and the new modules may need to import from each other (e.g., `chart.ts` exports `chrome`, `trade-shell.ts` uses it). Keep the dependency direction one-way: `main.ts` → `chart.ts` / `trade-shell.ts` / `replay.ts` / `palette-shell.ts`, and modules import from existing leaf modules (`feed.ts`, `trade/*.ts`, `shell/*.ts`) — not from each other. If `trade-shell.ts` needs `chrome` from `chart.ts`, have `main.ts` pass it in (composition root), not have `trade-shell.ts` import `chart.ts`.
- **`as never` casts:** Some casts may become unnecessary after the split if the module boundaries tighten the types. Audit after the split; remove only casts that TypeScript can now verify. Don't break working code chasing casts.
- **E2E timing:** The E2E test uses `waitForTimeout(15_000)` for chart load. If the split changes load order slightly, the timeout may need adjustment. The timeout is already generous (15s for 441 bars); minor ordering changes shouldn't break it.

---

## 10. Out of Scope

- Merging the two WS sockets (future optimization, not blocked by this split)
- Adding unit tests for `createReplayBar` or `ControlSocket`
- Moving CSS out of `index.html` into a stylesheet
- TypeScript strict-mode cleanup of `as never` casts (audit only, remove only what's safe)
- Any behavior changes to order placement, replay, indicators, or the trading host
