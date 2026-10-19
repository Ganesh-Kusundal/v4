# Pre-Deployment Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all dead/duplicate/shadow code (D1-D14), clarify split owners (M1-M4), and add blocking deploy proofs (C2/C5 + 4 e2e).

**Architecture:** One cleanup commit (deletions first, cheap wins D4-D13 before D14 tree move), then one test commit (chaos + e2e). No behavior change except deleted paths.

**Tech Stack:** Python 3.12, pytest (root testpaths), ruff, mypy, git.

**Spec:** Pre-Deployment System Review 2026-10-19: DELETE D1-D14, MERGE M1-M4, Required Tests C2/C5 + 4 e2e, NO-GO reasons.

## Global Constraints

- No partial fixes; no backward compat for dead paths; no keep-just-in-case.
- Every deletion verified by `git grep <deleted-path>` = zero outside the commit.
- Full gate `python -m pytest` green after each task group.
- `*.db`, `workspace.sqlite`, `*.idempotency.db` never ship.
- Prefer fewer modules over clever abstractions.

## File Structure

- Deletions touch only listed files + import sites (1-8 files each).
- Merges touch owner file + call sites (no new packages).
- New tests live in per-package `tests/` (never `trading/tests/`).
- `trading/tests/` rescue targets move to owning package suite, then tree deletes.

### Task 1: Shadow state + empty markers (D1, D2, D3)

**Interfaces:** DELETE `trading/data/ohlcv/` (277MB fork; real lake `<repo>/data`); DELETE `$JCODE_SCRATCH_DIR/`; DELETE 4 empty inits (`analytics/.../oscillators|studies|volatility|volume/__init__.py`, graph degree 0).

**Steps:**
- [ ] `rm -rf trading/data '$JCODE_SCRATCH_DIR'` + 4 empty inits.
- [ ] `git grep "trading/data" -- '*.py'` shows only bug-doc mentions, no prod readers.
- [ ] `python -m pytest market_data/tests trading/tests/datalake -q` green.
- [ ] Commit.

### Task 2: Time + interval + status owners (D5, D7, D8, D4)

**Interfaces:** `domain/timeframe.py` (`normalize`, `to_timeframe`, `TIMEFRAMES`) gains daily aliases; `domain/market_calendar.py` gains `NSETradingCalendar`; `execution/kill_switch.py:37` gains ACTIVE/PENDING sets. DELETED: `domain/intervals.py`, `domain/trading_calendar.py`, `runtime/calendar.py`, `domain/order_status.py`, `SESSION_OPEN/CLOSE` + `IST_TIMEZONE` in constants.

**Steps:**
- [ ] Retarget `tests/domain/test_intervals.py`, `test_order_status.py`, `trading/tests/runtime/test_calendar.py` first (TDD seam).
- [ ] Fold daily aliases into `timeframe.normalize()`; retarget `replay_controller.py:219`, `routes/chart.py:873`, `routes/stream.py:626`; delete `_REPLAY_INTERVALS`, `_SUPPORTED_TIMEFRAMES`.
- [ ] Move calendar class; fix `brokers/paper/adapter.py`; delete 2 files; fix `runtime/__init__.py:7`.
- [ ] Move status sets into `kill_switch.py`; delete `domain/order_status.py`.
- [ ] Delete 3 constants names; retarget readers to `MARKET_*`.
- [ ] `git grep` each deleted path returns zero; `python -m pytest tests/domain domain/tests runtime/tests brokers/tests -q`; commit.

### Task 3: Marks + rate-limit + builders + paths + sync (D6, D9, D10, D13)

**Interfaces:** `MarkToMarketService.select_mark()` sole mark owner; `tradex_shared` owns `RateLimitConfig`, `make_candle`, `candles_from_dataframe`; `domain/paths.py` sole runtime resolver; `sync_service.py` gains `series_to_frame()`. DELETED: `domain/marketing.py`, `resilience.RateLimitConfig`, `brokers/market_builders.py`, `brokers/paths.py`, `simple_sync()` function.

**Steps:**
- [ ] Delete `marketing.py`+test; pin math onto `select_mark` tests; consolidate `dhan/client:421` + `upstox/client:346` inline copies.
- [ ] `rate_table.py:13` imports from `tradex_shared`; delete copy, facade, paths shim.
- [ ] Move `series_to_frame`; fix `reconcile_bars.py:60`; delete dead `simple_sync()`.
- [ ] `git grep` verify; `python -m pytest brokers/tests market_data/tests execution/tests -q`; commit.

### Task 4: WS indicator + fee wrappers (D11, D12) and M1-M4

**Interfaces:** `stream_indicators.py:IndicatorStreamRegistry` absorbs `IndicatorStream`; `fees.py` + `fee_processor.py` (new `accrued` property); DELETED `indicator_stream.py`, engine `_compute_fee/_fee_for_fill` wrappers. M-clarify: delete engine cash passthroughs; authority docstrings on both sqlite modules; type feed quartet `startup.py:125-218`; audit `streaming.py` 73L.

**Steps:**
- [ ] Merge indicator class; fix `stream.py:57`; constants from `domain.constants`.
- [ ] Inline fee calls at `engine:810,995,1147`; expose `accrued`; delete wrappers.
- [ ] M1-M4 edits; `python -m pytest interfaces/tests execution/tests -q`; full gate; commit.

### Task 5: Shadow suite rescue + delete (D14)

**Interfaces:** ~12 rescued files `git mv` to owning per-package `tests/`; then delete `trading/tests/` (362 files, absent from root testpaths).

**Steps:**
- [ ] Rescue unique-coverage files (e2e smoke, parity, datalake).
- [ ] Delete tree; root `python -m pytest -q` matches `trading/`-cwd run; commit.

### Task 6: Blocking deploy proofs (C2, C5, 4xe2e, feed sim, determinism)

**Interfaces (new tests):** `market_data/tests/test_tz_mix.py::test_mixed_tz_converts_not_replaces` (C2 BLOCKS); `interfaces/tests/test_orders_not_ready_http.py` (C5, assert no store append); provenance, tamper, e2e-backtest, e2e-live-order; `runtime/tests/test_feed_recovery_sim.py` (AC-20); `test_replay_paisa_parity.py`; `test_grid_parity_30m_1h.py`; C6 serve smoke.

**Steps:**
- [ ] TDD each: failing test, minimal fix, green.
- [ ] Quarantine/fix `test_stale_takeover_race`; fix 5 lints; clean `*.db`/`workspace.sqlite`.
- [ ] Full gate green on clean tree; commit.

## Review Focus

1. Tz-mix conversion (5h30-shift class) - C2 is the deploy gate.
2. HTTP NOT_READY appends nothing - engine-only test insufficient.
3. Fee replay parity to paisa (H2 semantics).
4. RESYNCHRONIZING rejection with `feed_not_ready` metric.
5. D14 rescue completeness - no unique coverage lost.
