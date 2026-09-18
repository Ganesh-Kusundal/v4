# Frontend Integration Map — what we have vs what we have to build

**Date:** 2026-09-13
**Goal:** the frontend must be the **upstream `openalgo-charts` UI and its flows**,
with the TradeX backend (102 indicators, strategies, screeners, backtests, orders)
integrated on top.
**Method:** every count and every "unwired" claim below is from a command run
against this checkout, not from reading docs.

---

## 1. Three layers

```
┌──────────────────────────────────────────────────────────────────────┐
│ L1  LIBRARY   openalgo-charts  2.1.8   (upgraded this session)       │
│     widget tier 15 modules + 7 dialogs · draw 14 · trade 12 · replay 1│
└──────────────────────────────────────────────────────────────────────┘
                              ▲ imported by
┌──────────────────────────────────────────────────────────────────────┐
│ L2  HOST      frontend/   9 modules, ~1145 lines                     │
│     main · feed · tier2 · orders · workspace · replay · ladder ·      │
│     trade-bar · apikey                                                │
└──────────────────────────────────────────────────────────────────────┘
                              ▲ HTTP + WS
┌──────────────────────────────────────────────────────────────────────┐
│ L3  BACKEND   tradex_trading  30 REST endpoints · 16 WS frames        │
│     102 indicators · 8 strategies · 3 scanners                        │
│     + the shared engine spine (already parity-tested)                 │
└──────────────────────────────────────────────────────────────────────┘
```

### L1 — Library: `openalgo-charts` **2.1.8**

| | |
|---|---|
| Repo | nested clone, `origin` = `marketcalls/openalgo-charts`, branch `master` |
| HEAD | `a0d4d66` *feat: release 2.1.8 with smooth navigation and mobile controls* (2026-09-13) |
| Parent tracking | **gitignored** (`.gitignore:64`) → consumed as an unmodified artifact |
| Working tree | clean |
| `dist/` | **rebuilt this session** (was stale, stamped `2.1.0`) |
| Local patches | 2 commits preserved on branch `backup/v2.1.0-local`; 3 parity scripts restored |
| Version before | 2.1.0 (13 commits behind) |

Widget tier (`src/widget/`, 15 modules): `widget` `mobile` `objects-panel`
`data-status` `topbar` `statusline` `rail` `toast` `context` `form` `keymap`
`styles` `tokens` `index` `dialogs/`
Dialogs (7): `indicator-picker` `indicator-settings` `settings`
`drawing-properties` `level-editor` `text-editor` `context-menu`

New in 2.1.1 → 2.1.8 (all **auto-mounted by the widget**):
- **mobile tier** (`mobile: 'auto'` default — ≤640 CSS px or coarse pointer)
- **objects panel + `ChartObjects` inventory**
- **data-status** (`DataLoadingController`)
- **smooth navigation** (proportional wheel, `animAutoscale`, axis scaling)
- **`Chart.setDataContext()`** — published on every symbol/interval/exchange change
- shared history request pool · footprint themes

### L2 — Host: `frontend/`

| module | lines | role |
|---|---|---|
| `main.ts` | 71 | composition root: `createWidget` + volume series + mounts |
| `feed.ts` | 375 | `DataFeed`: REST history + WS bars/depth, `withBarCache`, reconnect/backoff |
| `tier2.ts` | 193 | 3 backend Tier-2 descriptors (`backtest-equity`, `market-profile`, `seasonality-heatmap`) |
| `orders.ts` | 175 | trade book: `GET /api/charts/book` + `POST /orders` + WS `order`/`fill` |
| `workspace.ts` | 116 | layout persistence |
| `replay.ts` | 107 | replay transport |
| `trade-bar.ts` | 39 | order qty control |
| `ladder.ts` | 32 | DOM ladder |
| `apikey.ts` | 37 | API key plumbing |

### L3 — Backend: `tradex_trading`

| Surface | Count |
|---|---|
| Indicator catalogue (`GET /api/charts/indicators`) | **102** — Trend 36, Momentum 29, Volatility 22, Volume 15 |
| Strategies | **8 discovered**, but only **2 backtestable** (`sma_cross`, `mean_reversion`) — see the correction in §3 |
| Scanners | **3** — `momentum` `nifty500_technical` `pullback` |
| REST endpoints | **30** across 10 routers |
| WS frames | **16** (`subscribe_bars`/`unsubscribe_bars`, `subscribe`/`unsubscribe`, `replay_start/pause/resume/speed/stop` + `replay_*` acks, `order`, `fill`, `indicator-sub`/`indicator-unsub`/`indicator-subscribed`) |

---

## 2. What is ALREADY wired end-to-end

| Flow | Path |
|---|---|
| Candles (history) | `GET /api/charts/history/{ex}:{sym}` → `feed.ts` → `withBarCache` → widget |
| Live bars | WS `subscribe_bars` → `barSocket` → widget |
| Market depth | WS `subscribe` → `barSocket` → `ladder.ts` |
| Symbol search | `GET /api/charts/symbols?q=` → `symbolSearch` option |
| Order entry | `onOrder` context menu → `POST /orders` → `trade-bar.ts` qty |
| Order/position book | `GET /api/charts/book` + WS `order`/`fill` refresh → `orders.ts` |
| Replay | WS `replay_*` → `replay.ts` transport bar |
| Layout persistence | `GET/PUT/DELETE /api/charts/workspace/{id}` → `workspace.ts` |
| Backend study: equity | `POST /api/charts/backtest` → `tier2.ts` → pane (⚠ hardcoded `sma_cross`) |
| Backend study: profile | `POST /api/charts/profiles/market-profile` → `tier2.ts` → POC/VAH/VAL |
| Backend study: seasonality | `POST /api/charts/seasonality` → `tier2.ts` → `ChartTable` on price pane |

---

## 3. Gap A — backend capability the UI never calls

Verified by grepping `frontend/src/` for each route (0 references = unwired).

> **Correction (2026-09-13, later the same day).** Two rows below were wrong.
> `GET /indicators` is **not** a gap: diffing the backend catalogue against the
> frontend registry's ids gives **102 = 102, with zero frontend-only ids**, so the
> picker already offers everything the backend can compute and registering the
> catalogue again would duplicate all 102 — the architecture debt §27 forbids.
> "0 references from `frontend/src/`" was true but meaningless, because the
> registry lives in the library.
>
> `GET /strategies` likewise understates the real number: the route discovers 8
> strategy classes but `/backtest` can only build its `_STRATEGY_FACTORIES` set,
> so `backtestable` is **2** (`sma_cross`, `mean_reversion`). A picker must read
> that field, not the `strategies` list, or it offers runs that cannot happen.

| Backend capability | UI status | Consequence |
|---|---|---|
| `GET /api/charts/indicators` (**102 catalogue**) | **n/a — not a gap** | the frontend registry is the same 102 ids; see the correction above |
| `POST /api/charts/indicators/compute` | **UNWIRED** | no way to view a backend-computed series the frontend doesn't have |
| `POST /api/charts/transforms/{id}` | **UNWIRED** | transforms tier has no UI |
| `POST /api/charts/profiles/{id}` (all but market-profile) | **partly unwired** | only `market-profile` is exposed |
| `GET /api/charts/strategies` → `backtestable` (**2**) | **WIRED** | the Backtest Equity pane's settings dialog now offers a Strategy select, built from this response |
| `POST /api/charts/scanner/run` (**3 scanners**) | **UNWIRED** | **no screener UI exists at all** |
| `GET /orders`, `GET /orders/{id}` | **UNWIRED** | host uses `/api/charts/book` instead |
| `GET /positions`, `/holdings`, `/account` | **UNWIRED** | see above |
| `GET /search`, `/quotes/{ex}:{sym}` | **UNWIRED** | top bar uses `/api/charts/symbols` |
| `GET /option-chain`, `/future-chain` | **UNWIRED** | no chain UI |
| `GET /extensions` | **UNWIRED** | no strategy/scanner discovery UI |
| WS `indicator-sub` / `indicator-unsub` | **UNWIRED** | **live incremental indicator push exists server-side (bar-close + tick modes) but the UI only does periodic Tier-2 fetches** |

## 4. Gap B — upstream UI with no backend data

| Upstream UI | Mounted? | Fed by us? |
|---|---|---|
| Objects panel / `ChartObjects` inventory | ✅ auto | ❌ nothing registers trades, positions, SL/TP brackets as chart objects |
| Data status | ✅ auto | ❌ no backend data-quality signal published into it |
| Indicator picker dialog | ✅ auto | ⚠️ lists the **frontend** registry, not the 102 backend descriptors |
| Settings / drawing-properties / level-editor / text-editor dialogs | ✅ auto | n/a (chart-local) |
| Context menu | ✅ auto | ⚠️ only trade rows wired; no "Backtest here" / "Scan this setup" actions |
| Statusline | ✅ auto | ❌ no portfolio/P&L numbers |
| Replay controls | ✅ auto | ✅ `replay.ts` |
| Toolbar / rail / shortcuts | ✅ auto | n/a (chart-local) |

## 5. Gap C — the trading-results flow (the original goal)

The spec's TradingView-style analysis flow has **no UI**:

```
Strategy picker (GET /strategies)        ✗ none
Parameter editor                          ✗ none   (params are hardcoded)
Run backtest (POST /backtest, async)      ~ sync only, hidden behind a pane
Entry/exit markers on candles             ~ fills exist in JSON, not drawn
Stop-loss / target levels                 ✗ none
Position zones                            ✗ none
Trade list (entry/exit/qty/P&L/MAE/MFE)   ✗ none
Statistics block (pf, expectancy, …)      ✗ none   (backend now computes it)
Equity curve                              ~ one Tier-2 line
Drawdown curve / monthly P&L              ✗ none
Scanner results → candidates → chart     ✗ none
```

Backend side of this is **done**: `analytics/trade_metrics.py` (this session) supplies
`Trade` + the full statistics block; `POST /backtest` returns
`{metrics, equity_curve, trades}`; `POST /scanner/run` returns candidates.

---

## 6. What we HAVE and what we HAVE TO

### HAVE (done this session)
1. Library upgraded **2.1.0 → 2.1.8**, `dist/` rebuilt, local patches preserved.
2. Host compiles + builds against 2.1.8 (typecheck clean).
3. Adopted upstream's **`dataContext` flow**; deleted the ~45-line host workaround
   (`syncTier2Instrument` + `addIndicator` monkey-patch).
4. Backend indicator golden parity still passes against 2.1.8 (**175 tests**).
5. `Trade` + statistics engine (`trade_metrics.py`) + `sortino_ratio`; **2257 tests** pass.
6. Discovery + gap-analysis doc (`2026-09-13-backtest-engine-discovery.md`).

### HAVE TO (ordered, each independently shippable)
| # | Deliverable | Backend ready? |
|---|---|---|
| 1 | **Strategy picker + params dialog** from `GET /strategies` (kill the hardcoded `sma_cross`) | ✅ |
| 2 | **Backtest results panel**: markers (entry/exit), SL/TP levels, position zones, trade list, statistics block, equity + drawdown curves | ✅ `/backtest` + `trade_metrics` |
| 3 | **Register trades/positions as `ChartObjects`** so the new objects panel manages them | ✅ data exists |
| 4 | **Scanner/screener UI**: run `POST /scanner/run`, show candidates, click → load chart | ✅ |
| 5 | **Indicator picker backed by the 102-entry backend catalogue** (`GET /indicators`) | ✅ |
| 6 | **Live indicator push** via WS `indicator-sub` (incremental, bar-close/tick) instead of polling | ✅ |
| 7 | **Statusline/portfolio wiring** (`/positions`, `/account`) | ✅ |
| 8 | **Transforms + remaining profiles panels** | ✅ |
| 9 | **Option/future chain** view | ✅ |
| 10 | **Backend data-quality → data-status panel** (gap detector) | ✅ |

**Not a gap:** upstream's reference host (`examples/yfinance`, 30 modules: toolbar,
rail, rail-flyout, menus, drawing, text/text-editor, level-editor, properties,
chart-settings, indicators, transforms, compare, split, link, hover, snapshot,
clipboard, status, timezone, watermark, axis-chrome, volume) — the 2.1.x **widget
tier now provides these natively**, so they must not be re-implemented in the host.

---

## 7. Guardrails for the build

- Do **not** modify `openalgo-charts/` — add adapters in `frontend/` (CLAUDE.md and spec §22).
- Do **not** duplicate indicator logic in the frontend — the backend registry is canonical (spec §2, already resolved at 102-indicator parity).
- **Do** delete host workarounds when the library gains the capability (as done for `dataContext`).
- Every new flow must have a backend route that already exists; no new endpoints until the ten above are wired.
