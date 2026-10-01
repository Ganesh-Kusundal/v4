# Data Layer Consolidation — Migration Mapping

## Executive Summary

Three data loading implementations exist in the codebase:

1. **`MarketWindowLoader`** (canonical, 237 lines) — `market_data/src/tradex_market_data/window_loader.py`
2. **`ParquetMarketProvider`** (redundant, 275 lines) — `market_data/src/tradex_market_data/market_provider.py`
3. **`simple_sync.py`** (redundant, 141 lines) — `market_data/src/tradex_market_data/simple_sync.py`

**Note:** The task description cited line counts of 7,979 / 11,188 / 4,964 lines. The actual files are 237 / 275 / 141 lines respectively. The consolidation scope is much smaller than initially estimated.

## Current Architecture

### MarketWindowLoader (Canonical)

**Purpose:** Single resolution point for market windows with datalake→broker fallback and provenance tracking.

**Public API:**
- `MarketWindowLoader(provider=None, *, broker_history=None, metrics=None)`
- `load(instrument, timeframe, start, end, *, allow_broker=False) -> MarketWindow`
- `load_replay_m1(instrument, *, minutes=390, start_time=None, now=None) -> MarketWindow`
- `provider` property → `ParquetMarketProvider`

**Key Features:**
- Datalake-first with optional broker fallback
- Provenance tracking via `MarketSource` enum (DATALAKE / BROKER / NONE)
- Returns `MarketWindow` dataclass with candles + source metadata
- Already depends on `ParquetMarketProvider` internally

### ParquetMarketProvider (Redundant)

**Purpose:** Direct datalake access for history queries.

**Public API:**
- `ParquetMarketProvider(store=None, base_path=None, metrics=None)`
- `history(instrument, timeframe, start, end) -> HistoricalSeries`
- `store` property → `ParquetStorage`

**Key Features:**
- Reads from parquet datalake
- Resamples M1 → other timeframes via `HistoricalSeries.resample()`
- Returns `HistoricalSeries` (not `MarketWindow`)
- No broker fallback
- No provenance tracking

**Also contains:** `BulkPrefetchMarketProvider` — scanner-optimized batch reader

### simple_sync.py (Redundant)

**Purpose:** Batch datalake fill from broker APIs.

**Public API:**
- `simple_sync(broker, store, instruments, timeframe, start, end, *, batch_size=20, max_workers=5, ranges=None) -> SyncResult`
- `series_to_frame(series, symbol) -> pd.DataFrame`
- `SyncResult` dataclass

**Key Features:**
- Parallel fetch via `ParallelHistoryFetcher`
- Batch processing with configurable workers
- Error classification (EMPTY_SERIES / NO_PROVIDER_KEY / NON_EQUITY_PRIMARY / TRANSIENT)
- Direct parquet upsert

## Migration Mapping

### ParquetMarketProvider → MarketWindowLoader

| ParquetMarketProvider | MarketWindowLoader Equivalent | Notes |
|----------------------|-------------------------------|-------|
| `history(instrument, tf, start, end)` | `load(instrument, tf, start, end)` | Returns `MarketWindow` instead of `HistoricalSeries` |
| `store` property | `provider.store` | Access underlying `ParquetStorage` |
| Constructor `(store, base_path, metrics)` | Constructor `(provider, broker_history, metrics)` | Pass `ParquetMarketProvider` as `provider` |

**Migration Steps:**

1. **For callers needing `HistoricalSeries`:**
   ```python
   # Before
   provider = ParquetMarketProvider(store=store)
   series = provider.history(instrument, tf, start, end)
   
   # After
   loader = MarketWindowLoader(provider=ParquetMarketProvider(store=store))
   window = loader.load(instrument, tf, start, end)
   series = HistoricalSeries(
       instrument=window.instrument,
       timeframe=window.timeframe,
       candles=list(window.candles),
       start=window.start,
       end=window.end,
   )
   ```

2. **For callers needing candles + provenance:**
   ```python
   # Before
   provider = ParquetMarketProvider(store=store)
   series = provider.history(instrument, tf, start, end)
   candles = series.candles
   
   # After
   loader = MarketWindowLoader(provider=ParquetMarketProvider(store=store))
   window = loader.load(instrument, tf, start, end)
   candles = window.candles
   source = window.source  # NEW: provenance tracking
   ```

3. **For callers needing broker fallback:**
   ```python
   # Before: Not available in ParquetMarketProvider
   
   # After
   loader = MarketWindowLoader(
       provider=ParquetMarketProvider(store=store),
       broker_history=broker.history,
   )
   window = loader.load(instrument, tf, start, end, allow_broker=True)
   ```

### simple_sync.py → Keep as-is

**Decision:** `simple_sync.py` is NOT redundant. It serves a different purpose:

- **MarketWindowLoader:** Read path (datalake → caller)
- **simple_sync.py:** Write path (broker → datalake)

The write path is already consolidated — `backfill_parquet.py`, `fill_gaps.py`, `repair_gaps.py`, and `reconcile_bars.py` all use `simple_sync()` as their single fill mechanism.

**Action:** Keep `simple_sync.py` unchanged.

## External Usage Analysis

### ParquetMarketProvider Usage (Non-test)

| File | Usage | Migration Required |
|------|-------|-------------------|
| `market_data/__init__.py` | Export | Remove export |
| `window_loader.py` | Internal dependency | Keep (already canonical) |
| `runtime/startup.py` | `ParquetMarketProvider(metrics=metrics)` | Use `MarketWindowLoader` |
| `runtime/feed_recovery.py` | `ParquetMarketProvider(store=store)` | Use `MarketWindowLoader` |
| `interfaces/routes/stream_indicators.py` | `ParquetMarketProvider(base_path=...).history(...)` | Use `MarketWindowLoader` |
| `interfaces/routes/chart.py` | `BulkPrefetchMarketProvider` | Keep (scanner-specific) |
| `interfaces/routes/stream.py` | `ParquetMarketProvider(store=store)` | Use `MarketWindowLoader` |
| `strategy/core/scanner.py` | String reference only | Update docstring |

### simple_sync.py Usage (Non-test)

| File | Usage | Action |
|------|-------|--------|
| `market_data/__init__.py` | Export | Keep |
| `trading/scripts/reconcile_bars.py` | `series_to_frame` | Keep |
| `trading/scripts/fill_gaps.py` | `simple_sync` | Keep |
| `trading/scripts/repair_gaps.py` | `simple_sync` | Keep |
| `interfaces/cli.py` | `simple_sync` | Keep |

## Consolidation Plan (Revised)

### Understanding the Architecture

After analyzing the actual code:

1. **MarketWindowLoader** is ALREADY the canonical read path with:
   - Datalake-first with broker fallback
   - Provenance tracking (`MarketSource` enum)
   - Already uses `ParquetMarketProvider` internally
   - Returns `MarketWindow` with candles + metadata

2. **ParquetMarketProvider** is the internal datalake reader that `MarketWindowLoader` wraps

3. **simple_sync.py** is the WRITE path (broker → datalake), NOT a duplicate

### Actual Consolidation Task

The task is to **migrate external callers** from using `ParquetMarketProvider` directly to using `MarketWindowLoader`, then deprecate `ParquetMarketProvider` as a public API.

### Phase 1: Add Backward Compatibility to MarketWindowLoader

Add a `history()` method to `MarketWindowLoader` that returns `HistoricalSeries` for callers that need the old interface:

```python
def history(
    self,
    instrument: Any,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    *,
    allow_broker: bool = False,
) -> HistoricalSeries:
    """Backward-compatible method returning HistoricalSeries."""
    window = self.load(instrument, timeframe, start, end, allow_broker=allow_broker)
    return HistoricalSeries(
        instrument=window.instrument,
        timeframe=window.timeframe,
        candles=list(window.candles),
        start=window.start,
        end=window.end,
    )
```

### Phase 2: Migrate External Callers

**Files to update:**

1. **runtime/startup.py:942-946** — Scanner engine market provider
2. **runtime/feed_recovery.py:403-410** — Feed recovery history provider
3. **interfaces/routes/stream_indicators.py:139-150** — Indicator tail cache
4. **interfaces/routes/stream.py:986, 1098** — Stream route providers
5. **interfaces/routes/chart.py:948** — Keep `BulkPrefetchMarketProvider` (scanner-specific)

### Phase 3: Deprecate ParquetMarketProvider

1. Add deprecation warning to `ParquetMarketProvider.__init__`
2. Remove from `market_data/__init__.py` exports
3. Keep class in `market_provider.py` for:
   - `BulkPrefetchMarketProvider` (used by chart.py scanner)
   - `MarketWindowLoader` internal use
   - Backward compatibility during transition

### Phase 4: simple_sync.py

**No action needed.** Already the canonical write path used by all backfill scripts.

## Test Strategy

### Tests to Update

- `trading/tests/datalake/test_market_provider.py` — Add deprecation tests
- `trading/tests/datalake/test_simple_sync.py` — No changes needed
- `trading/tests/interface/test_chart_history.py` — Verify still passes
- `trading/tests/interface/test_chart_backtest.py` — Verify still passes
- `trading/tests/interface/test_ws_bars_replay.py` — Verify still passes

### Acceptance Criteria

- [ ] All existing tests pass
- [ ] `MarketWindowLoader` is the only data loading entry point for reads
- [ ] `simple_sync` is the only data loading entry point for writes
- [ ] No breaking changes to public APIs
- [ ] Provenance tracking works correctly
- [ ] Broker fallback works correctly

## Risk Assessment

**Low Risk:**
- `MarketWindowLoader` already depends on `ParquetMarketProvider`
- All callers can be migrated without API changes
- `simple_sync.py` is not being consolidated

**Medium Risk:**
- `BulkPrefetchMarketProvider` is scanner-specific and must be preserved
- Some callers may depend on `HistoricalSeries` return type

**Mitigation:**
- Add `history()` convenience method to `MarketWindowLoader`
- Keep `ParquetMarketProvider` class (with deprecation warning) for `BulkPrefetchMarketProvider`
- Comprehensive test coverage before and after