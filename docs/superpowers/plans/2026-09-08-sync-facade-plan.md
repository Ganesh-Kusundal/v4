# Sync Facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the thin sync facade with bug fixes and updated callers/tests.

**Architecture:** Facade delegates to fetcher + gaps + store; batched upsert keeps memory bounded.

**Tech Stack:** Python, pandas, ParallelHistoryFetcher, GapDetector, ParquetStorage.

## Global Constraints

- Breaking ctor `(store, fetcher, gaps)`.
- Gap-aware by default; `min_gap_stamps=15`, `batch_size=20`.
- tz-naive IST storage contract.

---

### Task 1: Facade rewrite

**Files:**
- Modify: `trading/src/tradex_trading/datalake/historical_sync.py`
- Test: `trading/tests/datalake/test_historical_sync.py`

**Interfaces:**
- Consumes: `ParallelHistoryFetcher.fetch(instruments, tf, start, end, ranges) -> (dict, list)`, `GapDetector.detect(...) -> list[(inst, ranges)]`, `ParquetStorage.upsert(df) -> int`
- Produces: `SyncOrchestrator(store, fetcher, gaps=None).sync(...)->SyncResult`, `sync_today(...)->SyncResult`, `series_to_frame(series, symbol)->DataFrame`

- [ ] **Step 1: Rewrite module**

See implementation (done inline): fixed `series_to_frame` tz attach-only-when-naive, `SyncResult.errors`, gap-aware `sync`, 09:15 `sync_today`, `_bar_freq` helper.

- [ ] **Step 2: Update `sync_today.py` to new ctor, delete `sync_now.py`**

- [ ] **Step 3: Rewrite `test_historical_sync.py` for mock fetcher/gaps**

- [ ] **Step 4: Run tests**

Run: `python -m pytest trading/tests/datalake/test_historical_sync.py trading/tests/datalake/test_parallel_fetcher.py trading/tests/datalake/test_gap_detector.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/datalake/historical_sync.py trading/scripts/sync_today.py trading/scripts/sync_now.py trading/tests/datalake/test_historical_sync.py docs/superpowers/specs/2026-09-08-sync-facade-design.md docs/superpowers/plans/2026-09-08-sync-facade-plan.md
git commit -m "refactor: sync facade over fetcher+gaps, fix tz/break/session bugs"
```
