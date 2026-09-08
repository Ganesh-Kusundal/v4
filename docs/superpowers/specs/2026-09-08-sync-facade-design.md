# Sync Facade Design

> Spec: thin `SyncOrchestrator` over `ParallelHistoryFetcher + GapDetector + ParquetStorage`. Approved scope A/A/A, approach 1.

**Goal:** Replace the sequential broker loop with a facade that delegates fetch/chunk/rate-limit/failover to the fetcher and skip-complete logic to gap detection.

**Architecture:** `SyncOrchestrator(store, fetcher, gaps)` holds no fetch logic. `sync()` = `gaps.detect()` → `fetcher.fetch(ranges)` → `series_to_frame` → batched `upsert`. `sync_today()` = `sync()` with 09:15→now window. `sync_now.py` deleted; `sync_today.py` thins to CLI wrapper.

**Tech Stack:** `trading/src/tradex_trading/datalake/{historical_sync,parallel_fetcher,gap_detector,parquet_storage}.py`, `Timeframe`, `to_ist_naive`, `MARKET_OPEN`.

## Constraints

- Breaking ctor change: `(store, fetcher, gaps)`, no `from_brokers` shim.
- Gap-aware by default (`skip_existing=True`, `min_gap_stamps=15`, `batch_size=20`).
- `series_to_frame` is the single converter (tz-bug fixed, 09:00→09:15).
- Self-review: no TBDs; architecture matches flow; single-plan scope; `ranges` keyed by `str(instrument_id)` made explicit.
