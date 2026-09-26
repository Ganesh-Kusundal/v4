# Architectural Audit — Shotgun Surgery & Structural Inconsistency

**Repo:** `v4` (branch `chore/cleanup-overhaul`, 432 modified files)
**Method:** static import-graph extraction, AST clone detection, literal-diff analysis, targeted source reading, empirical execution of suspect paths.
**Ground truth:** 3,856 tests collect clean; 155 parity tests pass; `tests/test_import_boundaries.py` (587 lines) enforces declared layer edges and currently passes.

> **Headline:** this codebase is *not* badly architected. It is **mid-migration**, and the audit's real subject is the **migration seam itself**. A DDD package set (16 `tradex_*` packages) is already extracted, boundary-tested, and money-path-centralized. Alongside it sits a 159-file legacy `tradex_trading` mirror and 6 half-wired cycles. Most remaining smells are **re-hardcodings of constants that already have a canonical home** — the exact failure mode of an extraction that centralized the definition but not the call sites.

---

# PHASE 1 — Codebase Mapping

## 1.1 Package inventory (17 workspace members, uv workspace)

| Package | src files | src LOC | test files | test LOC | Declared responsibility |
|---|---:|---:|---:|---:|---|
| `domain` | 18 | 3,208 | 15 | 2,402 | Shared kernel: enums, value objects, instruments, protocols, wire mapping, events. **Zero deps.** |
| `brokers` | 59 | 12,712 | 44 | 11,696 | Broker SDK: Dhan/Upstox/Paper adapters, auth, token lifecycle, rate limiting, circuit breaker, WS streams |
| `config` | 3 | 394 | 1 | 5 | `AppConfig`, env loader |
| `trading` | **159** | **2,572** | **274** | **50,239** | **Legacy composition root — 155 of 159 files are ≤25-line re-export shims** |
| `research` | 3 | 445 | 2 | 519 | Research manifests, point-in-time validators |
| `operations` | 1 | 41 | 1 | 29 | Runbook + metrics catalog index |
| `observability` | 2 | 176 | 1 | 14 | Metrics registry |
| `analytics` | 36 | 11,973 | 1 | 5 | Indicators (102 registered), studies, profiles, orderflow |
| `reactive` | 4 | 584 | 1 | 5 | RxPY event bus, thread-safe bus, event log |
| `execution` | 21 | 5,896 | 1 | 5 | Execution engine, OMS, risk, fees, fills, stores |
| `strategy` | 27 | 2,608 | 1 | 5 | Strategy runtime, registry, extensions (scanners + strategies) |
| `replay` | 5 | 1,478 | 1 | 5 | Backtest engine, walk-forward, optimization, synthetic ticks |
| `application` | 2 | 100 | 1 | 5 | Command handlers |
| `market_data` | 12 | 2,527 | 1 | 5 | Datalake: parquet storage, catalog, gap detection, fetchers |
| `interfaces` | 27 | 6,177 | 1 | 5 | FastAPI app, routes, auth, CLI |
| `runtime` | 17 | 4,261 | 1 | 5 | Boot, live wiring, market feed, bar aggregation, supervision |
| `persistence` | 1 | 19 | 1 | 5 | SQLite order/event stores (facade only) |

**Non-workspace code:** `frontend/src` (22 TS files, 7,770 LOC), `trading/scripts` (22 py), `scripts` (13 files), `poc/` (19 entries), `benchmarks/`, `openalgo-charts/` (vendored chart library, has its own `.git`).

> **⚠ Finding M-0 (structural, HIGH).** `trading/` (159 files, 2,572 LOC) mirrors the *entire* extracted package set — `analytics/` 36 shims, `datalake/` 12 shims, `execution/` 22 shims, `strategy/` ~25 shims, `interface/` ~12 shims, `reactive/`, `replay/`, `research/`, `runtime/`, `application/`. Only **2 files hold real logic**: `sdk/session.py` (497 L) and `sdk/live_fill_bridge.py` (283 L). Meanwhile `trading/tests` still holds **274 of 513 test files (50,239 LOC)** — the test suite was never strangled alongside the source.

## 1.2 Import graph (package → package), extracted mechanically

```
domain        → (none)                                     ← the only true kernel
operations    → (none)
observability → (none)
research      → domain
config        → domain
reactive      → domain
analytics     → domain
brokers       → domain
execution     → domain, reactive, observability
strategy      → domain, analytics, market_data
application   → domain, execution
persistence   → execution
interfaces    → domain, config, execution, application, strategy, replay,
                market_data, analytics, reactive, runtime
replay        → domain, execution, strategy, analytics, market_data, reactive
runtime       → domain, brokers, config, execution, strategy, market_data,
                reactive, observability, tradex_trading ⚠
trading       → all 16 (documented composition-root exemption)
```

**Import count by target:** `tradex_domain` 476 · `tradex_brokers` 159 · `tradex_execution` 60 · `tradex_analytics` 57 · `tradex_market_data` 46 · `tradex_strategy` 42 · `tradex_runtime` 42 · `tradex_interfaces` 42 · `tradex_trading` 22 · `tradex_config` 17 · `tradex_replay` 9 · `tradex_reactive` 9 · `tradex_observability` 4 · `tradex_application` 2.

## 1.3 Manifest-vs-reality drift (undeclared internal dependencies)

| Package | Imports but does **not** declare in `pyproject.toml` | Declares but never imports |
|---|---|---|
| `market_data` | `tradex_replay`, `tradex_runtime` | — |
| `interfaces` | `tradex_brokers` | `httpx`, `websockets` |
| `runtime` | `tradex_trading` | `rx` |
| `brokers` | — | `protobuf`, `rx` |
| `trading`, `execution`, `replay` | — | `rx` |

Every `pyproject.toml` version is `0.1.0` with `>=0.1.0,<0.2` — **no package declares a dependency on a newer extracted sibling**, so packaging is not enforcing the graph the boundary test enforces.

## 1.4 Shared vocabulary: what already exists (reuse these, do not reinvent)

| Asset | Location | Status |
|---|---|---|
| `market_calendar` (`MARKET_OPEN/CLOSE`, `IST`, `DHAN_SESSION_*`, `NSE_HOLIDAYS_2026`, `to_ist_naive`) | `domain/market_calendar.py` | **Canonical** — 4 call sites bypass it |
| `timeframe` registry (`bucket_seconds`, `dhan_interval`, `upstox_unit_interval`) | `domain/timeframe.py` | **Canonical** — 3 runtime modules re-declared a subset and dropped `W1` |
| `enums` (`ExchangeId`, `BrokerId`, `Timeframe`, `ProductType`, `OrderType`, `OrderSide`, `TimeInForce`) | `domain/enums.py` | Canonical; leaks across the Python↔TS boundary |
| `q2` (money rounding, `ROUND_HALF_UP`) | `domain/utils.py:19` | Canonical; `fees.py` bypasses it 4× |
| `position_math.apply_fill` (weighted-avg + realized P&L) | `domain/position_math.py:89` | **Sole authority** — verified, no second definition |
| `FeeCalculator` (STT/brokerage/exchange/GST/SEBI) | `execution/fees.py` | Canonical, named constants, history documented |
| `FillModel` | `execution/fill_model.py` | Canonical cross-mode fill resolution |
| `PositionAccountant` / `fill_with_remark` | `execution/position_accountant.py` | Atomic fill+remark seam |
| `IndicatorRegistry` + `@register_indicator` | `analytics/registry.py` | Good — 102 indicators, one dispatch seam |
| `StrategyRegistry` | `strategy/registry.py` | Good |
| `BrokerAdapter`/`SessionFacade`/`Clock` protocols | `domain/protocols.py` | Good, but returns `object` at 6 sites |
| Per-broker rate tables | `brokers/{dhan,upstox,paper}/rate_table.py` | Good — **provider-specific, do not globalize** |
| Path anchoring (`datalake_root()`, `default_runtime_dir()`) | `market_data/paths.py`, `brokers/common/paths.py` | Good pattern, inconsistently adopted |
| Boundary/authority guards | `tests/test_import_boundaries.py` (587 L), `test_no_foreign_private_writes.py` | **Strong** — but `_SRC`-coverage gaps (see F-21) |

---

# PHASE 2 — Shotgun Surgery Detection

Findings ordered by blast radius. Every one was opened and read; every `file:line` was verified.

---

## Tier 1 — HIGH impact, verified divergences

### [SMELL-01] ⚠️ RETRACTED — investigated, disproved, kept for the record
**Original claim:** duplicate brokerage accrual — two independent dicts compute the same fees, breaking zero-discrepancy parity.
**Files:** `execution/src/tradex_execution/engine.py:244,782-783`; `replay/src/tradex_replay/backtest.py:278,320-325`
**Status: RETRACTED. The finding is wrong.**

**Why the claim looked true:** both dicts exist, both call `FeeCalculator.calculate_capped`, and an instrumented 200-fill run showed **400 calls for 200 fills** (`engine.py:783` ×200, `backtest.py:321` ×200). That is real double-computation.

**Why it is not a defect:**
1. **The two ledgers are disjoint by construction.** The engine charges to `position_manager.on_fee` (`engine.py:816`); the backtest charges to its own `CashLedger` (`backtest.py:325`). Inside backtest, `engine._cash_ledger` is `None`, so `engine.py:818` never fires. The backtest's ledger is the **only** cash ledger in that mode.
2. **They measure different things.** Position book = realized P&L. Cash ledger = reported `total_fees`. The two are *supposed* to be computed independently — one is an accounting output, one is a reporting aggregate.
3. **They agree empirically.** Instrumented run over 200 fills: position book `12.80`, cash ledger `12.80`, **divergence `0.00`**. A second run with multi-order bursts: **0 divergent orders**. The two `calculate_capped` calls are *deterministic pure functions of `(fill, accrued)`*, and both ledgers feed the same `accrued` value, so identical inputs give identical outputs by construction.
4. **The duplication is a deliberate, documented fix.** `backtest.py:273-278` states the rationale: brokerage caps **per order, not per fill**; without a second accrual the cap would be charged once per partial and the backtest would overcharge split orders. It explicitly names the engine's `_brokerage_accrued` as the state it is mirroring.

**Residual (LOW, real but minor):** the fee computation *is* performed twice per fill — pure-function recomputation, not a correctness bug. If `calculate_capped` ever became stateful or non-deterministic, the two ledgers would silently diverge. That is a fragility, not a parity break.
**Refactored as:** REF-8 becomes *"extract `BrokerageAccrual` so the fee is computed once and fanned out to both books"* — a performance/robustness cleanup, **not** a correctness fix. **No golden P&L files should change.**

**Lesson for the audit:** the sub-agent that produced this finding reported a specific, plausible, and *wrong* number ("8 calls for 4 fills", "60.14 vs 41.87") backed by a real experiment. The experiment was misread. Every Tier-1 claim in this report has been re-derived independently.

### [SMELL-02] B — Two different "RSI" in the same package; one is not Wilder's
**Files:** `strategy/src/tradex_strategy/extensions/strategies/rsi_reversal.py:41` (calls canonical `rsi`), `strategy/src/tradex_strategy/extensions/strategies/mean_reversion.py:121-138` (private `_rsi`, non-Wilder), `analytics/src/tradex_analytics/indicators.py:81-128` (canonical Wilder RSI)
**Symbol:** `rsi` / `_rsi`
**Blast Radius:** 3 files, **5 of 10 strategy extensions** roll their own indicator
**Impact:** HIGH
**Evidence:** `mean_reversion.py:124-126` docstring states *"Uses plain (un-smoothed) average gain/loss — reference-grade, not Wilder's smoothing."* `analytics/indicators.py:116-119` applies Wilder's recursive smoothing. Two strategies named for the same oscillator compute different values on the same bars. Also private `_sma` in `multi_symbol_sma_cross.py:117` and `sma_cross.py`, private `_atr` in `bracket_breakout.py`. Import-count per strategy file: `rsi_reversal`/`macd_cross`/`bollinger_breakout`/`ema_ribbon_pullback`/`supertrend_flip` import analytics; `mean_reversion`/`multi_symbol_sma_cross`/`sma_cross`/`bracket_breakout` do not.
**Domain-knowledge flag:** whether `mean_reversion` *should* use simple RSI is a strategy-design decision, not a refactor decision. Do not silently change signal values.

### [SMELL-03] H/F — 5 WebSocket backends × 2 brokers, each reimplementing the connect lifecycle
**Files:** `brokers/src/tradex_brokers/dhan/ws_streams.py:334` **and** `:633` (two *byte-identical* `_ensure_ws` in one file), `brokers/src/tradex_brokers/upstox/ws_streams.py:475`
**Symbol:** `_ensure_ws` — clone detector found **3 structural clones, ~69 AST nodes**
**Blast Radius:** 5 classes × ~8 lifecycle methods (`_ensure_ws`, `_open_socket`, `_resubscribe`, `_receive_loop`, `close`, `feed_raw`, `_send_subscribe`, `_cached_instrument`) ≈ **40 methods across 2 files**
**Impact:** HIGH
**Evidence:** `DhanMarketDataStreamBackend` (L220) and `DhanDepthStreamBackend` (L517) are near-verbatim twins; `DhanOrderStreamBackend` (L59) and both Upstox backends repeat the same `_open_socket`→lock→`_resubscribe`→spawn-thread skeleton. `AutoReconnectMixin` already centralizes *backoff*; the *lifecycle* around it is what is duplicated.

### [SMELL-04] E/F — F — 2 brokers × 6 mirrored modules with lockstep-change obligation
**Files:** `brokers/src/tradex_brokers/{dhan,upstox}/` — `_orders.py` (284/202 L), `_portfolio.py` (204/166 L), `_marketdata.py` (388/398 L), `master.py` (152/153 L), `auth_flow.py` (84/155 L), `instruments.py` (121/28 L); plus `client.py` (534/453 L), `adapter.py` (379/330 L), `ws_streams.py` (760/591 L)
**Symbol:** `OrdersMixin(Protocol)`, `PortfolioMixin(Protocol)`, `MarketDataMixin(Protocol)` — same class name, same method set, different bodies
**Blast Radius:** **12 files, 2,968 LOC, 2,500+ test LOC** — clone detector found **18 mirrored test functions** (`test_submit_order`, `test_cancel_order`, `test_modify_order`, `test_get_order`, `test_convert_position`, `test_get_quote`, `test_ltp`, `test_history`, `test_submit_forever_order`, `test_submit_slice_order`, `test_set_order_operations_enabled`, `test_reconnect_disabled_keeps_single_socket`, …) at near-identical line offsets
**Impact:** HIGH
**Evidence:** A new `BrokerAdapter` method touches 2 client files + 2 `_*.py` mixins + 2 adapters + 2 test files = **8 files minimum**, and `test_adapter_protocol.py` (292 L) must agree.

### [SMELL-05] B — Import cycles: 4 confirmed, 2 crossing the market-data/replay/runtime core
**Files:** `market_data/src/tradex_market_data/backtest_loader.py:38,41,207` → imports `tradex_runtime.calendar` (hard) and `tradex_replay.backtest` (TYPE_CHECKING + line 207 runtime import); `replay/src/tradex_replay/backtest.py` → `tradex_market_data`; `runtime/src/tradex_runtime/{startup.py,feed_recovery.py}` → `tradex_market_data`; `runtime/src/tradex_runtime/session.py:7` → `tradex_trading.sdk.session`
**Symbol:** package-level cycles
**Blast Radius:** 4 cycles; `market_data → replay → strategy → market_data` and `market_data → runtime → strategy → market_data` are **3-node cycles through the datalake core**
**Impact:** HIGH
**Evidence:** The declared boundary table *permits* `market_data → {replay, runtime}` while `replay → market_data` and `runtime → market_data` are also permitted — the allowlist encodes a contradiction it cannot detect, because it checks edges, not cycles. `session.py` is a 9-line façade that reaches back into the legacy monolith.

### [SMELL-06] E — Legacy `trading/` mirror: 155 shims + 274 orphaned test files
**Files:** `trading/src/tradex_trading/**` (155 shims), `trading/tests/**` (274 files, 50,239 LOC)
**Blast Radius:** **429 files** — 43% of the Python test corpus tests a package that is 97% re-export
**Impact:** HIGH
**Evidence:** `analytics/` 36 shims / `datalake/` 12 / `execution/` 22 / `interface/` ~12 / `strategy/` ~25. Test dirs mirror the same 13 concerns (`analytics`, `application`, `datalake`, `execution`, `interface`, `reactive`, `replay`, `research`, `runtime`, `scripts`, `sdk`, `strategy`, plus `parity/` and `contracts/`). `parity/` and `contracts/` (32 files, 7,296 LOC) are the highest-value suites in the repo and they live in the *legacy* tree.

### [SMELL-07] B — Zero shared bootstrap for 14 operational scripts
**Files:** `trading/scripts/{audit_symbol_resolution,backfill_2025,fill_gaps,reconcile_bars,rename_symbol,repair_gaps,sync_today}.py` (identical `sys.path` block, **md5 `887fe0b8…`**), `+7` more; `trading/scripts/{backfill_parquet,…}.py` (variant, md5 `c4f4f162…`)
**Symbol:** `ROOT`/`sys.path.insert` bootstrap; `--data-root` flag parsing; `ohlcv` root resolution
**Blast Radius:** 14 scripts; **0 import `tradex_market_data` directly** — all 15 route through the `tradex_trading.datalake` shim; 4 separate implementations of the `base_path.name == "ohlcv"` rule
**Impact:** HIGH
**Evidence:** Adding a new extracted package requires editing the bootstrap in every script — the `market_data/src` entry is *missing* from the bootstrap entirely, so scripts work only because the shim masks it.

### [SMELL-08] A — `[LIVE BUG]` Scripts pass 2 brokers to a 1-broker API
**Files:** `trading/scripts/backfill_2025.py:98`, `trading/scripts/backfill_parquet.py:187` → `market_data/src/tradex_market_data/parallel_fetcher.py:184-188`
**Symbol:** `ParallelHistoryFetcher.__init__` raises `ValueError` unless `len(brokers) == 1`
**Blast Radius:** 2 scripts, **both permanently broken when both brokers are configured**
**Impact:** HIGH
**Evidence:** Re-verified directly. The guard is at `parallel_fetcher.py:184` (`if len(brokers) != 1: raise ValueError("... requires exactly one broker")`). Both scripts call `ParallelHistoryFetcher(brokers, ...)` where `brokers` is a multi-broker dict. `ValueError` reproduced by execution. Neither script's test covers the both-brokers case. `simple_sync` (single-broker by design) is unaffected.
*Note: the sub-agent cited this guard at lines 21-24; the real location is 184-188. Line numbers re-checked.*

---

## Tier 2 — MEDIUM impact

### [SMELL-09] A — IST timezone: 15 files, 2 incompatible representations
**Files:** `interfaces/src/tradex_interfaces/routes/stream.py:710,900,923,925,968,1119`; `routes/chart.py:42`; `routes/stream_indicators.py:23,72`; `replay_run.py:13`; `brokers/dhan/tick_parser.py:46`; `analytics/profiles.py:15,16`; `analytics/seasonality.py:17`; `frontend/src/feed.ts:665`; `openalgo-charts/src/profile/market-profile.ts:69`; `openalgo-charts/src/feed/time.ts:10`
**Symbol:** `IST` — `domain/market_calendar.py:21` defines it; **nothing outside `domain` imports it**
**Blast Radius:** 15 files
**Impact:** MEDIUM (HIGH at the Python↔TS seam)
**Evidence:** `stream_indicators.py` is the sharpest: `_IST = "Asia/Kolkata"` at L23 for `_IST_ZONE()`, then `IST = timezone(timedelta(hours=5, minutes=30))` at L72 — **byte-for-byte the body of `domain.market_calendar.IST`**, two representations in one file. The numeric-offset form also appears as `IST_OFFSET_SECONDS = 5*3600+30*60` in `profiles.py:16`, `studies_complex.py:193`, `time.ts:10`.

### [SMELL-10] A — Timeframe→seconds map re-declared 3×, one copy missing `W1`
**Files:** `runtime/src/tradex_runtime/bar_aggregator.py:61,71-76`; `runtime/feed_recovery.py:39-44,143`; `runtime/feed_integrity.py:43-48`; canonical `domain/timeframe.py:45-48`
**Symbol:** `_tf_seconds`, `bucket_seconds`
**Blast Radius:** 4 files
**Impact:** MEDIUM
**Evidence:** `domain` maps `Timeframe.W1 → 604800`; `bar_aggregator._tf_seconds` has no `W1` key → a weekly bar-aggregation request raises `ValueError` where the canonical registry would answer. `86_400` vs `86400` spelling drift in all three.

### [SMELL-11] A — Datalake root `"data/"` re-hardcoded past the module written to kill it
**Files:** `market_data/market_provider.py:36,118`; `market_data/backtest_loader.py:56`; `market_data/catalog.py:28` (**`"data/lake"` — a third spelling**); `market_data/paths.py:4` (the fix module)
**Symbol:** `"data/"` / `datalake_root()`
**Blast Radius:** 4 files, 5 divergent spellings
**Impact:** MEDIUM
**Evidence:** `paths.py` docstring names the production incident (2026-09-02: `tradex serve` launched from `trading/` served `source: "none"`, zero bars). `catalog.py` using `"data/lake"` means the two can never agree on a root even after anchoring.

### [SMELL-12] A — Runtime state dir: two *different* defaults for the same env var
**Files:** `config/src/tradex_config/env.py:47` (`.tradex_v4`); `config/src/tradex_config/schema.py:155,203` (`.tradex_v4`); `brokers/common/paths.py:32-37` (`<repo>/runtime`); `interfaces/fastapi_app.py:119-125` (correct — delegates)
**Symbol:** `TRADEX_RUNTIME_DIR`
**Blast Radius:** 3 files
**Impact:** MEDIUM
**Evidence:** With the var unset, the config layer and the broker layer resolve the same variable to **two different directories** — the documented forked-token-state bug, reintroduced through a second default.

### [SMELL-13] G/A — Order-type and product-type vocabularies diverge at the Python↔TS boundary
**Files:** `frontend/src/order-safety.ts:2` (`'MARKET'|'LIMIT'|'SL'`), `frontend/src/chart-lifecycle.ts:204` (`'SL-M'` — **not in the declared union**), `frontend/src/main.ts:144`; `brokers/dhan/client.py:220-223` (`STOP_LOSS`/`STOP_LOSS_MARKET`); `brokers/upstox/client.py:161-165` (`SL`/`SL-M`); `interfaces/routes/orders.py:146,207,444` (**two different defaults in one file**: `MARKET` and `LIMIT`); `frontend/src/shellbar.ts:323,392` (`MIS`/`CNC`/`NRML`); canonical `domain/enums.py:17-22,40-45`
**Symbol:** `OrderType`, `ProductType`
**Blast Radius:** 7 files + **1 silent field drop**
**Impact:** MEDIUM
**Evidence:** Re-verified directly. `routes/orders.py:159-168` (`_build_request`) and `:216-231` (`_build_bracket_request`) both construct the request **without a `product` argument**; `grep -n "product" routes/orders.py` returns **zero hits**. `OrderRequest` does accept `product_type`, so the field is dropped, not rejected. Meanwhile `frontend/src/chart-lifecycle.ts:189` sends `product: 'MIS'` and `shellbar.ts:323` renders a live `MIS`/`CNC`/`NRML` selector. The UI's product selector has no effect on the order.

### [SMELL-14] A — Market session window re-hardcoded past `market_calendar.py`
**Files:** `runtime/feed_integrity.py:120` (`session_window=(time(9,15),time(15,30))`); `trading/scripts/seed_e2e_datalake.py:73`; `trading/scripts/datalake_stats.py:107,109,111` (SQL `09:16:00`/`09:15:00`/`15:29:00`), `:284,286`; `openalgo-charts/src/profile/market-profile.ts:69`
**Symbol:** `MARKET_OPEN`/`MARKET_CLOSE`
**Blast Radius:** 4 files + 1 TS
**Impact:** MEDIUM
**Evidence:** `market_calendar.py` docstring claims it "owns the only copy"; `runtime/calendar.py` and `market_data/parquet_storage.py` import it correctly, proving the pattern works. The SQL's `09:16`/`15:29` interior band is a **fourth, non-obvious** set nothing constrains to stay in sync.

### [SMELL-15] A — Annualisation basis: `252` and `375` as 21 bare literals
**Files:** `analytics/reports.py:19-41` (`252`×16, `375`×2); `analytics/engine.py:165,300,306,307,312` (`252`×5); `analytics/volatility/volatility.py:9`; `trading/scripts/seed_e2e_datalake.py:74`
**Symbol:** `periods_per_year`, `SESSION_MINUTES`
**Blast Radius:** 4 files, 21 sites
**Impact:** MEDIUM
**Evidence:** `reports.py:19` documents the derivation ("NSE 375 min/day") then writes `375` as a bare literal. Changing session length or trading-week count = 21 edits across 4 packages, zero edit point.

### [SMELL-16] B — Session-break heuristic triplicated across Python and TS
**Files:** `analytics/ma_vol.py:68,79`; `analytics/studies/studies_complex.py:264,272`; `openalgo-charts/src/feed/time.ts:490`
**Symbol:** `max(4*gap, 4h)` / `36h` cadence ceiling
**Blast Radius:** 3 files
**Impact:** MEDIUM
**Evidence:** Both Python files explicitly document themselves as ports of the TS `sessionStartIndices`. **They differ in the fallback**: `ma_vol` falls back to the IST calendar day, `studies_complex` returns `None`.

### [SMELL-17] A — Dhan exchange-segment: 4 tables + 7 tables of instrument-kind codes
**Files:** `brokers/dhan/client.py:54-64` (`_DHAN_EXCHANGE_SEGMENT`), `brokers/dhan/tick_parser.py:33-42` (`SEGMENT_EXCHANGE` — the *inverse*, no shared declaration, `6` absent), `brokers/dhan/instruments.py:22-34` (`SEGMENT_CANONICAL`), `brokers/dhan/client.py:75-85` (`_OPTION_LEG_EXCHANGE`); `brokers/dhan/instruments.py:36-38` + `brokers/dhan/client.py:294-306` + `brokers/upstox/master.py:118-120` (`FUTIDX`/`OPTSTK`/… three times, one in the *other broker's* package)
**Symbol:** Dhan segment/instrument-kind taxonomy
**Blast Radius:** 5 files, 7 tables
**Impact:** MEDIUM
**Evidence:** `runtime/feed_integrity.py:38` uses raw strings `frozenset({"NSE","BSE","IDX"})` where `ExchangeId` exists.

### [SMELL-18] A — `q2` bypassed in `fees.py`: `ROUND_HALF_EVEN` vs `ROUND_HALF_UP` on the money path
**Files:** `execution/fees.py:119,174,254,255` (raw `quantize(Decimal("0.01"))`); `execution/fees.py:170` (uses `q2`); canonical `domain/utils.py:19`
**Symbol:** money quantization
**Blast Radius:** 2 files, 4 divergent sites
**Impact:** MEDIUM
**Evidence:** **Adjacent branches of the same `if/else` round differently** — L170 uses `q2` (capped), L174 uses raw `quantize` (uncapped). One paisa per fill; exactly the divergence class that breaks backtest/live parity, and the repo has a parity suite guarding that.

### [SMELL-19] A — Credentials: 3 backend read-sites + 2 frontend transports
**Files:** `interfaces/auth/deps.py:83,116,203`; `frontend/src/apikey.ts:41`; `frontend/src/feed.ts:497` (`?api_key=` query param)
**Symbol:** `X-API-Key`
**Blast Radius:** 2 files, 5 sites
**Impact:** MEDIUM
**Evidence:** A header rename is a 5-site edit with no compiler help on the Python↔TS seam. The query-param transport also puts a credential in URLs/logs.

### [SMELL-20] A — `TRADEX_UI_ORIGIN` default: CORS and CSRF can disagree
**Files:** `interfaces/fastapi_app.py:107`; `interfaces/auth/deps.py:153-156`; `frontend/src/feed.ts:16`
**Symbol:** `http://localhost:5173` vs `127.0.0.1`
**Blast Radius:** 3 files
**Impact:** MEDIUM
**Evidence:** `fastapi_app.py` ignores `app.state.ui_origin` entirely; `deps.py` honours it. If a caller sets `app.state.ui_origin` without the env var, **CORS and CSRF disagree about the allowed origin**.

### [SMELL-21] H — `BrokerAdapter` protocol returns `object` at 6 sites (abstraction leak)
**Files:** `domain/src/tradex_domain/protocols.py:102,106,110` (`stream_backend`/`market_stream_backend`/`depth_stream_backend`), `:119,127,135` (`subscribe_quotes`/`subscribe_depth`/`unsubscribe`)
**Symbol:** `-> object` on stream seams
**Blast Radius:** 6 method signatures, every broker adapter
**Impact:** MEDIUM
**Evidence:** `MarketStreamPort`/`DepthStreamPort` exist at L223/L236 but the `BrokerAdapter` methods that produce them are typed `object`, so callers must `hasattr`-probe. The `return object` is what makes a strategy reach into backend internals (Law of Demeter violation) legal to the type checker.

### [SMELL-22] B — 7 copies of `_candle()` test fixture + 3 copies of `_request()`
**Files:** `_candle()` ×7: `trading/tests/{datalake/test_analytics_datalake.py:37, replay/test_replay_backtest.py:41, analytics/test_engine_historical.py:30, analytics/test_engine_gaps.py:25, strategy/test_strategy_engine.py:39, strategy/test_strategy_protocols.py:33, strategy/test_strategy_scanner.py:49}`; ×5 more: `trading/tests/parity/{test_corporate_action_parity.py:32, test_replay_parity.py:32, test_recording_bridge_guard.py:43, test_golden_all_costs_parity.py:42}`, `trading/tests/replay/test_bridge_protective_levels.py:28`; `_request()` ×3 groups
**Symbol:** test fixture duplication
**Blast Radius:** 12+ test files
**Impact:** MEDIUM
**Evidence:** Structural-clone detector, ~52 AST nodes each. `_make_registry()` duplicated ×2, `_make_order()` ×2. A candle-shape change is a 12-file edit.

---

## Tier 3 — LOW impact / hygiene

| ID | Pattern | Finding | Blast |
|---|---|---|---|
| [SMELL-23] | A | HTTP timeout `30.0` as 11 bare literals: `brokers/common/transport.py:34,56,120`, `brokers/common/resilience.py:568,887`, `runtime/live.py:82,141,228,418,497,572`, `trading/scripts/e2e_smoke.py:51,77`. `live.resolve_fetch(30.0)` is the *composition root* — so `HttpTransport`'s default is **not** the live path; two independent knobs that agree today. | 4 files / 13 sites |
| [SMELL-24] | A | `DAY_SECONDS=86400`/`HOUR_SECONDS=3600` re-declared ×4: `analytics/profiles.py:17`, `analytics/ma_vol.py:41-42`, `analytics/studies/studies_complex.py:194-195`, `runtime/bar_aggregator.py:61` | 4 files |
| [SMELL-25] | A | `tick_size` default diverges **inside one file**: `profiles.py:330` signature `0.05` vs `profiles.py:666,672,679` `PROFILE_SPECS` `0.1`; TS `market-profile.ts:118` uses `0.05`. Signature default is unreachable via normal dispatch but is 2× wrong for equity (NSE equity tick = 0.01, index = 0.05 — *neither number is named for its instrument*). | 2 files |
| [SMELL-26] | A | `value_area_percent=0.7` + TPO `0.35`/`0.3` duplicated line-for-line Python↔TS: `profiles.py:126,173,295,314,334,666,672,683` vs `openalgo-charts/src/profile/market-profile.ts:122,420,441` and `volume-profile.ts:13,20` | 3 files |
| [SMELL-27] | A | `252` trading days vs `375` session minutes: see SMELL-15. Related: `_HOUR_SECONDS`-derived `4h` break threshold appears 3×. | merged above |
| [SMELL-28] | A | Equity fallback universe `("RELIANCE","TCS","INFY","HDFCBANK")` duplicated: `brokers/dhan/adapter.py:42`, `brokers/upstox/adapter.py:40`, `frontend/src/main.ts:21`, `frontend/src/tier2.ts:59,69`, `trading/scripts/seed_e2e_datalake.py:76`, **+7 strategy extensions** each binding `RELIANCE` | 12 files |
| [SMELL-29] | A | Frontend exchange allow-list: `main.ts:23` and `shellbar.ts:168` both declare `['NSE','NFO','BSE','BFO','MCX']` (a 5-subset of `ExchangeId`'s 9); `'NSE'` default repeated at `main.ts:21`, `tier2.ts:58,68` | 3 files |
| [SMELL-30] | A | Datetime formats: `%Y-%m-%d %H:%M:%S` at `dhan/_marketdata.py:230,240,241`, `backfill_parquet.py:66,67,174`; **wire contract `%Y%m%d`** spelled separately in `domain/wire.py:86` and `domain/value_objects.py:123,154`; `logging_config.py:58`; `cli.py:437,473`; a private 7-entry format ladder at `brokers/common/provider_common.py:184-190` | 6 files |
| [SMELL-31] | A | `max_mark_age_seconds=5.0` ×3: `config/schema.py:31`, `execution/risk.py:70,273` — gates **live order rejection**; config is the user knob, the two `risk.py` defaults are silent fallbacks | 2 files |
| [SMELL-32] | A | `stale_after=30.0`: `runtime/market_feed.py:95`; `runtime/feed_monitor.py:36-37` re-reads it as a `getattr` fallback and reuses the same number for an unrelated poll-interval cap | 2 files |
| [SMELL-33] | A | `OrderSide.BUY` compared as raw `"BUY"`: `domain/position_math.py:104,119`, `execution/fees.py:253`, `execution/cash_ledger.py:69,71`. A typo here **silently flips position direction** rather than raising | 4 files |
| [SMELL-34] | D | `strategy/src/tradex_strategy/amt/` — **empty, untracked, never committed** directory. Dead scaffold | 1 dir |
| [SMELL-35] | E | `poc/` (19 entries) contains live-looking modules incl. a parallel `duck_analytics.screener` CLI registry and `_sql_paths()` duplicated ×3 (`poc/chartink_screener.py:59`, `poc/timesfm_0950.py:64`, `poc/data/build_0950_dataset.py:51`) and sys.path shims to `services/duckdb-analytics/src` — a path that **does not exist** in this repo | 3+ files |

---

## Findings NOT present (verified absent — the codebase is better than average here)

- **No second `apply_fill`.** `test_import_boundaries.py:530-551` enforces it; only `domain/position_math.py:89` defines it. Position math is genuinely single-authority.
- **No duplicate order state machine.** The legacy `tradex_trading.events` stack (~2.7k LoC, *different* risk/FSM/kill-switch semantics) was retired; `test_retired_events_stack_is_not_imported` + `test_retired_events_stack_is_actually_gone` guard re-creation.
- **The money path is genuinely sound.** Single `apply_fill`. Single `FillModel`. The two brokerage accruals are disjoint by construction and were measured in agreement to 0.00 across 200 fills and a multi-order burst run (see the SMELL-01 retraction). The "zero-discrepancy" claim holds for the paths that were tested.
- **No foreign private writes** in `domain/`, `brokers/`, `trading/`.
- **Indicator registry works.** 102 indicators, one dispatch seam, decorator self-registration, idempotent on re-import. Adding an indicator is **3 files backend** (module + `__all__` import + golden `PARAM_MAP` entry) — and `test_golden_parity.py:377` *fails the build* if a registered indicator is missing from `PARAM_MAP` (239 entries). That gate is the best guardrail in the repo.
- **Scanner discovery works.** Add a screener = 3 files (definition + `__all__` line + shim). `frontend/src/screener.ts` populates its dropdown from `GET /api/charts/strategies` — the frontend needs **zero** changes.
- **Calendar consolidation is the model to copy.** `domain/market_calendar.py` centralized `09:15`/`15:30` and its docstring names the duplication it removed. SMELL-14 is the straggler set.

---

# PHASE 3 — Root Cause Classification

| # | Root cause | Findings | Why |
|---|---|---|---|
| **RC-1** | **Missing shared vocabulary layer** — constants exist centrally but call sites re-hardcode them | SMELL-09, 10, 11, 12, 14, 15, 16, 17, 18, 23, 24, 25, 26, 28, 29, 30, 31, 32, 33 | The dominant root cause (19 of 35). Not a *missing* layer — a **partially adopted** one. `market_calendar.py` and `timeframe.py` prove the pattern works; ~19 call sites never adopted it. This is textbook shotgun surgery: one logical change (session length) = 21 edits. |
| **RC-2** | **Missing service/use-case layer** — business logic leaking into I/O adapters | SMELL-13, 18, 20 | `routes/orders.py` accepts-then-drops `product`; `fees.py` re-derives rounding; `fastapi_app`/`deps.py` each resolve origin policy independently. *(The original 4th member, SMELL-01, was retracted — see above.)* |
| **RC-3** | **Missing domain model** — raw dicts/primitives where typed entities exist | SMELL-13, 17, 21, 29, 33 | `OrderType`/`ProductType`/`ExchangeId` exist in `domain` but the browser boundary uses free-form strings; `SEGMENT_EXCHANGE` is an untyped int table; `BrokerAdapter` returns `object`; `feed_integrity` uses raw exchange strings. |
| **RC-4** | **Boundary violations** — modules importing across layer lines | SMELL-05, 21, 07, 06 | 4 import cycles (2 through the datalake core); `runtime.session` reaches into the legacy monolith; 15 scripts reach through the shim into `market_data`; `trading/` re-imports everything. The boundary test checks *edges* but not *cycles* — a structural blind spot. |
| **RC-5** | **Premature file splitting / duplication** — one concept split without a unifying interface | SMELL-03, 04, 06, 22, 01(residual) | `_ensure_ws` ×3 verbatim in 2 files; 5 WS backends × 8 lifecycle methods each; 2 brokers × 6 mirrored mixins; 155 shims + 274 orphaned tests; 12 duplicated test fixtures; the benign double fee computation. |
| **RC-6** | **Absent/inconsistent coding standards** | SMELL-02, 13, 18, 25, 33, 21, 23 | Two different RSI algorithms; two rounding modes adjacent in one `if/else`; two `order_type` defaults in one file; a `tick_size` default that disagrees with its own spec table; `enum` bypassed with raw strings on the signed-quantity path; 7 of 10 strategies roll their own indicators. |
| **RC-7** *(new)* | **Enforcement asymmetry** — strong guards in some places, none in others | SMELL-05, 22, 06 | `test_import_boundaries.py` (587 L) is genuinely strong but: (a) checks edges not cycles, (b) `test_no_foreign_private_writes.py` scans only 3 of 17 src dirs (`domain`, `brokers`, `trading` — **not** `execution`, `analytics`, `strategy`, `runtime`, `interfaces`, `market_data`, `replay`). CI runs `--cov` for 9 of 17 packages and marks **7 of 10 mypy jobs `continue-on-error`**. The unguarded layers are exactly where the structural findings live. |

---

# PHASE 4 — Refactoring Plan

Dependency-ordered. Foundational extractions first. **All tasks are sequenced so no task breaks a passing test.**

## Wave 0 — Stop the bleeding (no behaviour change, 1 PR, ~1 day)

### REF-1 · Fix the live broken scripts
**Root Cause:** RC-2 (boundary violation in ops layer)
**Action:** Enforce boundary — make the contract failure loud, then fix the callers
**From:** `trading/scripts/backfill_2025.py`, `trading/scripts/backfill_parquet.py`
**To:** same files; `parallel_fetcher.py` gains a typed `BrokerSet`
**Touches:** 2 scripts, 1 test file (`trading/tests/datalake/test_parallel_fetcher.py`)
**Test Strategy:** Add the missing both-brokers test that would have caught this. Integration test per script.
**Sequencing:** none — do first, it is a live defect.
**Trace:** SMELL-08

### REF-2 · Close the enforcement gaps
**Root Cause:** RC-7
**Action:** Extend — add cycle detection to the boundary test; widen private-write scan to all 17 src dirs
**From:** `tests/test_import_boundaries.py`, `tests/test_no_foreign_private_writes.py`
**To:** same
**Touches:** 2 test files
**Test Strategy:** Both must **fail** on the current tree (proving the gaps are real), then be allowlisted for known findings before landing green.
**Sequencing:** **must precede every other task** — it is the regression net for Waves 1-3.
**Trace:** RC-7, SMELL-05, SMELL-22

### REF-3 · Delete dead scaffold
**Root Cause:** RC-5
**Action:** Delete
**From:** `strategy/src/tradex_strategy/amt/`, `poc/` sys.path shims to the non-existent `services/duckdb-analytics/src`
**To:** —
**Touches:** 1 dir + 3 poc files
**Test Strategy:** `pytest --collect-only` must show no change (nothing imports them).
**Sequencing:** none
**Trace:** SMELL-34, SMELL-35

## Wave 1 — Foundational: close the vocabulary layer (RC-1)

> These are mechanical, low-risk, and unblock everything in Wave 3.

### REF-4 · Adopt `domain/timeframe.py` everywhere
**Root Cause:** RC-1
**Action:** Delete duplication — replace 3 private maps with `bucket_seconds()`
**From:** `runtime/bar_aggregator.py:61,71-76`, `runtime/feed_recovery.py:39-44,143`, `runtime/feed_integrity.py:43-48`
**To:** `domain/timeframe.py` (already canonical)
**Touches:** 3 files + `runtime/tests/`
**Test Strategy:** Unit-test `W1 → 604800` through the runtime path (currently `ValueError`). Type check `runtime`.
**Sequencing:** none
**Trace:** SMELL-10

### REF-5 · Adopt `domain/market_calendar.py` everywhere
**Root Cause:** RC-1
**Action:** Delete duplication
**From:** `runtime/feed_integrity.py:120`, `trading/scripts/seed_e2e_datalake.py:73`, `trading/scripts/datalake_stats.py:107,109,111,284,286`
**To:** `domain/market_calendar.py` (`MARKET_OPEN/CLOSE`, `MARKET_OPEN_STR/CLOSE_STR`, `to_ist_naive`)
**Touches:** 4 files (SQL literals become f-string interpolation from the `_STR` constants)
**Test Strategy:** Unit test asserting the generated SQL matches the current literal output byte-for-byte, then snapshot the stats report.
**Sequencing:** after REF-4 (same wave, independent)
**Trace:** SMELL-14

### REF-6 · Single IST identity
**Root Cause:** RC-1, RC-3
**Action:** Introduce abstraction + Delete duplication
**From:** 13 Python sites (`interfaces/routes/stream.py` ×6, `chart.py`, `stream_indicators.py` ×2, `replay_run.py`, `brokers/dhan/tick_parser.py`, `analytics/profiles.py`, `analytics/seasonality.py`)
**To:** `domain/market_calendar.py` `IST` + a new `domain/timezones.py` exposing `IST_ZONE` (`ZoneInfo`) and `IST_OFFSET_SECONDS`
**Touches:** 8 files
**Test Strategy:** Assert every route emits identical timestamps before/after. Type check `interfaces` (first to be non-`continue-on-error`).
**Sequencing:** after REF-5
**Trace:** SMELL-09

### REF-7 · Unify the two rounding modes on the money path
**Root Cause:** RC-6, RC-2
**Action:** Enforce standard — all money quantizes through `q2`
**From:** `execution/fees.py:119,174,254,255`
**To:** `domain/utils.py:q2`
**Touches:** 1 file + `trading/tests/parity/test_execution_cost_parity.py`, `test_golden_all_costs_parity.py`
**Test Strategy:** **Parity suite is the test.** Add a half-paisa boundary test proving capped and uncapped branches now agree. *Flag: a divergence here means historical backtest numbers shift by one paisa — announce, don't silently fix.*
**Sequencing:** before REF-9 (both touch the fee path)
**Trace:** SMELL-18

### REF-8 · Deduplicate the fee computation (was: "fix the dual ledger")
**Root Cause:** RC-5 (premature duplication) — **NOT a correctness bug**, see SMELL-01 retraction
**Action:** Extract — compute the fee once, fan out to both books
**From:** `replay/backtest.py:278,320-325` (second `calculate_capped` call)
**To:** `execution/brokerage.py` — a `BrokerageAccrual` object that both the engine and the backtest hold a reference to, so one `calculate_capped` result feeds both `position_manager.on_fee` and the backtest `CashLedger`
**Touches:** `replay/backtest.py`, `execution/engine.py`, new `execution/brokerage.py`
**Test Strategy:** (a) **parity suite must show ZERO golden P&L changes** — this is the acceptance criterion; (b) assert `calculate_capped` is called exactly once per fill (currently twice); (c) assert position-book fees == cash-ledger fees (currently true, must stay true).
**Sequencing:** after REF-7. **Low priority** — it is a performance/robustness cleanup.
**Trace:** SMELL-01 (retracted)

### REF-9 · Single indicator authority for strategies
**Root Cause:** RC-6, RC-5
**Action:** Delete duplication
**From:** `strategy/extensions/strategies/{mean_reversion.py:121-138, multi_symbol_sma_cross.py:117, sma_cross.py, bracket_breakout.py}`
**To:** `analytics/indicators.py` (`rsi`, `sma`, `atr`) — `strategy` already declares `tradex-analytics` as a dependency
**Touches:** 4 strategy files + their tests
**Test Strategy:** Golden strategy-output tests side-by-side. **⚠ Domain-knowledge flag: this changes `mean_reversion` signal values** (simple → Wilder RSI). Requires a quant decision: pick one RSI, state it in an ADR, regenerate goldens. Do **not** auto-apply.
**Sequencing:** after REF-8
**Trace:** SMELL-02

## Wave 2 — Contract generation for the language boundary (RC-3)

### REF-10 · Generate a typed wire contract (TS ↔ Python)
**Root Cause:** RC-3 — **unblocks the entire frontend**
**Action:** Introduce abstraction — single generated source of truth
**From:** hand-written `frontend/src/order-safety.ts:1-4`, `chart-lifecycle.ts:204`, `shellbar.ts:323`, `main.ts:21-23`, `apikey.ts:41`; `interfaces/routes/orders.py:146,207,444`; `openalgo-charts/src/feed/time.ts:10`, `profile/market-profile.ts:69`
**To:** new `contracts/wire_contract.json` → generates `frontend/src/generated/wire.ts` + `domain/src/tradex_domain/generated/wire.py` (Pydantic models + TS types)
**Test Strategy:** (a) CI check that generated files are in sync with the source-of-truth (regenerate + `git diff --exit-code`); (b) a test that `product` is **not** silently dropped; (c) golden indicator-catalogue snapshot.
**Sequencing:** before REF-11, REF-12
**Trace:** SMELL-13, 09, 19, 25, 26, 28, 29, 30

### REF-11 · Honour or remove the `product` field
**Root Cause:** RC-2
**Action:** Fix boundary — the field crosses the wire and is discarded
**From:** `interfaces/routes/orders.py:_build_request`
**To:** same, reading the generated `ProductType`
**Touches:** `routes/orders.py`, `contracts/wire_contract.json`, `frontend/src/chart-lifecycle.ts`
**Test Strategy:** Order-submission test asserting the broker receives the selected product. **Decision required: honour it, or delete it from the UI. Both are acceptable; silently dropping it is not.**
**Sequencing:** after REF-10
**Trace:** SMELL-13

### REF-12 · One credential transport
**Root Cause:** RC-6
**Action:** Enforce standard — one canonical header
**From:** `interfaces/auth/deps.py:83,116,203`; `frontend/feed.ts:497` (query param)
**To:** generated `X-API-Key` constant
**Touches:** 2 files
**Test Strategy:** Auth contract test. **Flag: removing the `?api_key=` transport may break external WS clients — deprecate with a warning, don't hard-remove.**
**Sequencing:** after REF-10
**Trace:** SMELL-19

### REF-13 · Single CORS/CSRF origin policy
**Root Cause:** RC-2
**Action:** Introduce abstraction — one resolver
**From:** `interfaces/fastapi_app.py:107`, `interfaces/auth/deps.py:153-156`
**To:** `interfaces/auth/origin.py:resolve_ui_origin(app_state)`
**Touches:** 2 files + `trading/tests/interface/`
**Test Strategy:** Test the divergent case directly: set `app.state.ui_origin` **without** the env var; assert CORS and CSRF agree. Currently they don't.
**Sequencing:** after REF-10
**Trace:** SMELL-20

## Wave 3 — Structural consolidation (RC-4, RC-5)

### REF-14 · Break the import cycles
**Root Cause:** RC-4
**Action:** Enforce boundary — extract the shared kernel
**From:** `market_data/backtest_loader.py:38` (`tradex_runtime.calendar`), `:41,207` (`tradex_replay.backtest`)
**To:** `domain/market_calendar.py` already has the calendar — `backtest_loader` must not import `runtime`. For the `replay` type: move `BacktestEngine`/`BacktestResult` behind a `domain`-level `BacktestPort` Protocol, or invert so `market_data` exposes a loader *interface* that `replay` implements.
**Touches:** `market_data/backtest_loader.py`, `replay/backtest.py`, `market_data/pyproject.toml` (drop `tradex_replay`, `tradex_runtime`), `replay/pyproject.toml`
**Test Strategy:** REF-2's new cycle test must go green. Full suite. Type check.
**Sequencing:** after Wave 1 (W1 constants reduce what crosses these edges)
**Trace:** SMELL-05, RC-7

### REF-15 · Fix manifest/import drift
**Root Cause:** RC-4
**Action:** Enforce boundary — make packaging enforce the graph
**From:** `market_data/pyproject.toml`, `interfaces/pyproject.toml`, `runtime/pyproject.toml`, plus 5 declared-but-unused
**To:** add missing deps; remove `httpx`/`websockets`/`rx`/`protobuf` where unused (verify `rx` — likely used via re-export)
**Touches:** 3-4 pyproject files
**Test Strategy:** New test asserting every `import tradex_*` in a package is declared in its `pyproject.toml`, and vice-versa.
**Sequencing:** after REF-14 (the dependency fix depends on the final edge set)
**Trace:** §1.3

### REF-16 · Extract the WS lifecycle template
**Root Cause:** RC-5
**Action:** Introduce abstraction
**From:** `brokers/dhan/ws_streams.py:334,633` (duplicate `_ensure_ws`), `upstox/ws_streams.py:475`; the 5 backend classes' `_open_socket`/`_resubscribe`/`_receive_loop`/`close` skeletons
**To:** `brokers/common/ws_lifecycle.py` — extend the existing `AutoReconnectMixin` (`ws_reconnect.py:124`) into a full `StreamLifecycle` base implementing the connect/reconnect/resubscribe/close skeleton; backends supply only `_open_socket`, `_resubscribe`, `_decode_frame`
**Touches:** `brokers/common/ws_reconnect.py` (or new file), `brokers/dhan/ws_streams.py`, `brokers/upstox/ws_streams.py`, `brokers/tests/`
**Test Strategy:** (a) clone-detector assertion: `_ensure_ws` structural signature appears exactly once; (b) `brokers/tests/common/test_ws_reconnect_hook.py` + both adapters' reconnect tests; (c) side-by-side diff of reconnect attempt sequences per broker.
**Sequencing:** after REF-15
**Trace:** SMELL-03

### REF-17 · Templatize the broker adapter contract
**Root Cause:** RC-5
**Action:** Introduce abstraction + Delete duplication
**From:** `brokers/{dhan,upstox}/_orders.py`, `_portfolio.py`, `_marketdata.py`, `master.py`
**To:** `brokers/common/` — a declarative field-mapping base; each broker supplies a mapping table, not a re-implemented method body
**Touches:** 12 broker files, 2,968 LOC production + ~2,500 LOC test
**Test Strategy:** (a) parametrize the 18 mirrored test functions into one conformance suite run against both brokers — **this alone removes ~2,000 test LOC**; (b) `trading/tests/contracts/test_adapter_protocol.py:292` must pass unchanged; (c) diff wire payloads for both brokers before/after.
**Sequencing:** after REF-16
**Trace:** SMELL-04

### REF-18 · Extract a shared script bootstrap
**Root Cause:** RC-4
**Action:** Introduce abstraction
**From:** 14 copies of the `sys.path` bootstrap in `trading/scripts/`, 6 `--data-root` parsers, 4 `ohlcv` root rules
**To:** `scripts/_bootstrap.py` (or a console-script entry point) exposing `add_repo_to_path()`, `resolve_datalake_root(args)`, `ohlcv_root(base)`
**Touches:** 14 scripts
**Test Strategy:** Each script still runs (`--help` smoke). Add the missing `market_data/src` path entry.
**Sequencing:** after REF-15
**Trace:** SMELL-07

### REF-19 · Strangle `trading/` — tests first
**Root Cause:** RC-5
**Action:** Move (do not delete)
**From:** `trading/tests/{analytics,datalake,execution,strategy,reactive,replay,research,runtime,application,interface,scripts}` (~230 files)
**To:** the owning package's `tests/` — the packages already declare `testpaths`, and 9 of them have a 1-file stub waiting to be filled
**From/To:** `trading/tests/parity/` and `trading/tests/contracts/` → **`tests/parity/` and `tests/contracts/` at repo root** (they are cross-package by nature; they test the composition, not one package)
**Touches:** ~274 test files + 10 `pyproject.toml` `testpaths` + `.github/workflows/parity.yml`
**Test Strategy:** **CI must stay green at every commit** — move one directory per commit, never batch. Run the full suite after each.
**Sequencing:** after Wave 1-2 (green tests are the safety net)
**Trace:** SMELL-06

### REF-20 · Delete the shim layer
**Root Cause:** RC-5
**Action:** Delete (only after REF-19 is fully green and all callers migrated)
**From:** 155 shim files in `trading/src/tradex_trading/`
**To:** — (callers import `tradex_*` directly)
**Touches:** 155 files + every importer
**Test Strategy:** Delete one subtree per commit. `test_import_boundaries.py` already exempts `trading` from the allowlist — **tighten that exemption as each subtree goes.**
**Sequencing:** **after REF-19 and REF-21.** Do not start before.
**Trace:** SMELL-06

### REF-21 · Extract `TradingSession` out of the legacy monolith
**Root Cause:** RC-4
**Action:** Move
**From:** `trading/src/tradex_trading/sdk/session.py` (497 L), `sdk/live_fill_bridge.py` (283 L)
**To:** `runtime/session.py` (currently a 9-line re-export façade) and `execution/live_fill_bridge.py`
**Touches:** 2 files moved, `runtime/session.py` rewritten, `runtime/pyproject.toml` (drop `tradex_trading`), the 2-module allowlist at `test_import_boundaries.py:390-393`
**Test Strategy:** `trading/tests/contracts/test_session_smoke.py:253` + the live-fill bridge tests. This **closes the last `runtime → trading` edge** and makes the `_RUNTIME_TRADEX_TRADING_ALLOWLIST` deletable.
**Sequencing:** before REF-20 (these are the only 2 real files left in `trading/`)
**Trace:** SMELL-05, SMELL-06

### REF-22 · Tighten `BrokerAdapter` protocol types
**Root Cause:** RC-3
**Action:** Enforce standard
**From:** `domain/protocols.py:102,106,110,119,127,135` (`-> object`)
**To:** `MarketStreamPort` / `DepthStreamPort` (already exist at L223/L236)
**Touches:** 1 file + every broker adapter + callers currently doing `hasattr`-probes
**Test Strategy:** Type check `brokers` + `domain` (both already non-`continue-on-error`). Runtime `isinstance` conformance test.
**Sequencing:** after REF-16 (the lifecycle base makes the real type nameable)
**Trace:** SMELL-21

### REF-23 · Promote mypy to blocking, package by package
**Root Cause:** RC-7
**Action:** Enforce standard
**From:** `.github/workflows/quality-gate.yml` — 7 of 10 mypy jobs are `continue-on-error`
**To:** remove `continue-on-error` in REF order: `market_data`, `replay`, `persistence`, `research`, `observability`, `operations`, `application` first (smallest), then the 7 existing
**Touches:** 1 workflow file + typing fixes per package
**Test Strategy:** CI.
**Sequencing:** **last.** Do this once the tree is quiet, or you'll be fighting two problems at once.
**Trace:** RC-7

### REF-24 · Extend coverage to all 17 packages
**Root Cause:** RC-7
**Action:** Extend
**From:** `.github/workflows/quality-gate.yml` — `--cov` for 9 of 17
**To:** all 17
**Touches:** 1 workflow file
**Test Strategy:** CI. Consider a coverage ratchet so it can only go up.
**Sequencing:** with REF-23
**Trace:** RC-7

---

## Sequencing summary

```
Wave 0  REF-1 (live bug)  → REF-2 (enforcement net) → REF-3 (dead code)
                              ↑ MUST be first; it's the regression net
Wave 1  REF-4 → REF-5 → REF-6      (vocabulary: timeframe, calendar, IST)
        REF-7 (rounding) → REF-8 (dual ledger) → REF-9 (indicator authority)
Wave 2  REF-10 (generated contract) → REF-11, REF-12, REF-13
Wave 3  REF-14 (cycles) → REF-15 (manifests)
        REF-16 (WS lifecycle) → REF-17 (adapter contract)
        REF-18 (script bootstrap)
        REF-19 (strangle tests) → REF-21 (extract session) → REF-20 (delete shims)
        REF-22 (protocol types)
        REF-23 + REF-24 (mypy + coverage) — LAST
```

**Critical path to the one remaining Tier-1 correctness defect (SMELL-08, the broken scripts):** REF-1.
**Highest-value structural work:** REF-2 (enforcement net) → REF-14 (cycles) → REF-16 (WS lifecycle) → REF-17 (adapter contract) → REF-19/20 (delete the shim layer).
**Note:** there is **no** confirmed money-path correctness defect. The money path (single `apply_fill`, single `FillModel`, matching fee ledgers) is sound.

---

# PHASE 5 — Structural Recommendations

## 5.1 Proposed target structure

```
v4/
├── contracts/                        # NEW: single source of truth for the wire
│   └── wire_contract.json            #   enums, product/order types, header names
│                                       #   GENERATES both TS and Python — see REF-10
├── domain/                           # LAYER 0 — kernel. Zero deps. Never grows an I/O import.
│   └── src/tradex_domain/
│       ├── enums.py                  # ExchangeId, OrderType, ProductType, Timeframe
│       ├── value_objects.py          # Money, Price, Quantity — Decimal-only
│       ├── instruments.py            # Equity, Future, Option + InstrumentId
│       ├── market_calendar.py        # MARKET_OPEN/CLOSE, IST, holidays  ← RC-1 anchor
│       ├── timezones.py              # NEW: IST, IST_ZONE, IST_OFFSET_SECONDS
│       ├── timeframe.py              # canonical timeframe→seconds/broker-interval registry
│       ├── position_math.py          # apply_fill — SOLE money authority
│       ├── fees_spec.py              # NEW: statutory rate constants (moved from execution/fees.py)
│       ├── protocols.py              # BrokerAdapter, MarketStreamPort, Clock  — fully typed
│       ├── events.py / wire.py / serialization.py / errors.py / options.py
│       └── generated/                # GENERATED from contracts/ — never hand-edited
│
├── brokers/                          # LAYER 1 — provider adapters. → domain only.
│   ├── common/                       # SHARED broker machinery, one copy each
│   │   ├── ws_lifecycle.py           # NEW: StreamLifecycle base — replaces 3× _ensure_ws
│   │   ├── ws_reconnect.py           # backoff (already good)
│   │   ├── provider_common.py        # date formats, unwrap, response ladders
│   │   ├── token_lifecycle.py, resilience.py, transport.py, paths.py, rate_table helpers
│   ├── dhan/                         # declarative mapping tables, NOT method bodies
│   ├── upstox/
│   └── paper/
│
├── analytics/                        # LAYER 1 — pure computation. → domain only.
│   ├── registry.py                   # IndicatorRegistry — the single dispatch seam
│   ├── indicators.py                 # canonical sma/rsi/atr — strategies import THESE
│   └── oscillators/ volatility/ volume/ studies/
│
├── market_data/                      # LAYER 1 — datalake. → domain only. NO replay/runtime.
├── execution/                        # LAYER 2 — OMS. → domain, reactive, observability.
│   ├── position_accountant.py        # atomic fill+remark seam
│   ├── fill_model.py                 # cross-mode fill resolution
│   ├── fees.py                       # uses domain.fees_spec rates + q2 everywhere
│   ├── brokerage.py                  # NEW: BrokerageAccrual — the ONE accrual owner
│   └── engine.py                     # the only _brokerage_accrued
│
├── strategy/  replay/  reactive/  research/  application/  observability/  config/
│
├── runtime/                          # LAYER 3 — composition. Owns boot + session.
│   ├── startup.py, live.py, market_feed.py, bar_aggregator.py
│   ├── session.py                    # REAL TradingSession after REF-21, not a façade
│   └── supervisor/                   # NEW: feed_* (5 modules, 1,237 LOC → 1 supervisor)
│
├── interfaces/                       # LAYER 3 — HTTP/WS/CLI. Only layer importing fastapi.
├── persistence/                      # LAYER 2 — SQLite stores. → execution.
│
├── tests/                            # cross-cutting: parity/, contracts/, guards/
├── frontend/src/generated/           # GENERATED from contracts/
├── scripts/_bootstrap.py             # NEW: one sys.path + root resolver for 14 scripts
└── trading/                          # DELETED by REF-20. `sdk/` goes to runtime/ + execution/.
```

**Net effect:** 17 packages → 15 (delete `trading`, fold `persistence` into `execution` if its facade stays 19 lines); 429 shim/orphan files eliminated; 4 import cycles → 0.

## 5.2 Boundary rules

```
LAYER 0  domain          imports NOTHING internal. No fastapi, no pandas, no pyarrow,
                         no broker SDK, no sqlite3. If domain needs it, it is not a
                         domain concept.
LAYER 1  analytics       → domain
        market_data      → domain                              (NOT replay, NOT runtime)
        brokers          → domain
        config           → domain
        reactive         → domain
        research         → domain
LAYER 2  execution       → domain, reactive, observability     (NOT fastapi, NOT replay)
        persistence     → execution
LAYER 3  strategy       → domain, analytics, market_data
        replay          → domain, execution, strategy, analytics, market_data, reactive
        application     → domain, execution
        runtime         → all below it, + nothing above it     (NOT tradex_trading)
        interfaces      → all below it                          (NOT fastapi elsewhere)
```

**Named rules (each maps to a REF task):**
1. **`domain/` must never import from any other `tradex_*` package.** *(enforced today — keep)*
2. **No package may import `tradex_trading`.** The legacy tree is a composition root, not a library. *(REF-21 closes the last violation)*
3. **The import graph must be acyclic.** Edges alone are insufficient — REF-2 adds cycle detection.
4. **`interfaces/` is the only package that may import `fastapi`.** *(enforced for `execution` today — extend to all 15)*
5. **`market_data/` must not import `replay` or `runtime`.** Datapaths are dependencies; engines are not. *(REF-14)*
6. **`strategy/` computes indicators by importing `tradex_analytics`, never by defining its own.** *(REF-9)*
7. **Every `import tradex_*` must be declared in that package's `pyproject.toml`, and vice-versa.** *(REF-15)*
8. **One accrual owner, one money path, one fill authority.** Enforced by test, not convention. *(REF-8)*
9. **Strategies reach brokers only through `SessionFacade` / `BrokerAdapter`.** Typed return values, never `object`. *(REF-22)*
10. **Anything crossing the Python↔TS boundary is generated from `contracts/wire_contract.json`.** Hand-written enums on both sides are forbidden. *(REF-10)*

## 5.3 Coding standards to enforce

Each is checkable and traces to a finding:

1. **Money is `Decimal` and quantizes only through `q2`.** `domain/utils.py:19`. No bare `quantize(Decimal("0.01"))` anywhere. → *SMELL-18, REF-7*
2. **Enum members over raw strings for domain concepts.** No `fill.side.value == "BUY"` — use `fill.side is OrderSide.BUY`. Raw strings are permitted **only** when parsing untrusted wire data. → *SMELL-33*
3. **No literal that `domain/` already defines.** `MARKET_OPEN`, `MARKET_CLOSE`, `IST`, `bucket_seconds()`, `Timeframe.*` seconds, `q2`, `ExchangeId`. A lint rule must reject a numeric literal that duplicates a `domain` constant. → *SMELL-09, 10, 14, 24*
4. **One algorithm, one name, one home.** If two modules both compute RSI/ATR/SMA/session-breaks, the second is deleted. → *SMELL-02, 16*
5. **The backend owns state-directory and datalake-root resolution** — `datalake_root()` / `default_runtime_dir()`. Bare relative path literals (`"data/"`, `".tradex_v4"`) are banned; the config layer must call the same function the broker layer does. → *SMELL-11, 12*
6. **Money-path invariants are test-enforced, not documented.** One accrual owner, one `apply_fill`, one `FillModel`, one rounding mode — each has a named guard test that fails if a second implementation appears. → *SMELL-01, 18*
7. **A new cross-cutting constant requires an ADR.** Session length, trading-day count, tick size, value-area %, fee rates, the 4h/36h session-break heuristic. Not "constants" — *values whose tuning changes results and cannot be inferred from code*. → *SMELL-15, 25, 26*
8. **Test fixtures live in one shared conftest per package.** No 7th copy of `_candle()`. → *SMELL-22*

## 5.4 Guardrails to prevent recurrence

| Guardrail | Implementation | Prevents |
|---|---|---|
| **Manifest/import symmetry test** | AST-parse every `import tradex_*`; assert set equality with `pyproject.toml` `dependencies`. ~40 lines. | §1.3 drift, RC-4 |
| **Import-cycle test** | DFS over the package graph asserting acyclicity. Extend `test_import_boundaries.py`. | SMELL-05 |
| **Widen the private-write guard** | `_SRC_DIRS` currently covers 3 of 17 packages. Add all 17. ~3 lines. | RC-7 |
| **No-constant-redefinition lint** | Custom `ruff` rule: flag a module-level numeric/string literal in `*/src` that textually matches a `domain` constant's value. | SMELL-09, 10, 14, 24, 31, 32 |
| **Clone detector in CI** | The AST clone script used in this audit, run as a test with a hardcoded allowed-clone count. Today's baseline: 58 groups, mostly test fixtures. | SMELL-03, 22 |
| **Cross-language parity gate** | Compare `contracts/wire_contract.json` against both generated outputs; regenerate + `git diff --exit-code` in CI. | SMELL-13, 19, 25, 26, 29 |
| **Money-path authority guard** | Already exists at `test_import_boundaries.py:530-551` for `apply_fill`. **Extend** to `FeeCalculator`, `FillModel`, `BrokerageAccrual`. | SMELL-01, 18 |
| **Indicator-strategy guard** | Forbid `def _rsi|_sma|_ema|_atr|_macd|_bollinger` in `strategy/`. | SMELL-02 |
| **mypy blocking, ratcheted** | Remove `continue-on-error` package by package (REF-23). | RC-7 |
| **Coverage ratchet across all 17** | Currently 9 of 17 (REF-24). Never decrease. | RC-7 |
| **Pre-commit** | ruff, mypy (changed files), the manifest-symmetry test, the constant-lint. | All — fast feedback before CI |
| **ADR template** | `docs/adr/NNNN-*.md` with: context, decision, blast-radius analysis, files-touched list, reversibility. Required when a change touches **>3 packages** or alters a **money-path or result-affecting value**. | RC-6, the actual recurrence mechanism |
| **Touch-point budget as a test** | Assert "adding a screener touches ≤3 files" and "adding an indicator touches ≤3 backend files". These are *architectural invariants* and should be executable. | SMELL-06, RC-5 |

---

# Appendix A — Verification Evidence

**Ground truth established:** 3,856 tests collect clean; 155 parity tests pass; `tests/test_import_boundaries.py` currently passes.

**Independently re-verified by the coordinator** (not just reported by sub-agents):
- Import cycles — DFS over the real AST graph: 4 cycles, each traced to specific `file:line`
- Undeclared internal deps — manifest-vs-AST diff: 4 packages, 6 entries
- Clone detection — AST structural hashing: 58 groups; `_ensure_ws` ×3 read and diffed line-by-line
- RSI divergence — both implementations read and compared line-by-line
- `trading/` shim census — 155 of 159 files under 25 lines; 2 files hold all real logic
- SMELL-08 (broken scripts) — `ValueError` reproduced by execution

**One finding RETRACTED after re-verification:** SMELL-01 (dual brokerage accrual). The sub-agent reported "8 calls for 4 fills" and a 60.14-vs-41.87 divergence. Re-deriving it myself: 400 calls for 200 fills (real double-computation), but **divergence 0.00** — the two ledgers are disjoint by construction, the duplication is a documented deliberate fix for per-order brokerage caps, and the two calls are deterministic pure functions of the same inputs. The original claim was wrong. This is the single most important lesson of the audit: *a specific, plausible, experimentally-backed number is not the same as a verified claim.*

**Domain-knowledge flags (not guessed):**
- `mean_reversion`'s non-Wilder RSI may be *intentional*. Requires a quant decision, not an auto-fix. (REF-9)
- `tick_size` `0.05` vs `0.1` — NSE index tick is 0.05, equity 0.01. Neither default is named for its instrument. (SMELL-25)
- `fees.py` rounding divergence may shift historical backtest numbers by one paisa. Announce, don't silently fix. (REF-7)
- `NSE_HOLIDAYS_2026` is a 6-date frozenset with sources in comments — verify against the current NSE circular before extending. Do not guess.
- Removing the `?api_key=` WS transport may break external clients. Deprecate, don't hard-remove. (REF-12)

**Not claimed:** I did not verify the correctness of trading strategy logic, fee statutory rates, or broker protocol behaviour against Dhan/Upstox documentation. Those need domain review.
