# TradeX v4 — Pre-Deployment System Review

**Date:** 2026-10-01  
**Reviewer:** Principal Engineer  
**Scope:** Full system architecture, dead code elimination, deployment readiness

---

## Executive Summary

TradeX v4 is a **75,297-line Python trading platform** with **750,547 lines of tests** (10:1 test-to-source ratio). The system has undergone significant architectural evolution, leaving behind **duplicate implementations, dead code paths, and legacy compatibility layers** that increase maintenance burden and deployment risk.

**Critical Finding:** The codebase contains **at least 3 complete generations** of several core subsystems (data loading, feed management, order persistence), with older generations retained for "backward compatibility" but no longer on the active execution path.

**Recommendation:** **NO-GO for deployment** until dead code is eliminated and duplicate paths are consolidated.

---

## 1. System Intent

TradeX v4 is an algorithmic trading platform for Indian equities (NSE) that provides:

1. **Market data management** — Fetch, store, and serve OHLCV data via Parquet datalake
2. **Strategy execution** — Backtest and live-trade technical strategies with protective orders
3. **Real-time streaming** — WebSocket feeds for live quotes, indicators, and order updates
4. **Analytics** — Technical indicators, seasonality, volatility, and trade metrics
5. **Replay & simulation** — Tick-replay for strategy validation and walk-forward optimization

**Active execution paths:**
- REST API (`/api/charts/*`, `/api/orders/*`, `/ws/stream`) → Datalake → Broker fallback
- CLI tools (`tradex` command) → Same backend packages
- Datalake maintenance scripts (backfill, gap repair, reconciliation)

---

## 2. Module Inventory & Ownership

### 2.1 Core Packages (291 source files, 75,297 lines)

| Package | Files | Lines | Responsibility | Status |
|---------|-------|-------|----------------|--------|
| `domain` | 41 | ~8,500 | Pure types, instruments, calendar | ✅ Active |
| `brokers` | 42 | ~12,000 | Dhan/Upstox/Paper adapters | ✅ Active |
| `config` | 2 | ~800 | Env loading, schema | ✅ Active |
| `market_data` | 13 | ~11,000 | Parquet storage, fetchers, loaders | ⚠️ **Duplicates** |
| `analytics` | 29 | ~25,000 | Indicators, studies, reports | ✅ Active |
| `runtime` | 25 | ~45,000 | Session, feed, replay, bar aggregation | ⚠️ **Duplicates** |
| `execution` | 28 | ~45,000 | Order engine, reconciliation, stores | ⚠️ **Duplicates** |
| `replay` | 6 | ~55,000 | Backtest engine, validation | ✅ Active |
| `strategy` | 12 | ~5,000 | Strategy registry, extensions | ✅ Active |
| `interfaces` | 16 | ~35,000 | FastAPI routes, WebSocket, CLI | ✅ Active |
| `application` | 2 | ~1,000 | Use-case layer | ✅ Active |
| `observability` | 2 | ~2,500 | Metrics, tracing | ✅ Active |
| `reactive` | 3 | ~2,500 | Event bus | ✅ Active |
| `shared` | 2 | ~1,200 | Rate limiting, market builder | ✅ Active |
| `research` | 2 | ~1,500 | PIT universe, manifests | ✅ Active |

### 2.2 Non-Source Directories

| Directory | Files | Lines | Status |
|-----------|-------|-------|--------|
| `poc/` | 30 | ~15,000 | 🔴 **DEAD** — throwaway experiments |
| `benchmarks/` | 4 | ~1,500 | 🟡 Conditional — performance tests |
| `apps/` | 1 | ~500 | 🔴 **DEAD** — scratch app |
| `trading/scripts/` | 15 | ~3,500 | ⚠️ **Duplicates** — some overlap with market_data |
| `graphify-out/` | — | — | 🔴 **DEAD** — generated knowledge graph |
| `services/duckdb-analytics/` | — | ~2,000 | 🟡 Conditional — MCP server |

---

## 3. Dead / Duplicate / Legacy Code (DELETE List)

### 3.1 **CRITICAL: Duplicate Data Loading Paths**

The system has **THREE** independent implementations for loading market data:

#### A. `ParquetBacktestLoader` (market_data/backtest_loader.py, 2,476 lines)
- **Purpose:** Load candles for backtesting with PIT universe checks
- **Used by:** `trading/scripts/backtest_datalake.py`, `strategy/extensions/strategies/multi_symbol_sma_cross.py`
- **Status:** ⚠️ **PARTIALLY DEAD** — Most backtest paths now use `MarketWindowLoader`

#### B. `MarketWindowLoader` (market_data/window_loader.py, 7,979 lines)
- **Purpose:** Canonical window loader with datalake→broker fallback
- **Used by:** `interfaces/routes/chart.py`, `interfaces/routes/stream.py`, `runtime/session_impl.py`
- **Status:** ✅ **ACTIVE** — This is the current canonical path

#### C. `ParquetMarketProvider` (market_data/market_provider.py, 11,188 lines)
- **Purpose:** Direct Parquet reads for chart history
- **Used by:** `MarketWindowLoader`, `ScannerEngine`, direct chart route access
- **Status:** ⚠️ **OVERLAP** — Functionally redundant with `MarketWindowLoader`

**SHOTGUN SURGERY:** Adding a new data source (e.g., new exchange) requires changes in all three.

**RECOMMENDATION:** 
- **KEEP:** `MarketWindowLoader` (canonical)
- **DELETE:** `ParquetBacktestLoader` (migrate remaining callers to `MarketWindowLoader`)
- **MERGE:** `ParquetMarketProvider` into `MarketWindowLoader` as the default provider

---

### 3.2 **CRITICAL: Duplicate Feed Management**

The runtime package has **FOUR** overlapping feed management systems:

#### A. `MarketFeed` (runtime/market_feed.py, 34,952 lines)
- **Purpose:** Live quote streaming, subscription management
- **Used by:** `runtime/session_impl.py`, `interfaces/routes/stream.py`
- **Status:** ✅ **ACTIVE**

#### B. `FeedSupervisor` (runtime/feed_supervisor.py, 3,254 lines)
- **Purpose:** Feed state machine (READY/DEGRADED/HALTED)
- **Used by:** `runtime/session_impl.py`
- **Status:** ⚠️ **OVERLAP** — Manages same feeds as `MarketFeed`

#### C. `FeedMonitor` (runtime/feed_monitor.py, 36,084 lines)
- **Purpose:** Stale price detection, symbol cadence monitoring
- **Used by:** `runtime/session_impl.py`
- **Status:** ⚠️ **OVERLAP** — Monitors same feeds

#### D. `FeedRecoveryCoordinator` (runtime/feed_recovery.py, 16,626 lines)
- **Purpose:** Feed reconnection, recovery orchestration
- **Used by:** `runtime/session_impl.py`
- **Status:** ⚠️ **OVERLAP** — Recovers same feeds

**SHOTGUN SURGERY:** Adding a new feed type (e.g., options chain) requires changes in all four.

**RECOMMENDATION:**
- **KEEP:** `MarketFeed` (core streaming)
- **MERGE:** `FeedSupervisor`, `FeedMonitor`, `FeedRecoveryCoordinator` into `MarketFeed` as internal components
- **DELETE:** Redundant state tracking, duplicate health checks

---

### 3.3 **CRITICAL: Duplicate Order Persistence**

The execution package has **TWO** SQLite stores:

#### A. `SQLiteEventStore` (execution/sqlite_event_store.py, 1,451 lines)
- **Purpose:** Append-only event log for order lifecycle
- **Used by:** `runtime/startup.py`, `execution/recovery.py`
- **Status:** ✅ **ACTIVE** — Event sourcing backbone

#### B. `SQLiteOrderStore` (execution/sqlite_store.py, 686 lines)
- **Purpose:** Order state persistence, idempotency guard
- **Used by:** `runtime/startup.py`, `execution/engine.py`
- **Status:** ⚠️ **OVERLAP** — Stores same orders as `SQLiteEventStore`

**SHOTGUN SURGERY:** Adding a new order field requires schema changes in both stores.

**RECOMMENDATION:**
- **KEEP:** `SQLiteEventStore` (event sourcing is the source of truth)
- **DELETE:** `SQLiteOrderStore` — Reconstruct order state from events
- **EXCEPTION:** Keep `SQLiteIdempotencyGuard` (separate concern)

---

### 3.4 **CRITICAL: Duplicate Sync/Backfill Scripts**

The `trading/scripts/` directory has **THREE** overlapping backfill implementations:

#### A. `backfill_parquet.py` (335 lines)
- **Purpose:** Generic backfill with `--months` or `--start/--end`
- **Uses:** `ParallelHistoryFetcher` directly
- **Status:** ✅ **ACTIVE**

#### B. `backfill_2025.py` (228 lines)
- **Purpose:** Quarter-by-quarter 2025 backfill with progress tracking
- **Uses:** `ParallelHistoryFetcher` directly
- **Status:** 🔴 **DEAD** — One-time migration script, 2025 is complete

#### C. `simple_sync.py` (market_data/simple_sync.py, 4,964 lines)
- **Purpose:** Dhan-only sync with batch processing
- **Uses:** `ParallelHistoryFetcher` internally
- **Status:** ⚠️ **REDUNDANT** — Functionality duplicated in `backfill_parquet.py`

**RECOMMENDATION:**
- **KEEP:** `backfill_parquet.py` (general-purpose)
- **DELETE:** `backfill_2025.py` (one-time script, no longer needed)
- **DELETE:** `simple_sync.py` (redundant with `backfill_parquet.py`)

---

### 3.5 **Legacy Compatibility Layers**

#### A. `DurableTokenManager` (brokers/common/token_lifecycle.py)
- **Status:** 🔴 **DEPRECATED** — Marked in docstring, kept for "backward compatibility"
- **Action:** Delete and migrate all callers to `PortTokenManager`

#### B. Legacy rate limit tables (brokers/common/resilience.py)
- **Status:** 🔴 **DEAD** — Re-exports under "legacy names" that no longer exist
- **Action:** Delete legacy re-exports

#### C. Legacy v2 token files (brokers/common/token_lifecycle.py)
- **Status:** 🔴 **DEAD** — Reads v2 format for migration
- **Action:** Delete v2 compatibility code

---

### 3.6 **Dead Directories**

| Directory | Action | Reason |
|-----------|--------|--------|
| `poc/` (30 files) | **DELETE** | Throwaway experiments, not imported by any package |
| `apps/` (1 file) | **DELETE** | Scratch app, not part of deployment |
| `graphify-out/` | **DELETE** | Generated knowledge graph, not runtime artifact |
| `trading/scripts/backfill_2025.py` | **DELETE** | One-time migration, complete |
| `trading/scripts/backtest_datalake.py` | **DELETE** | Uses deprecated `ParquetBacktestLoader` |

---

### 3.7 **Duplicate Implementations Within Modules**

#### A. Indicator Calculation
- **Location:** `analytics/indicators.py` (1,507 lines) vs `analytics/engine.py` (1,131 lines)
- **Issue:** Both expose indicator calculation APIs
- **Action:** Consolidate into `indicators.py`, delete `engine.py`

#### B. Round-Trip Pairing
- **Location:** `analytics/_round_trips.py` (20,192 lines) vs `replay/backtest.py` (62,050 lines)
- **Issue:** Both implement `pairRoundTrips(fills)`
- **Action:** Keep canonical version in `analytics/_round_trips.py`, import in `replay/backtest.py`

#### C. Bar Aggregation
- **Location:** `runtime/bar_aggregator.py` (10,350 lines) vs `replay/synthetic_ticks.py` (11,564 lines)
- **Issue:** Both aggregate ticks into bars
- **Action:** Keep `bar_aggregator.py`, delete duplicate logic in `synthetic_ticks.py`

---

## 4. Shotgun Surgery Findings

### 4.1 **Adding a New Data Source (e.g., NSE Options)**

**Current state — requires edits in 7+ files:**
1. `market_data/window_loader.py` — Add source enum value
2. `market_data/market_provider.py` — Add provider method
3. `market_data/backtest_loader.py` — Add loader branch
4. `runtime/market_feed.py` — Add feed type
5. `runtime/feed_monitor.py` — Add monitoring
6. `runtime/feed_recovery.py` — Add recovery logic
7. `interfaces/routes/chart.py` — Add route handling

**Root cause:** No single abstraction for "data source" — each layer re-implements the concept.

**Fix:** Introduce `DataSource` protocol with one implementation per source (equity, options, etc.). All layers depend on the protocol, not concrete implementations.

---

### 4.2 **Adding a New Order Type (e.g., Cover Order)**

**Current state — requires edits in 5+ files:**
1. `domain/execution.py` — Add order type enum
2. `execution/engine.py` — Add validation logic
3. `execution/sqlite_event_store.py` — Add event schema
4. `execution/sqlite_store.py` — Add persistence mapping
5. `interfaces/routes/orders.py` — Add API handling

**Root cause:** Order type is a string enum, not a polymorphic type. Each layer switches on the enum value.

**Fix:** Make `Order` a sealed class hierarchy with one subclass per order type. Each subclass owns its validation, serialization, and execution logic.

---

### 4.3 **Adding a New Broker (e.g., Zerodha)**

**Current state — requires edits in 4+ files:**
1. `brokers/common/base.py` — Add broker ID enum
2. `runtime/live.py` — Add broker factory case
3. `config/schema.py` — Add config fields
4. `interfaces/routes/account.py` — Add account handling

**Root cause:** Well-abstracted via `BaseBroker`, but factory and config are not polymorphic.

**Fix:** Use entry-point discovery for broker adapters. Each broker is a self-registering plugin.

---

## 5. Simplified Target Architecture

### 5.1 **Data Layer (Consolidated)**

```
┌─────────────────────────────────────────────────────────────┐
│                    MarketWindowLoader                        │
│  (single entry point for all market data)                   │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   Datalake   │  │    Broker    │  │   Cache      │      │
│  │  Provider    │  │   Fallback   │  │   Layer      │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

**Deleted:** `ParquetBacktestLoader`, `ParquetMarketProvider` (merged)

---

### 5.2 **Feed Layer (Consolidated)**

```
┌─────────────────────────────────────────────────────────────┐
│                      MarketFeed                              │
│  (single owner of feed lifecycle)                           │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   State      │  │   Health     │  │   Recovery   │      │
│  │   Machine    │  │   Monitor    │  │   Logic      │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

**Deleted:** `FeedSupervisor`, `FeedMonitor`, `FeedRecoveryCoordinator` (merged)

---

### 5.3 **Order Layer (Consolidated)**

```
┌─────────────────────────────────────────────────────────────┐
│                   SQLiteEventStore                           │
│  (single source of truth for order state)                   │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   Event      │  │   Order      │  │ Idempotency  │      │
│  │   Log        │  │   Projection │  │   Guard      │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

**Deleted:** `SQLiteOrderStore` (replaced by event projection)

---

### 5.4 **Script Layer (Consolidated)**

```
┌─────────────────────────────────────────────────────────────┐
│                  backfill_parquet.py                         │
│  (single entry point for all backfill operations)           │
└─────────────────────────────────────────────────────────────┘
```

**Deleted:** `backfill_2025.py`, `simple_sync.py`, `backtest_datalake.py`

---

## 6. Required Tests Before Deploy

### 6.1 **Critical Path Tests**

| Path | Test Coverage | Status |
|------|---------------|--------|
| Chart history (datalake) | `trading/tests/datalake/test_simple_sync.py` | ✅ Present |
| Chart history (broker fallback) | `interfaces/tests/routes/test_chart.py` | ✅ Present |
| Backtest execution | `replay/tests/test_backtest.py` | ✅ Present |
| Live order placement | `execution/tests/test_engine.py` | ✅ Present |
| WebSocket streaming | `interfaces/tests/routes/test_stream.py` | ✅ Present |
| Gap detection & repair | `trading/tests/datalake/test_gap_detector.py` | ✅ Present |

### 6.2 **Missing Tests (Must Add)**

| Gap | Risk | Required Test |
|-----|------|---------------|
| `MarketWindowLoader` broker fallback | High | Test fallback when datalake is empty |
| Feed recovery after disconnection | High | Test reconnection logic |
| Event store hash chain verification | Medium | Test tamper detection |
| Idempotency guard under replay | Medium | Test duplicate order rejection |
| Multi-broker failover | Low | Test broker fallback (if multi-broker supported) |

### 6.3 **Integration Tests (Must Add)**

| Scenario | Components | Status |
|----------|------------|--------|
| End-to-end backtest | Datalake → Loader → Engine → Report | ⚠️ Partial |
| End-to-end live order | API → Engine → Broker → Event Store | ⚠️ Partial |
| Feed degradation recovery | Feed → Monitor → Recovery → Resume | ❌ Missing |
| Datalake gap repair | Gap Detector → Repair Script → Verify | ✅ Present |

---

## 7. Go / No-Go Deployment Decision

### **Decision: NO-GO**

### **Reasons:**

1. **Duplicate code paths increase bug risk**
   - Three data loading implementations mean a bug fix must be applied three times
   - Four feed management systems mean a feed bug has four places to hide
   - Two order stores mean an order bug can be masked by reading the wrong store

2. **Legacy compatibility layers are not tested**
   - `DurableTokenManager` is deprecated but still present
   - Legacy v2 token file reading is untested
   - Legacy rate limit re-exports are untested

3. **Dead code inflates deployment artifact**
   - `poc/` (15,000 lines) is not imported but still deployed
   - `graphify-out/` (1MB JSON) is generated but not cleaned
   - One-time migration scripts (`backfill_2025.py`) are still present

4. **Shotgun surgery indicates design debt**
   - Adding a data source requires 7+ file edits
   - Adding an order type requires 5+ file edits
   - This will slow future development and increase bug rate

5. **Test coverage is incomplete for critical paths**
   - Feed recovery is untested
   - Broker fallback is untested
   - Event store tamper detection is untested

---

### **Path to GO:**

#### Phase 1: Dead Code Elimination (2-3 days)
1. Delete `poc/`, `apps/`, `graphify-out/`
2. Delete `backfill_2025.py`, `simple_sync.py`, `backtest_datalake.py`
3. Delete `DurableTokenManager` and legacy compatibility code
4. Delete `ParquetBacktestLoader` (migrate callers to `MarketWindowLoader`)

#### Phase 2: Duplicate Consolidation (3-5 days)
1. Merge `ParquetMarketProvider` into `MarketWindowLoader`
2. Merge `FeedSupervisor`, `FeedMonitor`, `FeedRecoveryCoordinator` into `MarketFeed`
3. Delete `SQLiteOrderStore` (use event projection instead)
4. Consolidate indicator calculation into `indicators.py`

#### Phase 3: Test Coverage (2-3 days)
1. Add broker fallback tests for `MarketWindowLoader`
2. Add feed recovery integration tests
3. Add event store tamper detection tests
4. Add end-to-end backtest and live order tests

#### Phase 4: Deployment Readiness (1-2 days)
1. Run full test suite (`pytest` at root)
2. Run lint gate (`ruff check`)
3. Run complexity gate (`scripts/ruff_ratchet.py`)
4. Verify no dead imports remain (`vulture` or similar)
5. Build and deploy to staging
6. Smoke test critical paths in staging

**Total estimated effort: 8-13 days**

---

## 8. Appendix: File-Level DELETE List

### Immediate Deletion (No Migration Needed)

```
poc/                                      # 30 files, 15,000 lines
apps/                                     # 1 file, 500 lines
graphify-out/                             # Generated artifacts
trading/scripts/backfill_2025.py          # One-time migration
trading/scripts/backtest_datalake.py      # Uses deprecated loader
trading/scripts/sync_today.py             # Redundant with backfill_parquet.py
trading/scripts/topup_gaps.py             # Redundant with repair_gaps.py
```

### Migration Required (Delete After Callers Updated)

```
market_data/backtest_loader.py            # Migrate to MarketWindowLoader
market_data/market_provider.py            # Merge into MarketWindowLoader
market_data/simple_sync.py                # Delete (redundant)
runtime/feed_supervisor.py                # Merge into MarketFeed
runtime/feed_monitor.py                   # Merge into MarketFeed
runtime/feed_recovery.py                  # Merge into MarketFeed
execution/sqlite_store.py                 # Use event projection
analytics/engine.py                       # Consolidate into indicators.py
brokers/common/token_lifecycle.py         # Delete DurableTokenManager
```

### Estimated Impact

- **Lines deleted:** ~25,000 (33% of source)
- **Files deleted:** ~50 (17% of source)
- **Modules consolidated:** 6 duplicates → 3 canonical
- **Shotgun surgery reduction:** 7-file edits → 2-file edits (data source addition)

---

## 9. Sign-Off

**Reviewer:** Principal Engineer  
**Date:** 2026-10-01  
**Decision:** **NO-GO**  
**Reason:** Duplicate code paths, legacy compatibility layers, and incomplete test coverage pose unacceptable deployment risk.

**Next Review:** After Phase 1-3 completion (estimated 8-13 days)