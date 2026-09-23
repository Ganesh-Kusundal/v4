# Deepen parquet market history implementation plan

> **For agentic workers:** Use a test driven workflow. Write each failing test first, run it, make the smallest production change, run the focused suite, then commit the green slice.

**Goal:** Make `ParquetMarketProvider.history()` the canonical parquet history seam for chart history, chart backtests, websocket indicator history, websocket replay candles, and the paper quote fallback read, without changing public payloads or market behavior.

**Source spec:** `docs/specs/0001-deepen-parquet-market-history.md`

**Architecture:** Keep `ParquetMarketProvider` parquet only. It returns `HistoricalSeries`. Routes retain serialization, cache, tail selection, replay selected start filtering, synthetic tick behavior, and broker fallback. Direct `date_range()` calls remain only where they are metadata needed to reproduce current latest available fallback behavior.

## Global constraints

- Do not change REST or websocket payload shapes.
- Do not change timezone, inclusive range, empty result, resampling, or fallback semantics.
- Do not move broker fallback into the datalake provider.
- Do not make `ParquetMarketProvider` return dictionaries, frames, HTTP fields, or websocket frames.
- Do not migrate `BulkPrefetchMarketProvider` direct reads. Its multi symbol prefetch is an intentional scanner optimization, not a duplicated interface history pipeline.
- Do not migrate `ParquetStorage` uses for symbol listing, date ranges, gap detection, audits, writes, or other non history operations.
- Never run operator scripts against live brokers. Tests must use temporary parquet stores and fakes.
- Use the sanctioned Python command:
  `.venv/bin/python -m pytest <paths> -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q`
- Commit each green slice with a conventional commit.

## Slice 1: Pin the provider contract

**Files:**

- Modify `trading/tests/datalake/test_market_provider.py`
- Modify `trading/src/tradex_trading/datalake/market_provider.py` only if a missing contract is exposed

**TDD steps:**

- [ ] Add a test that asserts result instrument, requested timeframe, start, and end metadata for M1 history, satisfies AC-4.
- [ ] Add tests for rows exactly at both inclusive range endpoints, satisfies AC-4.
- [ ] Add a test using timezone aware input and assert existing store normalization remains unchanged, satisfies AC-4.
- [ ] Add a test for instrument symbol selection and the instrument id fallback, satisfies AC-4.
- [ ] Add a test for an empty in range result and missing symbol, including requested metadata, satisfies AC-4.
- [ ] Run the provider file and confirm any new test fails for the intended missing behavior, not a fixture error.
- [ ] Make the smallest provider change needed. Prefer no production change if the current implementation already passes.
- [ ] Run `trading/tests/datalake/test_market_provider.py` and confirm green.

**Implementation note:** The current provider already converts through `candles_from_dataframe`, builds M1 `HistoricalSeries`, and calls `resample`. Treat this slice as a contract pin, not an invitation to redesign it.

**Commit:** `test(datalake): pin parquet market history contract`

## Slice 2: Migrate chart datalake history

**Files:**

- Modify `trading/src/tradex_trading/interface/routes/chart.py`
- Modify `trading/tests/interface/test_chart_history.py`
- Add or modify `trading/tests/interface/test_chart_backtest.py` only for shared seam coverage

**TDD steps:**

- [ ] Add a test that patches `ParquetMarketProvider.history()` and verifies `_bars_from_datalake` passes the exact instrument, timeframe, start, and end, satisfies AC-1 and AC-4.
- [ ] Add or retain the direct resampling parity test, satisfies AC-1 and AC-5.
- [ ] Add or retain tests for limit truncation, `last_closed_time`, IST wall time to UTC seconds, and empty datalake to broker fallback, satisfies AC-1 and AC-5.
- [ ] Run the focused chart tests and observe the new seam test fail because the route still reads the store directly.
- [ ] Change `_bars_from_datalake()` to obtain a `ParquetMarketProvider` over the existing datalake root and call `history()`. Keep `_serialize_series()` and the limit in the route.
- [ ] Remove only history specific `ParquetStorage`, `candles_from_dataframe`, and `HistoricalSeries` imports from that function.
- [ ] Keep `_get_store()` for symbol listing and other non history uses.
- [ ] Run:
  `.venv/bin/python -m pytest trading/tests/interface/test_chart_history.py trading/tests/interface/test_chart_backtest.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q`

**Commit:** `refactor(interface): route chart parquet history through provider`

## Slice 3: Migrate chart backtest candles

**Files:**

- Modify `trading/src/tradex_trading/interface/routes/chart.py`
- Modify `trading/tests/interface/test_chart_backtest.py`

**TDD steps:**

- [ ] Add a seam test for `_backtest_candles()` that patches `ParquetMarketProvider.history()` and asserts exact arguments and returned candle forwarding, satisfies AC-1.
- [ ] Run the focused test and confirm it fails against the direct dataframe pipeline.
- [ ] Replace `_backtest_candles()` with a provider history call and return `series.candles`.
- [ ] Preserve the existing one day warmup extension at the call site and preserve empty series as an empty list.
- [ ] Run chart history and backtest tests.

**Commit:** `refactor(interface): use provider for chart backtest candles`

## Slice 4: Migrate websocket indicator history

**Files:**

- Modify `trading/src/tradex_trading/interface/routes/stream_indicators.py`
- Modify `trading/tests/interface/test_ws_indicator_push.py` or add `trading/tests/interface/test_stream_indicators_history.py`

**TDD steps:**

- [ ] Add a test that patches `ParquetMarketProvider.history()` and verifies the existing thirty day window, instrument, and timeframe are passed, satisfies AC-2.
- [ ] Add tests for tail truncation to `_TAIL_LIMIT`, live bar replacement, empty history, and provider failure degrading to live only, satisfies AC-2 and AC-5.
- [ ] Run the focused test and observe the seam test fail against direct `ParquetStorage` use.
- [ ] Refactor `_load_datalake_tail()` to build the existing `Equity`, compute the existing window, call provider history, slice the final `_TAIL_LIMIT` candles, and serialize exactly as before.
- [ ] Keep the broad exception degradation behavior and logging unchanged.
- [ ] Run:
  `.venv/bin/python -m pytest trading/tests/interface/test_ws_indicator_push.py trading/tests/interface/test_stream_indicators_history.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q`

**Commit:** `refactor(stream): route indicator history through parquet provider`

## Slice 5: Migrate websocket replay candle loading

**Files:**

- Modify `trading/src/tradex_trading/interface/routes/stream.py`
- Modify `trading/tests/interface/test_ws_bars_replay.py`

**TDD steps:**

- [ ] Add a test that patches `ParquetMarketProvider.history()` and verifies the normal replay window arguments for a minutes based replay, satisfies AC-2.
- [ ] Add a test for a selected start time verifying the three day widened start, current end, provider timeframe, and post load selected start filtering, satisfies AC-2.
- [ ] Retain or add tests for fallback to the latest available date range, no history errors, replay started and done acknowledgements, and synthetic tick output, satisfies AC-2 and AC-5.
- [ ] Run the focused replay tests and observe the provider seam test fail against direct dataframe loading.
- [ ] Replace only dataframe to candle loading with provider history.
- [ ] Retain the narrow `date_range(symbol)` lookup and retry window when the initial requested window is empty. This is metadata needed to preserve current latest available replay recovery, not a second candle conversion pipeline.
- [ ] Preserve selected start filtering, replay lifecycle, pause, step, stop, and synthetic tick logic byte for byte where possible.
- [ ] Run:
  `.venv/bin/python -m pytest trading/tests/interface/test_ws_bars_replay.py trading/tests/interface/test_replay_order_gate.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q`

**Commit:** `refactor(stream): route replay candles through parquet provider`

## Slice 6: Migrate paper quote fallback carefully

**Files:**

- Modify `trading/src/tradex_trading/interface/routes/stream.py`
- Modify the relevant existing websocket snapshot test, or add `trading/tests/interface/test_ws_snapshot_datalake_fallback.py`

**Boundary decision:** Keep `date_range(symbol)` direct because the fallback is explicitly based on the latest stored partition, not the current wall clock. Use `ParquetMarketProvider.history()` for the bounded two day read once the latest endpoint is known. If an exact provider call cannot preserve the final stored close, retain the entire narrow read as a documented non history metadata fallback and do not force a lossy migration.

**TDD steps:**

- [ ] Add a test with a temporary store and paper placeholder quote asserting the fallback close, bid, ask, rounding, provider or store call window, and `set_quote` behavior, satisfies AC-3 and AC-5.
- [ ] Add a test for a real broker quote proving the datalake fallback is not used, satisfies AC-3.
- [ ] Run the test and confirm it fails only for the missing seam or test setup.
- [ ] Migrate the bounded close read to provider history if exact latest close semantics remain equal. Keep `date_range()` as metadata lookup.
- [ ] Preserve quote construction and all broad best effort failure handling.
- [ ] Run the snapshot and replay focused tests.

**Commit:** `refactor(stream): share parquet history for paper quote fallback`

## Slice 7: Remove duplicated pipelines and add the source guard

**Files:**

- Modify `trading/src/tradex_trading/interface/routes/chart.py`
- Modify `trading/src/tradex_trading/interface/routes/stream.py`
- Modify `trading/src/tradex_trading/interface/routes/stream_indicators.py`
- Add or modify `tests/test_import_boundaries.py` or a focused architecture test under `tests/`

**TDD steps:**

- [ ] Add a source guard listing the migrated functions and asserting they do not import or call `ParquetStorage`, `DATALAKE_ROOT`, or `candles_from_dataframe` for history loading, satisfies AC-6.
- [ ] Exclude approved non history operations such as symbol listing and `date_range()` metadata fallback from the guard.
- [ ] Run the guard first and confirm it catches a deliberately retained history pipeline or use an equivalent red fixture check.
- [ ] Remove dead imports and duplicated conversion bodies.
- [ ] Run the guard and focused interface tests.
- [ ] Run `ruff check` on all modified Python files.

**Commit:** `test(architecture): guard parquet history callers use provider seam`

## Final verification

- [ ] Run provider tests:
  `.venv/bin/python -m pytest trading/tests/datalake/test_market_provider.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q`
- [ ] Run interface tests:
  `.venv/bin/python -m pytest trading/tests/interface/test_chart_history.py trading/tests/interface/test_chart_backtest.py trading/tests/interface/test_ws_indicator_push.py trading/tests/interface/test_ws_bars_replay.py trading/tests/interface/test_replay_order_gate.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q`
- [ ] Run the sanctioned full suite, satisfying **AC-7**:
  `.venv/bin/python -m pytest domain/tests brokers/tests trading/tests tests -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q`
- [ ] Run `ruff check` on modified source and tests.
- [ ] Run frontend typecheck only as a sanity check because frontend source is not changed.
- [ ] Inspect `git diff --stat` and `git diff --check`.
- [ ] Run `git grep` to confirm no migrated history function rebuilt the direct pipeline.
- [ ] Run the kanban update command if the repository skill path is available. The checked in `.qoder` path is currently absent, so do not fabricate or modify the kanban digest manually.
- [ ] Request a fresh code review before merge, using the spec, this plan, the base commit `1b3c2b0`, and the final implementation commit.

## Rollback

Each slice is independently revertible. If a focused regression appears, revert the current slice and keep earlier green migrations. No data migration, feature flag, or production data rollback is required.

## Known exclusions

- `trading/src/tradex_trading/datalake/backtest_loader.py` already uses `ParquetMarketProvider` and requires no change.
- `BulkPrefetchMarketProvider` intentionally performs a multi symbol direct read and remains unchanged.
- `trading/src/tradex_trading/interface/routes/market_data.py` broker history remains outside this parquet seam.
- `GapDetector`, datalake writers, audit scripts, top gainers, DuckDB catalog code, and sync scripts are separate storage concerns and remain unchanged.
- Replay lifecycle extraction is a follow up, not part of this plan.
