# Frontend Host (v4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thin v4 chart frontend on `openalgo-charts` 2.1.0: datalake bars render first, then live broker ticks, then replay/orders — incrementally, each milestone independently verified.

**Architecture:** `createWidget` (widget tier = topbar/rail/dialogs/toasts/persistence, ~70% of chrome free) + ONE `V4DataFeed` adapter (~150 lines: REST history + WS stream) + ONE WS client helper. Backend untouched except one bug (`bar.source` tag). Numbers/string/interval mismatches are parsed in the adapter — the adapter is ours, engine adapters' fail-closed rule doesn't apply to it.

**Tech Stack:** Vite + vanilla TS, `openalgo-charts` as `file:../openalgo-charts` dep, FastAPI static mount at `/ui` (mount code already exists, just needs `frontend/dist`).

## Global Constraints

- Bar `time` on the wire is UTC seconds — engine convention; backend already emits it everywhere.
- Datalake is the data source for history (M1); live broker feed only layers on top (M2). Never require broker for chart render.
- Each milestone ends with the server restarted + a curl-verifiable + browser-verifiable state.
- Every file works when `--broker paper` (no credentials) AND when `--broker dhan` (live).

---

### M1: Datalake flow — candles render from `/api/charts/history`

**Files:**
- Create: `frontend/package.json`, `frontend/vite.config.ts`, `frontend/index.html`, `frontend/src/main.ts`, `frontend/src/feed.ts`
- Modify: `openalgo-charts` — none (file dep)

**Interfaces:**
- Produces: `V4DataFeed implements DataFeed` (`getBars` via `GET /api/charts/history/{exchange}:{symbol}?interval=&from=&to=`), `streamSocket()` WS helper (`/ws/stream?api_key=`).
- Produces: `dist/` at `/ui` (vite base `./`).

- [ ] `frontend/package.json`: deps `openalgo-charts: file:../openalgo-charts`, devDeps `vite`, `typescript`; scripts `dev`/`build`. `npm install`.
- [ ] `vite.config.ts`: `base: './'` (served under `/ui/`), build outDir `dist`.
- [ ] `src/feed.ts`: `V4DataFeed.getBars(req)` — fetch history, map `bars` → `Bar[]` (`time` already UTC sec), throw `NotFoundError` on `bars: []`. Parse engine error envelope `{"error":{code,message}}` → typed errors. Wrap with `withBarCache(liveFeed, {timezone: 'Asia/Kolkata'})`.
- [ ] `src/main.ts`: `createWidget(container, { feed, symbol: 'RELIANCE', exchange: 'NSE', interval: '1m', theme: 'dark', persist: true, symbolSearch })` — symbolSearch calls `GET /api/charts/symbols?q=`. Price series candlestick + volume histogram (widget handles panes; add explicitly per engine bootstrap if widget doesn't).
- [ ] Verify: `npm run build` → restart server (`serve --broker paper --port 8000`) → `curl -s :8000/ui/` 200, `curl -s :8000/api/charts/history/NSE:RELIANCE?interval=1m&limit=5` has bars → browser `/ui/` shows RELIANCE candles **from datalake**.
- [ ] Commit: `feat(frontend): M1 widget scaffold, datalake history flow`

### M2: Live flow — broker ticks → forming bars

**Files:**
- Modify: `frontend/src/feed.ts` (add `subscribeBars` over WS `subscribe_bars` + `bar` frames)
- Modify: `trading/src/tradex_trading/runtime/bar_aggregator.py` + `replay` publisher (one line: tag `source`)
- Test: `trading/tests/runtime/test_bar_frame_source.py`

- [ ] Backend one-liner: `BarFrame` gets `source: str = "live"`; synthetic-tick replay path constructs frames with `source="sim"`. One small test.
- [ ] `feed.ts`: `subscribeBars(req, onBar)` — on ack, send `subscribe_bars`; forward `bar` frames as `Bar` + `series.update(bar)` forming, `setData`-append closed (engine `update()` handles forming; closed bars go through the same path — verify engine docs: `update(bar)` for the tail is correct for both).
- [ ] Widget wiring: feed is feature-detected; `withBarCache` forwards subs.
- [ ] Verify: paper broker `serve` → open `/ui/` → forming bar ticks in (volume/candle moves), `curl /health` 200. With `--broker dhan` (if creds) real ticks; without creds the paper path still proves the pipe.
- [ ] Commit: `feat(frontend): M2 live bars over /ws/stream; tag replay source`

### M3: Replay + depth + orders

**Files:**
- Modify: `frontend/src/feed.ts` (subscribeDepth over WS), create `frontend/src/replay.ts` (transport mapping), `frontend/src/orders.ts` (TradingController)

- [ ] Depth: `subscribeDepth` → WS `subscribe {depth:"30"}` + `depth` frames → `MarketDepth` (already numeric/engine-shaped). Feed `DomLadder.setDepth` (widget/rail: ladder mounted as primitive beside chart).
- [ ] Replay: buttons (start from selected bar / pause / resume / speed 0.5–10 / stop) → WS `replay_start` (requires prior `subscribe_bars` — already true) / `replay_pause|resume|speed|stop`; `replay_done` → toast. Forming/closed bars arrive as normal `bar` frames (source `sim` after M2 fix).
- [ ] Orders: `TradingController` (base-tier push-render) + poll `GET /api/charts/book` (3–8s) + WS `order`/`fill` frames → `setOrders/setPositions`; context-menu order rows → `POST /orders` with `Idempotency-Key` (crypto.randomUUID). Order lines/position markers render via `chart.tradeHost`.
- [ ] Verify: paper → place order from UI context menu → order line appears, fill → position marker; replay run bars stream; ladder shows paper depth.
- [ ] Commit: `feat(frontend): M3 replay transport, DOM ladder, order lines`

### M4: Tier-2 + persistence + polish

**Files:**
- Create: `frontend/src/tier2.ts`
- Modify: `frontend/src/main.ts`

- [ ] Tier-2 descriptors: `backtest-equity` (`POST /api/charts/backtest` → points `{time, values:{value}}`), `seasonality-heatmap` (`POST /api/charts/seasonality`), `market-profile` (`POST /api/charts/profiles/market-profile`). `createTier2Indicator` + `registerIndicator`; `refetchOn: ['symbol','exchange','interval']`.
- [ ] Workspace save/load: widget `persist` (localStorage) stays for UI state; chart state (`chart.getState()`) saved on interval to `PUT /api/charts/workspace/{symbol}_{interval}_default`, restored on load (revision + 409 → reload blob).
- [ ] Verify: add Tier-2 indicator → renders from datalake-computed data; reload page → chart state restored.
- [ ] Commit: `feat(frontend): M4 Tier-2 backend indicators + workspace persistence`

## Skipped (ponytail), add when needed

- Backend numeric/string unification (A1/A2): adapter parses strings — one `Number()` per field, zero backend churn.
- `D` vs `1d`: adapter maps WS `1d` ↔ REST `D`.
- `replay_start from` param: trailing window is fine for replay UX; add when backtest-style anchored replay is asked for.
- Client-side `ReplayController` scrub: server replay drives everything; add only if scrub UX demands it.
- History cache-hit `last_closed_time`: stale badge derives from the last bar client-side.
- Error-envelope code alignment: adapter maps `{"error":{code,message}}` once.
