# Backtest / Strategy / Screener Engine — Discovery & Gap Analysis

**Date:** 2026-09-13
**Scope:** Audit this repo against a 38-section "production-grade DuckDB
backtesting, strategy, screener and live-parity engine" specification, decide
what to reuse, and name the real gaps.
**Method:** source inspection plus executed tests. Every claim below is
traceable to a file and line, or to a command whose output is quoted.

---

## 0. Verdict

**The architecture the spec asks for already exists and is tested.** Its core
requirement — *one trading brain, multiple execution/data environments* — is
implemented, not aspirational:

- `BacktestEngine` constructs `ReactiveStrategyEngine`
  (`trading/src/tradex_trading/replay/backtest.py:270`).
- The **live** boot path constructs the *same* `ReactiveStrategyEngine`
  (`trading/src/tradex_trading/runtime/startup.py:412`).

There is no `BacktestStrategy` / `LiveStrategy` split. The strategy protocol
(`trading/src/tradex_trading/strategy/core/protocols.py`) is identical in both.

```
$ python -m pytest trading/tests/parity/ -q
131 passed in 2.80s

$ python -m pytest trading/tests domain/tests -q
2257 passed in 38.10s
```

Most of the spec's 38 sections are therefore **reuse and document**, not
build. Section 36 of the spec demands exactly this answer, and Section 27
forbids solving it by duplicating working subsystems. Building a parallel
engine would be the architecture debt the spec sets out to prevent.

The genuine gaps are narrow and listed in §6. One of them is closed in this
change (§7).

---

## 1. Traced historical data flow (as it exists)

```
Parquet  (data/ohlcv/, Hive: symbol=/year=/month=)
   ↓   tradex_trading/datalake/parquet_storage.py  (ParquetStorage)
DuckDB   ← only a `duckdb_scan()` helper (parquet_storage.py:319);
   ↓      the analytical layer lives OUT of process (see G5)
Historical Data Layer
   ↓   datalake/market_provider.py  (ParquetMarketProvider,
   ↓                                 BulkPrefetchMarketProvider)
Candle / Market Data Model
   ↓   domain/market.py  (OHLC, Candle, Quote, Depth, HistoricalSeries)
   ↓   domain/market_calendar.py, domain/timeframe.py (_bucketize)
HistoricalSeries.resample → Timeframe aggregation
   ↓
Indicator Layer
   ↓   analytics/registry.py (IndicatorRegistry) + analytics/indicators.py
   ↓   (full catalog: trigrams/plots/levels/fn)
Scanner / Screener
   ↓   strategy/core/scanner.py (ScannerEngine) + strategy/extensions/scanners/*
Strategy
   ↓   strategy/core/protocols.py (Strategy) ── one contract
   ↓   strategy/core/engine.py (ReactiveStrategyEngine)
Signal
   ↓   domain/strategy.py (Signal, StrategyContext)
Order / Execution Simulator
   ↓   execution/engine.py (ExecutionEngine) + execution/fill_sources.py
   ↓   (Simulated / Paper / Replay / Broker) + fees.py + slippage.py
Position Manager
   ↓   execution/position_manager.py → domain/position_math.py (ONE model)
   ↓   execution/cash_ledger.py, trading_cache.py, mark_to_market.py
Portfolio / P&L
   ↓   domain/execution.py (Position, Account, PortfolioSnapshot)
Backtest Result
   ↓   replay/backtest.py (BacktestResult)   ← thin; see G2/G3
API
   ↓   interface/routes/chart.py (POST /api/charts/backtest)
Frontend
   ↓   frontend/src/tier2.ts (Tier-2 backend-indicator contract)
   ↓   openalgo-charts (render-only)
Chart / Equity curve / Trades / Statistics
```

## 2. Traced live data flow (as it exists)

```
Live Tick Feed
   ↓   brokers/* (Dhan / Upstox WS) → runtime/market_feed.py
Candle Builder
   ↓   runtime/bar_aggregator.py  (BarAggregator)
   ↓   ⚠ same code path as replay — SyntheticTickGenerator feeds it too
Live Market Data Event
   ↓   reactive/bus.py (ReactiveBus) / thread_safe_bus.py
   ↓   domain/events.py (DomainEvent + 10 typed events)
Indicator State
   ↓   analytics/registry.py — same registry as historical
Scanner / Strategy
   ↓   SAME ReactiveStrategyEngine as §1
Signal → Real Order Execution
   ↓   execution/engine.py + fill_sources.BrokerFillSource
   ↓   sdk/live_fill_bridge.py
Position Manager → Portfolio / P&L
   ↓   SAME position_manager.py / position_math.py as §1
Frontend
   ↓   interface/routes/stream.py (/ws/stream)
```

## 3. Convergence point

Both flows converge at the **Candle**, exactly as the spec requires in §12:

> `BarAggregator`: *"One aggregation code path for every source: live broker
> quotes (MarketFeed) and synthetic generator quotes (replay) both feed the
> same buckets, so a forming bar means the same thing regardless of where the
> ticks came from."* — `runtime/bar_aggregator.py:1`

and at the **strategy runtime**, and at the **position model**:

> `domain/position_math.py:1`: *"Every execution mode books positions and
> realized P&L through exactly the same math here: the reactive pipeline
> (`PositionManager`), the backtester (`BacktestEngine`), the paper broker
> (`PaperBroker`), and corporate-action adjustments … so there is no
> duplicated accounting logic that could diverge between backtest, replay,
> paper, and live (parity review CRITICAL-1)."*

---

## 4. Reuse verdict by subsystem (spec §36 format)

| Spec | Subsystem | What exists | Reusable? | Action |
|---|---|---|---|---|
| §3 | Canonical market data | `domain/market.py` — `OHLC`/`Candle`/`Quote`/`Depth`/`HistoricalSeries`, frozen, `Decimal`; IST-naive datalake convention; `market_calendar.py` | **Yes** | None |
| §4 | DuckDB historical layer | `ParquetStorage` + `ParquetMarketProvider`; DuckDB only via `duckdb_scan()` | Partial | **G5** |
| §5 | SQL indicators | `duck_analytics/scanners.py::_technical_sql` (out of process) | Partial | **G5** |
| §6 | Indicator registry | `analytics/registry.py` — `IndicatorRegistry`, `IndicatorSpec`, `register_indicator` | Partial | **G4** |
| §7 | Screener | `strategy/core/scanner.py` + `extensions/scanners/*` | Yes | **G6** (dedupe) |
| §8 | Strategy contract | `strategy/core/protocols.py` — `strategy_id`, `version`, `on_start/on_bar/on_quote/on_depth/on_fill/on_stop` | **Yes** | None |
| §9 | Zero-parity engine | One `ReactiveStrategyEngine`, two adapters | **Yes** | None |
| §10 | Event model | `domain/events.py` + `reactive/bus.py` (RxPY, causal drain) | **Yes** | None |
| §11 | Look-ahead prevention | `test_golden_mode_parity.py` — next-bar-open fills, no same-close look-ahead | **Yes** | None |
| §12 | Candle semantics | `runtime/bar_aggregator.py` | **Yes** | None |
| §13 | Execution simulator | `fill_sources.py` (4 sources), `fees.py`, `slippage.py`, `fill_model.py` | **Yes** | None |
| §14 | Shared position model | `position_manager.py`, `domain/position_math.py` | **Yes** | None |
| §15 | Replay engine | `replay/synthetic_ticks.py` + `/ws/stream` replay controls | **Yes** | None |
| §16 | Determinism | seed-stable correlation ids; `version` swept onto signals/orders | Yes | **G2** (run record) |
| §17 | Strategy+screener composition | scanner bound into runtime at boot | Partial | **G6** |
| §18 | Multi-symbol / multi-timeframe | `HistoricalSeries.resample`, `_bucketize`, `BulkPrefetchMarketProvider._resampled_frame` | Partial | **G7** |
| §19 | TradingView results UI | only 3 metrics; one Tier-2 equity pane | **No** | **G1/G2/G3** |
| §20 | Async backtest API | `POST /api/charts/backtest` is synchronous | Partial | **G8** |
| §21 | Frontend indicator integration | `frontend/src/tier2.ts` Tier-2 contract | **Yes** | None |
| §22 | openalgo-charts integration | adapter layers in `frontend/src/*` | **Yes** | None |
| §23 | `BacktestRun` domain object | flat `BacktestResult` | **No** | **G2** |
| §24 | Testing | 2257 passing incl. 131 parity tests | Yes | **G1** (UI E2E) |
| §25 | Data-quality gate | `datalake/gap_detector.py`, `corporate_actions.py` exist | Partial | **G9** |
| §26 | Idempotency / dupes | `execution/idempotency.py`, `order_store.py` | **Yes** | None |
| §30 | Conformance report | 14 parity test files pass | Partial | **G10** |
| §31 | Observability | `reactive/event_log.py` | Partial | **G3** |
| §34 | Documentation | `docs/ARCHITECTURE.md` + dated specs/plans | Yes | this doc |

---

## 5. Indicator placement audit (spec §2)

**This is already resolved and documented — including the exact failure mode
the spec warns about.** `docs/superpowers/specs/2026-09-08-indicator-parity-2-1-0-design.md`
records full 102-indicator parity between `openalgo-charts` and the backend
registry (differential harness, 12 ports, drift fixes).

- **Category A (strategy/screener/backtest-critical):** all in the backend
  registry (`analytics/indicators.py`, catalog of `IndicatorSpec` with
  `id/name/category/placement/params/plots/fn`).
- **Category B (visualization-only):** permitted to stay in the chart.
- **Category C (shared):** served to the chart over the **Tier-2 external-data
  contract** (`frontend/src/tier2.ts` → `createTier2Indicator`), so the chart
  renders backend values rather than recomputing them.

`CLAUDE.md` encodes the rule directly:

> *"DON'T compute indicators/strategy logic in the frontend — the chart
> renders; `tradex_trading` computes (new backend indicator registry entries
> appear in the UI automatically via the Tier-2 wiring)."*

So `Backend RSI = 61.23 / Frontend RSI = 61.71` cannot happen by construction.

---

## 6. The real gaps

Ranked by value. Only G1–G3 block the spec's Definition of Done.

**G1 — No TradingView-style results UI.** The chart has a single
`backtest-equity` Tier-2 pane (`frontend/src/tier2.ts`). There is no statistics
panel, no trade list, no drawdown curve, no daily/monthly P&L, no win/loss
distribution. The spec's DoD explicitly fails on *"the UI only shows a basic
trade table"*.

**G2 — `BacktestResult` is not a first-class domain object.** It is a flat
dataclass of 9 fields (`replay/backtest.py:81`). There is no `BacktestRun`
carrying run metadata, config hash, dataset version, indicator versions, or the
orders/positions/trades legs. Reproducibility (§16) is therefore only partially
satisfiable.

**G3 — The statistics block is 3 functions.** `analytics/reports.py` held only
`sharpe_ratio`, `max_drawdown`, `total_return`. Missing: profit factor, win/loss
rate, gross profit/loss, average/largest trade, expectancy, risk/reward,
Sortino, exposure, MAE/MFE — and there was **no `Trade` concept at all**
(`domain/execution.py` has `Order`/`Fill`/`Position`/`Account`/`PortfolioSnapshot`,
no `Trade`). *Closed in this change — see §7.*

**G4 — Indicator registry metadata is thinner than the spec's
`IndicatorDefinition`.** `IndicatorSpec` carries
`name/inputs/params/outputs/compute`. The spec wants `version`,
`warmup_period`, `sql_supported`, `incremental_supported`, `stateful`,
`vectorized_supported`, `frontend_supported` so the registry can drive SQL
pushdown and incremental warm-up decisions.

**G5 — DuckDB is a separate stack, not the engine's historical provider.**
`trading/` references duckdb exactly **4 times**, all inside a single
`duckdb_scan()` helper. The real DuckDB work lives in
`services/duckdb-analytics/` (own `catalog.py`, `query.py`, `screener.py`,
`scanners.py`, `resample.py`), and **nothing in `trading/` imports it** — only
`poc/` scripts do. So spec §4's "DuckDB as the primary historical analytics
layer" is unbuilt on the engine side.

**G6 — Two independent screener implementations.** Python
(`strategy/core/scanner.py` + `extensions/scanners/*`) and DuckDB SQL
(`duck_analytics/scanners.py::scan_screener`). The same logical condition
("RSI < 30") is expressed twice, in two languages, with no shared definition.
Directly against spec §7 and §27.

**G7 — Multi-timeframe synchronisation is implicit.** Aggregation exists
(`resample`, `_bucketize`, `_resampled_frame`) but there is no documented
contract for "the 15m bar and the 1m bar a strategy sees are synchronised",
which is spec §18's explicit race-condition concern.

**G8 — Backtest API is synchronous.** `POST /api/charts/backtest` blocks for
the whole run. No `backtest_id`, no `GET /backtests/{id}`, no pagination or
downsampling — spec §20.

**G9 — No data-quality gate before a backtest.** `gap_detector.py` and
`corporate_actions.py` exist but are not enforced as a fail-loud precondition
on the backtest path (spec §25).

**G10 — No unified conformance report.** 14 parity test files pass, but there
is no single command that prints the spec §30 block
(`Signal parity: PASS / Order parity: PASS / …`) with a per-event diff on
failure.

---

## 7. Implemented in this change (G3)

New module `trading/src/tradex_trading/analytics/trade_metrics.py`:

- **`Trade`** — one closed round trip (the results-UI trade-list row): symbol,
  side, qty, entry/exit time+price, gross P&L, costs, net P&L, MAE, MFE,
  holding period, exit reason. `.to_dict()` is JSON-ready.
- **`round_trip_trades(fills, ...)`** — FIFO lot pairing over the shared `Fill`
  stream. Handles partial exits, scale-ins and reversals; attributes order-level
  costs proportionally to each matched portion; measures MAE/MFE from bars when
  supplied (binary search over the holding window) and reports `None` rather
  than guessing when they are not.
- **`compute_statistics(trades, equity_curve=..., frequency=...)`** — the full
  spec §19 block: net/gross profit, gross loss, profit factor, win/loss rate,
  total/winning/losing trades, average trade, average winner/loser, largest
  win/loss, expectancy, risk/reward, max drawdown (+ %), Sharpe, Sortino,
  exposure. `.to_dict()` is float/int at the API boundary.

Also added **`sortino_ratio`** to `analytics/reports.py` (its natural home,
beside `sharpe_ratio`), reusing the existing `win_rate` from
`analytics/probability.py` rather than re-deriving it.

Verification:

```
$ python -m pytest trading/tests/analytics/test_trade_metrics.py -q
18 passed

$ python -m pytest trading/tests domain/tests -q
2257 passed   # no regression

$ ruff check <4 changed files>      → All checks passed
$ mypy  <2 changed files>           → Success: no issues found
```

Deriving trades from `Fill` (not from backtest-specific dicts) is deliberate:
the same function serves backtest, replay, paper and live, because all four
already produce the same `Fill` objects through `position_math.apply_fill`.

---

## 8. Recommended next phases

Ordered to unblock the spec's Definition of Done with the least new surface:

1. **G2 — `BacktestRun` domain object + config hash.** Bundle metadata,
   config, trades, orders, fills, positions, equity, drawdown, metrics, and
   stamp dataset/strategy/indicator/execution-model versions. *Depends on: §7.*
2. **G1 — Results UI in `openalgo-charts`.** Wire the §7 stats block and trade
   list into chart panes + markers (entry/exit/SL/TP), drawdown and equity
   curves, monthly P&L, distributions.
3. **G8 — Async backtest API.** `POST /backtests → backtest_id`,
   `GET /backtests/{id}[/trades|/equity|/metrics|/chart]`, paginated.
4. **G6 — One screener definition, two backends.** Express conditions once and
   lower them to DuckDB SQL (bulk) or the incremental engine (live).
5. **G5 — `HistoricalDataProvider` with a `DuckDBParquetProvider`** so the
   engine stops loading Parquet through pandas.
6. **G4, G7, G9, G10** — registry metadata, MTF sync contract, data-quality
   gate, unified conformance report.

Do **not** build a second strategy runtime, candle builder, position model,
event bus or indicator registry. Those exist, are shared, and are covered by
131 passing parity tests.

---

## 9. Reproducing this audit

```bash
python -m pytest trading/tests/parity/ -q          # 131 parity tests
python -m pytest trading/tests domain/tests -q     # 2257 total
grep -rn "ReactiveStrategyEngine" trading/src      # the one runtime
grep -rn "duckdb" trading/src                      # 4 hits: the G5 evidence
grep -rn "duck_analytics" trading/ poc/            # only poc/ imports it
```
