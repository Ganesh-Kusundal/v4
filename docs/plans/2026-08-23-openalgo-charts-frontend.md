# OpenAlgo-Charts Frontend — Backend-Owned Engine Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the lightweight-charts prototype (`../web/`) with an openalgo-charts UI where the v4 backend is the *only* brain: all indicators, strategies, replay/simulation, and order execution run in tradex_trading. The browser renders; it computes nothing.

**Non-goals:** porting the chart library's 91 built-in indicators to JS-side use, any DOM chrome beyond the host shell we write, touching domain/ or brokers/.

---

## Division of labor (the invariant every task preserves)

| Concern | Owner | Never in frontend |
|---|---|---|
| Indicator math | `trading/analytics` (registry, Task 3) | no formula ever |
| Strategy logic + discovery | `strategy/core` + auto-discovered extensions | no strategy code |
| Backtest / walk-forward | `replay/backtest.BacktestEngine`, `replay/walk_forward` | no P&L math |
| Tick simulation | `replay/synthetic_ticks.SyntheticTickGenerator` | no random walks |
| Forming-bar aggregation | NEW `runtime/bar_aggregator.py` | no bar building |
| Order pipeline (risk, fees, fills, OMS) | `execution.*` via `session.trade` | no client-side validation beyond UX |
| History storage | parquet datalake + `ParquetMarketProvider` | no persistent cache of bars |

openalgo-charts contributes only: canvas rendering, panes/scales, drawing tools, linked grids, ReplayController playhead, TradeController primitives, and the **Tier-2 external-data indicator contract** (`createTier2Indicator`) which is the seam through which backend-computed indicator series reach the chart.

## Architecture

```
Browser (frontend/, vite build -> served by FastAPI at /ui)
┌────────────────────────────────────┐        ┌─────────────────────────────────────────┐
│ openalgo-charts (npm, render only) │        │ FastAPI (interface/fastapi_app.py +      │
│  TradexDataFeed   ── GET /api/charts/history    NEW interface/chart_api.py)          │
│  Tier-2 fetch()   ── POST /api/charts/indicators  ──► analytics registry                │
│  OrderFeed        ── POST/PUT/DELETE /orders (existing) ─► ExecutionEngine              │
│  TradeController.reconcile ◄─ ws control msgs (order/fill — existing)                  │
│  ReplayController ◄─ ws bar/sim frames (NEW msg types on /ws/stream)                   │
└────────────────────────────────────┘        └─────────────────────────────────────────┘
```

Decisions locked with the user:

1. **Location:** `v4/frontend/` workspace; built assets mounted as StaticFiles by the existing FastAPI app (single origin, no CORS). Dev mode proxies `/api` + `/ws` to `127.0.0.1:8000` like today's `web/`.
2. **Library:** npm `openalgo-charts@^1.6.0`. The untracked `openalgo-charts-master/` download is reference-only during implementation and is removed at Task 8 sign-off.
3. **Indicators:** core set first (grow later) — see Task 3.
4. **Data modes:** live broker feed + datalake history + offline simulation, all three at launch.

### Time convention (get this right once)

Datalake timestamps are IST tz-naive (repo rule). The history endpoint converts at the edge: `ts.replace(tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()` -> chart `time` in UTC seconds. The chart's default zone is already `Asia/Kolkata`, so labels render back as IST wall clock. Bars are filtered to 9:15-15:30 IST defensively (phantom-bar rule). The gapless axis collapses non-session time.

---

## Dependency graph

```
Task 1 (scaffold frontend + StaticFiles mount)
Task 2 (history API + TradexDataFeed)          ── depends on 1
Task 3 (indicator registry + compute API)      ── depends on 2
Task 4 (bar aggregator + ws subscribe_bars)    ── depends on 2
Task 5 (ws replay/sim control)                 ── depends on 4
Task 6 (trade tier wiring)                     ── depends on 2 (independent of 3,4,5)
Task 7 (strategy/backtest/scanner panel)       ── depends on 3
Task 8 (polish, e2e verify, drop vendor copy)  ── depends on all

Parallel groups:
- Group A: Tasks 2 (after 1)
- Group B (after 2, parallel): Tasks 3, 4, 6
- Group C (after B): Task 5 (needs 4), Task 7 (needs 3)
- Task 8 last.
```

Per openalgo-charts CLAUDE.md concurrency rule: `src/core/chart.ts` / `pane.ts` need a single writer — but we do not modify the library (npm dep), so agent fan-out is safe; still keep exclusive ownership of `frontend/src/main.ts` and each task's backend files.

Writing rules inherited from the library's CLAUDE.md and adopted repo-wide for this work: no emoji, no em/en dashes, Conventional Commits, comments explain why.

---

## Task 1: Frontend scaffold + static serving

**Files:**
- Create: `frontend/package.json`, `frontend/vite.config.ts`, `frontend/index.html`, `frontend/tsconfig.json`, `frontend/src/main.ts`
- Modify: `trading/src/tradex_trading/interface/fastapi_app.py` (mount `/ui` StaticFiles when the dist dir exists)
- Test: `trading/tests/interface/test_ui_mount.py`

- [ ] Vite vanilla-ts project; `npm install openalgo-charts`; page renders one candlestick chart from generated bars (proves the import works before any backend exists)
- [ ] `create_app()` mounts `frontend/dist` at `/ui` if the directory exists (absent dir = API-only behavior byte-identical to today)
- [ ] Vite dev proxy: `/api` and `/ws` -> `http://127.0.0.1:8000`
- [ ] Test: TestClient GET `/ui/` returns HTML when dist exists, 404/no-mount when absent

## Task 2: History API + TradexDataFeed

**Files:**
- Create: `trading/src/tradex_trading/interface/chart_api.py` (router factory, included by `create_app`)
- Modify: `fastapi_app.py` (include router)
- Test: `trading/tests/interface/test_chart_history.py`

Endpoints:
- `GET /api/charts/symbols?q=` — universe + master search (reuses broker.search / load_universe)
- `GET /api/charts/history?exchange=&symbol=&interval=1m|5m|15m|30m|1h|D&from=&to=` — closed bars only

Behavior:
- Source precedence: `ParquetMarketProvider.history()` first (offline, rate-limit-free), broker `session.broker.history()` fallback when the datalake has nothing for the window
- Resample M1 store data via `HistoricalSeries.resample()` — never hand-roll resampling
- Response carries `last_closed_time` so the frontend knows where live subscription takes over (never-cache-forming-bar rule, server side of it)
- Brief TTL cache keyed on (symbol, interval, from, to) for datalake hits

Frontend (`frontend/src/feed.ts`):
- `TradexDataFeed implements DataFeed`: getBars maps interval codes -> query params; registers our codes with `registerInterval` ('M' deliberately NOT registered — monthly needs a calendar bucketing decision later)
- `withBarCache(feed, { ttlMs })` wraps it, honoring the library rule that the forming bar is never cached

- [ ] Golden parity test: API response for M5 window equals `HistoricalSeries.resample(M5)` computed directly from the store
- [ ] Empty-symbol -> empty bars array, not error
- [ ] Browser check: RELIANCE 1m from datalake renders identical silhouette to a quick pandas plot (green units do not prove renderers)

## Task 3: Indicator service (core set) + compute API

**Files:**
- Modify: `trading/src/tradex_trading/analytics/indicators.py` — introduce a registry: `_REGISTRY: dict[str, IndicatorSpec]` where spec = `{id, name, category, placement: overlay|pane, params: {name,default}, plots: [{key,type,title}], fn(candles, params) -> {key: list[float|None]}}`
- Add implementations: bollinger, atr, vwap, stochastic, supertrend, obv alongside existing sma/ema/rsi/roc/macd (which get wrapped into the same registry shape)
- Create: compute + catalogue endpoints in `chart_api.py`
- Test: `trading/tests/analytics/test_indicator_registry.py` (golden values), `trading/tests/interface/test_chart_indicators.py`

Endpoints:
- `GET /api/charts/indicators` — the catalogue: ids, names, param schemas, plot shapes (frontend builds its entire indicator menu from this; no hardcoded list in JS)
- `POST /api/charts/indicators/compute` — body `{exchange, symbol, interval, from, to, id, params}`; internally fetches the SAME bar window as /history so points are index-aligned with the bars the chart holds; returns `{points: [{time, <plotkey>: value|null}...], meta}`

Frontend (`frontend/src/backend-indicators.ts`):
- Fetch catalogue; for each entry `registerIndicator(createTier2Indicator({...}))` with `fetch:` calling compute; plot keys/levels/ranges copied verbatim from the catalogue response

- [ ] Every registered indicator has a golden-value unit test (hand-computed small fixture)
- [ ] None-padded warmup region survives JSON round trip (null, not NaN — json.dumps would emit invalid bare NaN)
- [ ] Adding a made-up id to the registry makes it appear in the UI menu with zero JS changes (proves the seam)
- [ ] VWAP anchors to session start (9:15 IST) using the chart-zone-aware anchor vocabulary, documented in the spec

## Task 4: Forming-bar aggregation + ws subscribe_bars

**Files:**
- Create: `trading/src/tradex_trading/runtime/bar_aggregator.py`
- Modify: `fastapi_app.py` ws_stream handler: new message types `subscribe_bars` / `unsubscribe_bars`
- Test: `trading/tests/runtime/test_bar_aggregator.py`, `trading/tests/interface/test_ws_subscribe_bars.py`

Aggregator contract:
- Buckets quotes onto the fixed-interval grid (IST session aware); emits an updated Candle per quote batch throttled to ~1 Hz per (instrument, interval), and a final immutable Candle on bucket close (15:30 IST flat-closes the day's tail)
- Same class serves BOTH live MarketFeed quotes and synthetic generator quotes (parity: one aggregation code path)
- Frames on ws: `{type:"bar", instrument, interval, time, o,h,l,c,v, closed:false|true}`
- Per-connection wanted-set integration mirrors existing quote/depth filtering; bar frames ride the ticks queue (freshness-bound, drop-oldest)

Frontend:
- `subscribeBars` implementation feeding `series.update(bar)`; on reconnect, refetch history from lastClosedTime (gap heal)

- [ ] Bucket boundary test: quote at 10:04:59.9 closes the 10:00 bar; 10:05:00.1 opens the next
- [ ] Throttle test: 600 quotes in one bucket produce ~10 frames, and exactly one `closed:true`
- [ ] Sim quotes through the same aggregator produce identical bucket boundaries as live quotes (one test, two sources)

## Task 5: Replay / simulation over ws

**Files:**
- Modify: `chart_api.py` or ws_stream: message types `replay_start`, `replay_pause`, `replay_resume`, `replay_speed`, `replay_stop`
- Test: `trading/tests/interface/test_ws_replay.py`

Design:
- `replay_start` spawns a per-connection asyncio task running `SyntheticTickGenerator(bus=per-connection mini-bus, clock=FakeClock(start), method='anchored'|'bridge', ticks_per_bar)` over the requested datalake window, scaled by speed; its quotes flow through the SAME bar_aggregator as Task 4; frames are tagged `"source":"sim"`
- One generator per connection (clients replay different windows independently); bounded queue already protects the socket
- `replay_stop` cancels the task and acks; frontend then restores full history via its local `ReplayController.stop()`

Division of replay labor:
- Bar-prefix scrubbing within fetched history = frontend `ReplayController` (pure view concern)
- Tick-level simulation, strategy-through-engine replay, and any fill generation = backend (SyntheticTickGenerator / BacktestEngine) — the frontend never fabricates market data

- [ ] Speed 0 (paused) emits no frames; resume continues from the same bar
- [ ] Stop mid-stream cancels cleanly (no orphan task, writer drains)
- [ ] Determinism: same seed -> identical frame sequence (regression-testable)

## Task 6: Trading wiring (independent track)

**Files:**
- Create: `frontend/src/trade-feed.ts` (maps chart `OrderFeed`/`TradeController` to our REST + ws)
- Test: `trading/tests/interface/test_order_status_mapping.py`

Mapping:
- placeOrder -> `POST /orders`, modifyOrder -> `PUT /orders/{id}`, cancelOrder -> `DELETE /orders/{id}` (all existing, auth-gated)
- Book reconciliation: poll `GET /orders` + `GET /positions` on connect, then fold ws control `order`/`fill` frames into `TradeController.reconcile(orders, positions)` snapshots
- Status mapping table (domain enum -> chart union): NEW/PENDING->pending, ACK/OPEN->working, PARTIALLY_FILLED->partial, FILLED->filled, CANCELLED->cancelled, REJECTED->rejected — one function, unit-tested exhaustively
- Analyzer mode is a backend property (paper vs live session boot); frontend shows it read-only from `/health/ready` + `/extensions` capabilities — no sandbox reimplementation
- Depth for the DOM ladder rides the existing depth subscription

- [ ] Mapping table test covers every domain OrderStatus value (fail loud on future enum members)
- [ ] Reconnect replays a fresh snapshot before applying deltas (no ghost lines)

## Task 7: Strategy, backtest, scanner panel

**Files:**
- Modify: `chart_api.py` — `GET /api/charts/strategies` (auto-discovered via `all_strategies()`), `POST /api/charts/backtest` (BacktestEngine over the datalake window + chosen strategy/params), `GET /api/charts/scanners` + `POST /api/charts/scanner/run` (ScannerService)
- Create: `frontend/src/panels/strategies.ts`
- Test: `trading/tests/interface/test_chart_backtest.py`

Response shapes:
- Backtest: metrics (return, sharpe, max drawdown, num_trades, num_rejected) + `equity_curve` + `trades` as `{time, side, price, reason}` entries rendered as chart buy/sell markers + an equity line added as a second Tier-2-style series; risk-rejected signals surfaced distinctly (they are in BacktestResult for exactly this)
- Walk-forward exposed as a follow-up knob on the same endpoint (`mode: "walk_forward"`), reusing `run_walk_forward_by_date`

- [ ] Backtest endpoint on a known window reproduces `scripts/backtest_datalake.py` numbers for the same inputs
- [ ] A rejected-signal-heavy run shows the rejection count in the UI without inventing fills

## Task 8: Polish + verification + cleanup

- [ ] Single-origin serve verified end to end: `tradex serve --broker paper` -> open `http://127.0.0.1:8000/ui/` -> chart, indicators, replay, orders all functional in a real browser (library rule: green units do not prove pixels)
- [ ] Dark-theme chrome per the library's UI standard: styled scrollbars, small square swatches, paired bullish/bearish color rows, no native form controls on dark panels
- [ ] Full gate: `npm run typecheck && npm test && npm run build` in frontend; `pytest trading/tests` green; existing import-boundary tests untouched and passing
- [ ] Update root `CLAUDE.md` (frontend section: how to run dev/prod) and `docs/ARCHITECTURE.md` flow F11 (browser)
- [ ] Delete `openalgo-charts-master/` after final visual sign-off (it was never tracked)

---

## Risks and stated limitations

- **Datalake depth:** ~63 trading days of M1. Intraday views older than that fall back to broker history (subject to Dhan 90-day intraday cap) or degrade to daily. Stated in the UI, not hidden.
- **Monthly interval deferred:** no calendar-month bucketing on either side at launch; 'M' intentionally unresolved.
- **Footprint/order flow:** backend Footprint accumulates from quote classification only; footprint charts stay out of scope until a tick recorder exists (same limitation the library itself states).
- **Multi-worker serve:** frontend works behind the existing single-writer constraint; `--workers` stays paper-only per `start_fastapi_server`.
- **WS frame volume:** bar frames are throttled server-side; the existing drop-oldest queues bound memory. If 1 Hz proves heavy for many subscriptions, coalesce per connection before enqueue (measure first).
