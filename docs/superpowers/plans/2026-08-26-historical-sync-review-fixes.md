# Historical Sync Review Fixes — 2026-08-26

Scope from the review of HistoricalSyncService / ParallelHistoryFetcher /
bench_parallel_fetcher.py. Ponytail-scoped: fix what's broken or misleading,
name the skipped upgrades.

## Round 1
1. **IST-naive windows** (`historical_sync.py`): all `datetime.now()` sites now
   use `to_ist_naive(datetime.now(UTC))`; phase 3 guarded off before 09:00 IST.
2. **Docstrings**: "two-phase" -> three-phase (module + class + `sync()`).
3. **Tests**: completed `test_historical_sync.py` from pre-built fixtures.
4. **Benchmark**: labels updated to always-split routing; quota-bound scenario
   added; artifact regenerated (split wins 1.5-1.9x latency-bound, 2.05x
   quota-bound).
5. **Storage one-liners**: thread id in atomic tmp name; module logger.
6. **CLAUDE.md**: stale routing note corrected.

## Round 2 — incremental speed + failure blacklist (user request)
1. **Ranged fetch** (`parallel_fetcher.fetch(..., ranges=)`): per-symbol
   missing sub-windows from GapDetector are fetched directly (each still
   chunked to broker caps, stitched+deduped); failover mirrors it.
2. **Service threading**: phases 1 and 2 pass detector ranges through — a
   20-minute hole costs one small poll instead of a 30-day re-pull.
3. **Failure blacklist** (`data/.sync_blacklist.json`, cooldown 7d): symbols
   still gapped after a run are skipped next runs and reported in
   `SyncResult.blacklisted`; entries bump `fails`, prune on recovery,
   auto-expire after cooldown.
4. **Gap reporting**: `SyncResult.gaps_before` / `gaps_remaining` bracket
   every run.
5. **Tests +9** (ranged windows/dedupe/mixed, blacklist create/skip/expiry/
   prune, gap bracketing). Suite: 138 passed.
6. **Live smoke** (`/tmp/smoke_incremental_sync.py`, real detector+storage):
   holed symbol fetched via single 19-minute ranged call; empty symbol got
   the full-window pull; rerun on complete datalake = zero broker calls;
   verification clean. PASS.

## Skipped (upgrade path)
- Rate-weighted broker split (Dhan 5/s vs Upstox 50/s asymmetry).
- Persistent cross-run broker-health map beyond the symbol blacklist.
