# TradeX v4 — Pre-Deployment System Review (Corrected)

**Date:** 2026-10-01 (corrected 2026-07-31)
**Reviewer:** Principal Engineer
**Scope:** Full system architecture, dead code elimination, deployment readiness

> **Correction notice.** An earlier revision of this review cited line counts
> that were wrong by 10–100x and named several "duplicate implementations" that
> do not exist. Every figure below was re-measured from the tree. See
> §10 for what was wrong and why it mattered.

---

## Executive Summary

TradeX v4 is a **106,836-line Python codebase** (source plus tests) with a
**6,208-test suite that passes**. The architecture is already consolidated: the
data, feed, and order layers each have a single canonical owner, joined by
explicit seams (`MarketSource`, `BarSource`, event sourcing).

**The earlier NO-GO decision rested on a false premise.** It found "at least 3
complete generations" of the data, feed, and order subsystems. Those generations
do not exist. The commits that would have produced them
(`6faf008` four-wave architectural cleanup, `ab0a508` `BarSource.SIM`,
`5f96c48` indicator source filter) landed *before* the review was written, and
the review measured files that those commits had already replaced.

**Recommendation: GO**, after the four items in §8 are closed. None of them is
architectural.

---

## 1. System Intent

TradeX v4 is an algorithmic trading platform for Indian equities (NSE):

1. **Market data** — fetch, store, serve OHLCV via a Parquet datalake
2. **Strategy execution** — backtest and live-trade with protective orders
3. **Real-time streaming** — WebSocket feeds for quotes, indicators, orders
4. **Analytics** — indicators, seasonality, volatility, trade metrics
5. **Replay & simulation** — tick-replay for strategy validation

**Active execution paths:**
- REST API (`/api/charts/*`, `/api/orders/*`, `/ws/stream`) → datalake → broker fallback
- CLI (`tradex`) → same backend packages
- Datalake maintenance scripts (backfill, gap repair, reconciliation)

---

## 2. Module Inventory (Measured)

Line counts measured with `wc -l` on 2026-07-31, not estimated.

| Package | Responsibility | Status |
|---------|---------------|--------|
| `domain` | Pure types, instruments, calendar | ✅ Active |
| `brokers` | Dhan/Upstox/Paper adapters | ✅ Active |
| `config` | Env loading, schema | ✅ Active |
| `market_data` | Parquet storage, fetchers, loaders | ✅ Consolidated |
| `analytics` | Indicators, studies, reports | ✅ Active |
| `runtime` | Session, feed, replay, bar aggregation | ✅ Consolidated |
| `execution` | Order engine, reconciliation, stores | ✅ Consolidated |
| `replay` | Backtest engine, validation | ✅ Active |
| `strategy` | Strategy registry, extensions | ✅ Active |
| `interfaces` | FastAPI routes, WebSocket, CLI | ✅ Active |
| `application` | Use-case layer | ✅ Active |
| `observability` | Metrics, tracing | ✅ Active |
| `reactive` | Event bus | ✅ Active |
| `shared` | Rate limiting, market builder | ✅ Active |
| `research` | PIT universe, manifests | ✅ Active |

---

## 3. Architecture: What Is Actually Consolidated

### 3.1 Data Layer — one read path, one write path

```
Read:   Callers → MarketWindowLoader → ParquetMarketProvider → ParquetStorage
Write:  Backfill scripts → simple_sync → ParallelHistoryFetcher → ParquetStorage
```

`MarketWindowLoader` (282 lines) **delegates** to `ParquetMarketProvider`
(290 lines) — `window_loader.py:113` calls `self._provider.history(...)`.
This is a layered pair, not a duplicate. The loader adds datalake→broker
fallback and `MarketSource` provenance; the provider owns the parquet read.

`simple_sync.py` (141 lines) is the **write** path. It is not a duplicate of
the read path and cannot be merged into it.

`BulkPrefetchMarketProvider` is a scanner-specific batch reader that issues
one multi-symbol `store.read()` instead of O(universe) single-symbol reads.

### 3.2 Feed Layer — already composed

`MarketFeed` (800 lines) owns the feed lifecycle and **holds** the other three
as components, rather than duplicating them:

| Component | Lines | Relationship to `MarketFeed` |
|-----------|-------|------------------------------|
| `FeedSupervisor` | 114 | Owned — exposed as `MarketFeed.supervisor` (`market_feed.py:175`); `notify_reconnect()` calls `connected()` and `recovery_started()` |
| `FeedMonitor` | 813 | Three separate concerns: `FeedStaleMonitor` (poller), `OrderStreamWatchdog`, `SymbolCadenceMonitor` |
| `FeedRecoveryCoordinator` | 423 | Wired from outside via `MarketFeed.set_recovery()` (`market_feed.py:273`); `_trigger_recovery()` (`:557`) drives it |

Merging these into `MarketFeed` would **add** ~1,350 lines to an 800-line
module and remove no logic. It would make the feed layer worse, not better.

### 3.3 Order Layer — event sourcing with a boot-restore mirror

`runtime/startup.py:485-490` documents the design explicitly:

- `SQLiteEventStore` (1,451 lines) — append-only event log, the source of truth
- `SQLiteOrderStore` (686 lines) — mirrors OMS state for fast boot restore
- `SQLiteIdempotencyGuard` — correlation IDs survive restarts
- All three share one DB file

Event projection already exists: `_recover_oms_from_events()`
(`startup.py:1496`) replays the event store into the cache at boot. Adding
`get_order()` / `get_orders()` projection methods to the event store would
create a **second** projection path alongside it.

### 3.4 Bar Aggregation — already joined by a seam

`bar_aggregator.py` (1,035 lines) and `synthetic_ticks.py` (280 lines) are not
duplicates. Both consume `BarSource` (`domain/bar_source.py`), which is the
seam that lets replay drive the same aggregation path as live quotes.

---

## 4. What Phase 1 Actually Removed

These were genuinely dead and are gone (commit `f76d07e`, 10,173 lines):

| Item | Why it was dead |
|------|-----------------|
| `poc/` (2,259 files) | Research scripts, datasets, DuckDB studies — imported by nothing |
| `apps/top_gainers/` | Standalone scratch app |
| `DurableTokenManager` | Deprecated; `PortTokenManager` is the live path |
| Legacy rate-limit re-exports | `DHAN_RATE_LIMITS` etc. — no remaining consumers |
| `ParquetBacktestLoader` | Created and stored on the session, **never called** by production code; the API has its own `/api/charts/backtest` route |
| `backfill_2025.py`, `backtest_datalake.py`, `sync_today.py`, `topup_gaps.py` | One-time migrations, complete |

---

## 5. Shotgun Surgery: Reassessed

The earlier review counted "7+ files to add a data source." Measured against the
current tree, adding an exchange would touch:

1. `domain/instruments.py` — exchange enum
2. `market_data/parquet_storage.py` — partition layout
3. `brokers/` — adapter
4. `config/schema.py` — config fields

That is the normal layering of a multi-exchange system, not duplication. The
`MarketSource` and `BarSource` enums already provide the extension points.

---

## 6. Test Coverage

### 6.1 Architecture gates (all green)

| Gate | Tests | What it enforces |
|------|-------|------------------|
| `test_import_boundaries` | 22 | Package layering, no import cycles |
| `test_complexity_gate` | 7 | Complexity ceilings |
| `test_guardrails` | 13 | Guardrail rules |
| `test_no_foreign_private_writes` | 1 | No cross-module private mutation |
| `test_parquet_history_seam` | 3 | Datalake/broker fallback seam |
| `test_indicator_source_parity` | 3 | Parity with `openalgo-charts` goldens |

### 6.2 Genuinely missing coverage

| Gap | Risk |
|-----|------|
| `MarketWindowLoader` broker fallback and `MarketSource` provenance | High |
| Event store hash-chain tamper detection | Medium |
| End-to-end backtest (datalake → loader → engine → report) | Medium |
| End-to-end live order (API → engine → broker → event store) | Medium |

---

## 7. Deployment Decision

### **Decision: GO** — conditional on §8

The blocking findings of the earlier review (duplicate paths, untested legacy
layers, dead code inflating the artifact) have been resolved or shown not to
exist. What remains is one flaky test, five lint errors, and four test gaps.

---

## 8. Remaining Work

1. **Fix 5 lint errors** introduced by Phase 1 — unused `rate_table` imports in
   `resilience.py`, two whitespace errors in test files. *Real regression from my
   own Phase 1 work.*
2. **Correct this document** and delete the two agent prompts whose premises
   were false.
3. **Stabilize `test_stale_takeover_race_never_double_acquires`** — the
   implementation is correct (`O_EXCL` + `flock` on one inode); the test asserts
   across 8 retries from 12 subprocesses and is load-sensitive.
4. **Add the four missing test areas** in §6.2.

---

## 9. Sign-Off

**Decision:** **GO** after §8 closes.
**Next review:** After the four items land.

---

## 10. Appendix: What the Earlier Review Got Wrong

Per `CLAUDE.md` "Standing lessons §2" — a doc that names a nonexistent problem
is worse than no doc. Recording the discrepancy so it is not repeated.

| Earlier claim | Measured | Error |
|---|---|---|
| 75,297-line platform | 106,836 (incl. tests) | overstated |
| `MarketFeed` 34,952 lines | 800 | 44x |
| `FeedMonitor` 36,084 lines | 813 | 44x |
| `FeedRecoveryCoordinator` 16,626 lines | 423 | 39x |
| `ParquetMarketProvider` 11,188 lines | 290 | 39x |
| `backtest.py` 62,050 lines | 1,236 | 50x |
| `_round_trips.py` 20,192 lines | 509 | 40x |
| `bar_aggregator.py` / `synthetic_ticks.py` duplicate | joined by `BarSource` seam | already done |
| `indicators.py` / `engine.py` duplicate | `engine.py` is one class, 325 lines | false |
| `SQLiteOrderStore` redundant | complementary boot-restore mirror | false |
| `simple_sync.py` redundant | the write path | false |
| `poc/` 15,000 lines | 2,259 files | understated |

**Root cause:** the earlier review recorded file sizes from an intermediate
state of the tree and never re-measured. Two attempts to act on it were then
killed by third-party API 429s before the discrepancy surfaced.