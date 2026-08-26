# Full openalgo-charts UI-Surface Adoption — Design

> **Status:** Approved (2026-08-26). Approach 1: stateless windowed transform/profile endpoints, backend computation, golden parity.
> **Date:** 2026-08-26

## Goal

Adopt the entire openalgo-charts feature surface into the TradeX frontend shell so the UI matches the reference `/Users/apple/Downloads/openalgo-charts-master` feature-for-feature, while **every computation lives in `tradex_trading`** (never in JS). Indicator parity is already at 90/91; this closes the remaining surface: transforms, profiles, seasonality, the trade tier, and pure-chrome Family B.

## Architecture

The chart is the single source of truth for the *visible window*; all computation flows through `tradex_trading` via stateless HTTP endpoints. Three transport shapes:

1. **Curves** → existing Tier-2 `POST /api/charts/indicators/compute` (90 indicators — done, unchanged).
2. **Windowed series (transforms)** → new `POST /api/charts/transforms/{id}`. The frontend posts the visible `{symbol, interval, bars}` window; the backend returns transformed bars. Bit-exact vs the reference via a Python port + golden harness (same recipe that delivered 90/91 indicator parity).
3. **Aggregate visuals (profiles, seasonality)** → new `POST /api/charts/profiles/{id}` returning profile cells / a seasonality table-hook payload.

Trade tier reuses existing `/orders`, `/positions`, `/account`; brackets map to the existing `submit_super_order` capability. Family B chrome (linking, shortcuts, primitives) is pure frontend — no computation, no backend.

## Global Constraints

- **Every computation in `tradex_trading`.** No transform/profile/seasonality math in JS.
- **Golden parity** at 1e-9 / None-aligned against the reference built bundle, via the existing `generate_goldens.mjs` + `test_golden_parity.py` harness pattern. Existing gates must stay green: analytics suite 311, golden gate 91.
- Reference dist in `frontend/node_modules/openalgo-charts` is **byte-identical** to `openalgo-charts-master/dist` — the parity target is fixed at 1.6.0.
- Backend ports are pure functions (`compute(bars, params) -> dict`): no session, no broker, no I/O. Fail-loud `ValueError` for unknown ids.
- Frontend register new chart-types/primitives via the library's public APIs (`registerChartType`, `createTier2Indicator`, plugin primitives) — never deep-import internal modules.
- Backend snake_case params; TS camelCase bridged in the test harness (existing PARAM_MAP pattern).
- Do NOT touch the unrelated working-tree files (`parallel_fetcher.py`, broker adapter work, untracked `data/`).

---

## Component 1 — Transforms (backend)

**Files:**
- Create: `trading/src/tradex_trading/analytics/transforms.py`
- Create: `trading/scripts/generate_goldens_transforms.mjs` (or extend the existing `generate_goldens.mjs`)
- Create: `trading/tests/analytics/goldens/transforms/*.json`
- Create: `trading/tests/analytics/test_golden_parity_transforms.py`

**Indicators to port (from reference `src/transform/`):**

| id | TS source | backend params | output |
|---|---|---|---|
| heikin-ashi | heikin-ashi.ts | — | transformed OHLC bars |
| renko | renko.ts | brick=ATR?/fixed, useATR | bars |
| range-bars | range-bars.ts | range | bars |
| line-break | line-break.ts | line | bars |
| point-figure | point-figure.ts | boxSize, reversal | bars |
| kagi | kagi.ts | reversal, useATR | bars |

Each is a **Python port** of the TS `calc`, identical to the indicator-port recipe: read the TS source, implement `compute(bars, **params) -> list[dict]`, generate goldens from the compiled bundle, gate at 1e-9.

## Component 2 — Profiles + Seasonality (backend)

**Files:**
- Modify: `trading/src/tradex_trading/analytics/volume_profile.py` (extend to reference bucketing if needed)
- Modify: `trading/src/tradex_trading/analytics/footprint.py` (extend to reference cell shape if needed)
- Create: `trading/src/tradex_trading/analytics/profiles.py` (market-profile, TPO over the reference model)
- Create: `trading/src/tradex_trading/analytics/seasonality.py` (monthly %-change table)

**Profile computations (from reference `src/profile/`):**
- `volume-profile`: `computeVolumeProfile(bars, opts)` → price buckets + volume per bucket, POC/VAH/VAL/LVN. Backend `volume_profile.py` already has `poc/value_area/vah/val/lvn` primitives — reuse, verify bucketing parity.
- `tpo`: `computeTpo(bars, opts)` → TPO letter-coded cells. New port.
- `market-profile`: market-profile aggregate over TPO. New port.
- `footprint`: `computeFootprint(bars, opts)` + `diagonalImbalances`/`cumulativeDelta`/`stackedImbalances`. Backend `footprint.py` has a `Footprint` class — reuse, verify cell shape parity.

**Seasonality** is a table (not a curve): monthly close-to-close % change matrix, one row per year, plus an average row. It is the 91st reference indicator and fills the only remaining indicator gap. Transport: the reference descriptor's `table` hook; in our stack it becomes a profile-style payload served by `POST /api/charts/profiles/seasonality`.

## Component 3 — Backend routes

**File:** `trading/src/tradex_trading/interface/routes/chart.py` (extend) or a new sibling `trading/src/tradex_trading/interface/routes/transform.py`.

- `POST /api/charts/transforms/{id}`
  - body: `{ "id": "renko", "params": {...}, "bars": [{"time": int, "open": float, "high": float, "low": float, "close": float, "volume": float}] }`
  - response: `{ "id": "renko", "bars": [{"time": int, "open": float, "high": float, "low": float, "close": float}] }`
- `POST /api/charts/profiles/{id}`
  - body: `{ "id": "volume-profile", "params": {...}, "bars": [...] }`
  - response: `{ "id": "volume-profile", "cells": [{ "price": float, "volume": float, ... }], "poc": float|null, "vah": float|null, "val": float|null }`
  - for `seasonality`: `{ "id": "seasonality", "table": { "rows": [{"year": int, "months": [float|null x12]}], "avg": [float x12] } }`
- Unknown id → 422 with fail-loud message. Schema validation mirrors `compute_indicator`.

**Time convention:** bars carry UTC seconds (chart timeline); backend converts to IST for session-aware profile/seasonality bucketing, consistent with the existing datalake convention.

## Component 4 — Frontend

**Files:**
- Create: `frontend/src/transforms.ts`
- Create: `frontend/src/profiles.ts`
- Create: `frontend/src/seasonality.ts`
- Create: `frontend/src/trade.ts`
- Create: `frontend/src/linking.ts`
- Create: `frontend/src/shortcuts.ts`
- Create: `frontend/src/primitives.ts`
- Modify: `frontend/src/main.ts` (register chart-types, mount primitives, shellbar entries)

**Transforms** (`transforms.ts`): for each transform id, a backend-driven chart-type:
- `registerChartType(id, { draw, extents, defaultStyle, isPriceSeries: true })` that renders transformed bars (P&F → `point-figure` renderer, kagi → `kagi` renderer, rest → `candlestick`).
- The type's data path: `POST /api/charts/transforms/{id}` with the current visible window; refetch on window/params change. The library's `dataLayer` renders backend bars verbatim.
- A shellbar/menu control switches the price series between raw and each transform (mirroring the reference's transform tier).

**Profiles** (`profiles.ts`): plugin primitives per profile id. Each attaches to the pane, POSTs the visible window, and draws backend cells (volume histogram, TPO letters, footprint bid/ask). Uses the library's primitive API (`IPrimitive`).

**Seasonality** (`seasonality.ts`): a Tier-2-style descriptor registered via `registerIndicator(createTier2Indicator({...}))` whose `fetch` returns the backend table; the reference's `table` hook renders the year/month matrix on the pane.

**Trade tier** (`trade.ts`): a `TradingController`-host:
- positions/orders from `GET /api/charts/positions` + `GET /api/charts/orders` (+ WS order stream if added)
- on-chart order lines + position markers (reference `WorkingOrderLine`, `PositionMarker`)
- PnL/risk readout (reference `unrealizedPnl`, `riskReward`)
- bracket order UI → `POST /orders` (existing route, `routes/orders.py`) with super-order legs → existing `submit_super_order` capability
- Reuses `TradexTradeFeed` from `trade-feed.ts`.

**Family B chrome** (pure frontend, no backend):
- `linking.ts` — `LinkGroup`/`LinkCrosshair` between the main chart and comparison panes.
- `shortcuts.ts` — `ShortcutManager` with `DEFAULT_KEYMAP` wired to existing actions (symbol search, interval, indicator modal, replay, compare).
- `primitives.ts` — watermark (`LogoWatermark`), series markers (`SeriesMarkers`), price levels (`PriceLevels`: prev close, session high/low), pane legend (`PaneLegend`), event markers (`EventMarkers`), buy-sell buttons, chart table — mounted with sensible defaults.

## Component 5 — Testing

- **Backend parity gate:** `test_golden_parity_transforms.py` + `test_golden_parity_profiles.py` gated at 1e-9/None-aligned. Every transform/profile/seasonality id in the reference must have a backend id (zero gaps) — extend the `ref-ids` parity comparison.
- **Backend unit:** edge tests for each transform/profile (mirror `test_indicator_registry.py` style).
- **Existing gates:** analytics suite stays 311, golden gate stays 91.
- **Frontend:** `tsc --noEmit` + `vite build`; `frontend/e2e` drives transform switch, profile overlay, seasonality table, trade markers, linking, shortcuts.
- **Parity script:** compare backend transform/profile catalogue ids vs the reference built bundle ids; assert zero gaps, zero extras.

## Decomposition into Plans

Four independently shippable sub-plans, backend-first (each: golden gate green before frontend wiring):

1. **P1 — Transforms:** backend ports (`transforms.py`) + golden harness + `POST /transforms/{id}` + frontend chart-types (`transforms.ts`).
2. **P2 — Profiles + Seasonality:** backend ports (`profiles.py`, `seasonality.py`, extend `volume_profile.py`/`footprint.py`) + goldens + `POST /profiles/{id}` + frontend primitives (`profiles.ts`, `seasonality.ts`).
3. **P3 — Trade tier:** `TradingController` host (`trade.ts`) over existing `/orders` `/positions` `/account`, bracket UI, on-chart markers; add WS order stream endpoint only if needed.
4. **P4 — Family B chrome:** `linking.ts`, `shortcuts.ts`, `primitives.ts` + shellbar entries.

Each plan is independently testable and mergeable. Order is P1 → P2 → P3 → P4.

## Explicit Non-Goals

- No new indicator-curve port (91st indicator `seasonality` is covered by the profile/table transport; all 90 curves already shipped).
- No change to the existing indicator Tier-2 path.
- No tick-level (intraday sub-minute) transform backend; transforms operate on whatever window the chart serves (1m+).
- No persistence of drawings/transforms to the backend (drawings already work via the library; out of scope here).