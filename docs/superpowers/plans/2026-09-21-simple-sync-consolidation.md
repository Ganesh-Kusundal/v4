# Simple Sync Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the sync unification — one datalake fill path (`simple_sync`) owning its own types, with the superseded `SyncOrchestrator`, the duplicate converter in `topup_gaps.py`, and dead flexibility deleted.

**Architecture:** `SyncResult` and `series_to_frame` move from `historical_sync.py` into `simple_sync.py` (the only entry point). `SyncOrchestrator` and its benchmark are deleted — they measure a superseded design and only their own tests keep them alive. `topup_gaps.py` drops its private converter and hand-rolled batch loop and calls `simple_sync` like every other caller. Inside `simple_sync`, gap planning derives from `gaps is not None` (all four callers already always pass a detector), the IPO/error classification flattens to one check, and batching uses `itertools.batched` (runtime is 3.13).

**Tech Stack:** Python 3.13, pandas, `ParallelHistoryFetcher`, `GapDetector`, `ParquetStorage`, pytest, ruff, mypy.

**Source spec:** ponytail-review findings of 2026-09-21 on `simple_sync.py` / `historical_sync.py` / `fill_gaps.py` / `repair_gaps.py` / `topup_gaps.py` (net: −248 lines possible).

## Global Constraints

- No behavior change to the fill contract: IPO (no data) = skip; 429/network = failed. The fetch always goes through `ParallelHistoryFetcher`; failover brokers reach it.
- Storage contract untouched: tz-naive IST, `upsert` dedup on `(symbol, timeframe, timestamp)`.
- CLI surface is not broken: `tradex sync --no-skip-existing` keeps its flag (it maps to `gaps=None`).
- Do not run any `trading/scripts/*` against live brokers during verification — tests are offline mocks only.
- Work in a dedicated branch/worktree (main currently carries unrelated dirty frontend/runtime files). Use `superpowers:using-git-worktrees` at execution time.
- Commit style: conventional commits, lowercase scope, as in `git log` (`refactor(datalake): …`).

---

### Task 1: Move `SyncResult` + `series_to_frame` + `_bar_freq` into `simple_sync.py`

The superseded module stops hosting the active path's types. Interim state: `historical_sync.py` imports the two names from `simple_sync` (one direction, no cycle) and keeps only `SyncOrchestrator` until Task 3 deletes it.

**Files:**
- Modify: `trading/src/tradex_trading/datalake/simple_sync.py`
- Modify: `trading/src/tradex_trading/datalake/historical_sync.py`
- Modify: `trading/src/tradex_trading/datalake/__init__.py`
- Modify: `trading/scripts/backfill_2025.py:56`, `trading/scripts/reconcile_bars.py:59`
- Test: `trading/tests/datalake/test_simple_sync.py` (gain `TestSeriesToFrame`), `trading/tests/datalake/test_historical_sync.py` (lose it)

**Interfaces:**
- Consumes: `HistoricalSeries`, `Timeframe` (unchanged).
- Produces: `tradex_trading.datalake.simple_sync.SyncResult` (dataclass: `requested: int, fetched: int, written: int, failed: list[str]`), `series_to_frame(series: HistoricalSeries, symbol: str) -> pd.DataFrame`, `_bar_freq(timeframe: Timeframe | str) -> str`. `tradex_trading.datalake` package re-exports all three from `simple_sync`.

- [ ] **Step 1: Move the converter tests first (RED)**

In `trading/tests/datalake/test_simple_sync.py`, add after the existing imports:

```python
import pandas as pd
from tradex_trading.datalake.simple_sync import series_to_frame
```

and append at the end of the file (tests moved verbatim from `test_historical_sync.py`, import retargeted):

```python
class TestSeriesToFrame:
    def test_converts_candles_to_storage_columns(self):
        df = series_to_frame(_series(n=3), "SYM0")
        assert list(df.columns) == [
            "symbol", "exchange", "kind", "timeframe",
            "timestamp", "open", "high", "low", "close", "volume",
        ]
        assert len(df) == 3
        assert df["symbol"].eq("SYM0").all()
        assert df["timestamp"].dt.tz is None

    def test_empty_series_gives_empty_frame(self):
        empty = HistoricalSeries(
            instrument=INSTRUMENTS[0], timeframe=Timeframe.M1,
            candles=[], start=BASE, end=BASE,
        )
        assert series_to_frame(empty, "SYM0").empty

    def test_aware_non_utc_converts_not_relabels(self):
        # 14:45 IST aware == 09:15 UTC; old replace() bug shifted it +5:30
        from datetime import timezone, timedelta as td
        ist = timezone(td(hours=5, minutes=30))
        src = _series(n=1).candles[0]
        candle = Candle(
            instrument=src.instrument, timeframe=src.timeframe,
            ohlc=src.ohlc, volume=src.volume,
            timestamp=datetime(2026, 8, 3, 14, 45, tzinfo=ist),
        )
        df = series_to_frame(
            HistoricalSeries(instrument=INSTRUMENTS[0], timeframe=Timeframe.M1,
                             candles=[candle], start=BASE, end=BASE), "SYM0")
        assert df["timestamp"].iloc[0] == pd.Timestamp("2026-08-03 14:45:00")
```

Run: `uv run pytest trading/tests/datalake/test_simple_sync.py -q`
Expected: FAIL — `ImportError: cannot import name 'series_to_frame' from 'tradex_trading.datalake.simple_sync'`

- [ ] **Step 2: Move the code (GREEN)**

In `simple_sync.py`:

1. Replace the import line `from tradex_trading.datalake.historical_sync import SyncResult, series_to_frame` with nothing (definitions move in).
2. Extend the module docstring's closing paragraph with: `SyncResult and series_to_frame moved here from historical_sync.py (2026-09-21): this is the only sync entry point, so the shared types live with it.`
3. Add imports `from dataclasses import dataclass` and `from tradex_domain.market import HistoricalSeries`.
4. Insert below `log = logging.getLogger(__name__)` (bodies moved verbatim from `historical_sync.py`):

```python
@dataclass
class SyncResult:
    requested: int
    fetched: int
    written: int
    failed: list[str]


def series_to_frame(series: HistoricalSeries, symbol: str) -> pd.DataFrame:
    """Convert HistoricalSeries to storage DataFrame (tz-naive IST)."""
    if not series.candles:
        return pd.DataFrame()
    from tradex_domain.market_calendar import to_ist_naive

    candles = series.candles
    timestamps = [
        to_ist_naive(c.timestamp if c.timestamp.tzinfo is not None
                     else c.timestamp.replace(tzinfo=UTC))
        for c in candles
    ]
    return pd.DataFrame({
        "symbol": symbol,
        "exchange": [c.instrument.exchange.value if hasattr(c.instrument, "exchange") else "NSE"
                     for c in candles],
        "kind": "equity",
        "timeframe": str(series.timeframe.value),
        "timestamp": timestamps,
        "open": [float(c.ohlc.open.value) for c in candles],
        "high": [float(c.ohlc.high.value) for c in candles],
        "low": [float(c.ohlc.low.value) for c in candles],
        "close": [float(c.ohlc.close.value) for c in candles],
        "volume": [float(c.volume.value) if c.volume else 0.0 for c in candles],
    })


def _bar_freq(timeframe: Timeframe | str) -> str:
    """Map a Timeframe to a GapDetector bar_freq."""
    tf = str(timeframe.value if isinstance(timeframe, Timeframe) else timeframe)
    return {"1m": "1min", "5m": "5min", "15m": "15min"}.get(tf, "1min")
```

5. Add `from datetime import UTC` to the existing `from datetime import datetime` line.

In `historical_sync.py`: delete the `SyncResult` dataclass, `series_to_frame`, and `_bar_freq` definitions (lines 25–71) and replace them with:

```python
from tradex_trading.datalake.simple_sync import SyncResult, series_to_frame  # noqa: F401 — re-export for SyncOrchestrator until deletion
```

In `trading/src/tradex_trading/datalake/__init__.py`, replace the two import lines:

```python
from tradex_trading.datalake.historical_sync import SyncOrchestrator
from tradex_trading.datalake.simple_sync import SyncResult, series_to_frame, simple_sync
```

(`__all__` gains `"series_to_frame"`; `SyncResult`/`SyncOrchestrator` entries unchanged.)

In `trading/scripts/backfill_2025.py` and `trading/scripts/reconcile_bars.py`, change:

```python
from tradex_trading.datalake.historical_sync import series_to_frame  # noqa: E402
```

to:

```python
from tradex_trading.datalake.simple_sync import series_to_frame  # noqa: E402
```

In `test_historical_sync.py`: delete the `TestSeriesToFrame` class and drop `series_to_frame` from its import (keep `SyncOrchestrator`).

- [ ] **Step 3: Run tests**

Run: `uv run pytest trading/tests/datalake/ trading/tests/scripts/ -q`
Expected: PASS (all 171 + script tests; the moved 3 tests now run under `test_simple_sync.py`).

- [ ] **Step 4: Commit**

```bash
git add trading/src/tradex_trading/datalake/simple_sync.py trading/src/tradex_trading/datalake/historical_sync.py trading/src/tradex_trading/datalake/__init__.py trading/scripts/backfill_2025.py trading/scripts/reconcile_bars.py trading/tests/datalake/test_simple_sync.py trading/tests/datalake/test_historical_sync.py
git commit -m "refactor(datalake): move SyncResult and series_to_frame into simple_sync"
```

---

### Task 2: Simplify `simple_sync` contracts; retarget callers

Four cuts from the review in one pass (they interlock): drop `skip_existing` (derived from `gaps`), drop the `hasattr` symbol fallback and the dead `df.empty` check, flatten the IPO/error classification, use `itertools.batched`, shrink `_plan_gaps`.

Equivalence note for the reviewer: the old two-stage filter (`no_data_ids` set + `real_errors` list + per-inst `any()`) and the new single check agree on every real `ParallelHistoryFetcher` output (an empty series is reported as an *error*, never as a results entry with zero candles). They differ only for a mock that returns a present-but-empty series *and* an "empty"-classified error string — the new code skips it, consistently with the IPO contract. The new pin test below locks that in.

**Files:**
- Modify: `trading/src/tradex_trading/datalake/simple_sync.py`
- Modify: `trading/scripts/fill_gaps.py:160`, `trading/scripts/repair_gaps.py:215`, `trading/scripts/sync_today.py:59`, `trading/src/tradex_trading/interface/cli.py:504-513`
- Modify: `trading/scripts/repair_gaps.py:86-87` (`_chunks` helper)
- Test: `trading/tests/datalake/test_simple_sync.py`

**Interfaces:**
- Consumes: `GapDetector.detect(instruments, start, end, timeframe, bar_freq) -> list[(inst, ranges)]` (confirmed signature, `gap_detector.py:172`).
- Produces: `simple_sync(broker, store, instruments, timeframe, start, end, *, batch_size=20, max_workers=5, gaps=None, failover_brokers=None) -> SyncResult`. **`skip_existing` parameter is removed** — passing it raises `TypeError`, which is how stale callers are caught.

- [ ] **Step 1: Rewrite the contract tests (RED)**

In `test_simple_sync.py`, replace `test_skip_existing_with_no_gaps_fetches_nothing` with the following four tests:

```python
    def test_gaps_short_circuit_before_fetching(self) -> None:
        """A complete lake (detector returns []) short-circuits before fetching."""
        gaps = MagicMock()
        gaps.detect.return_value = []
        store = _store()
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            result = simple_sync(
                MagicMock(), store, INSTRUMENTS, "1m", START, END, gaps=gaps,
            )
        assert result.requested == 3
        assert result.fetched == 0
        phf.assert_not_called()
        store.upsert.assert_not_called()

    def test_no_gaps_detector_fetches_everything(self) -> None:
        """gaps=None means a full fetch — no detector, no skip logic."""
        results = {str(i.instrument_id): _series(instrument=i) for i in INSTRUMENTS}
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            phf.return_value = _patched_fetcher(results, [])
            result = simple_sync(MagicMock(), _store(10), INSTRUMENTS, "1m", START, END)
        assert result.fetched == 3

    def test_present_but_empty_series_is_skipped(self) -> None:
        """A present-but-empty series is an IPO skip, never a failure."""
        results = {
            str(INSTRUMENTS[0].instrument_id): _series(n=0, instrument=INSTRUMENTS[0])
        }
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            phf.return_value = _patched_fetcher(results, [])
            result = simple_sync(MagicMock(), _store(10), INSTRUMENTS, "1m", START, END)
        assert result.failed == []

    def test_partial_gaps_fetch_only_ranged_symbols(self) -> None:
        """Detector hits pass both the symbol filter and the ranges through."""
        results = {str(INSTRUMENTS[0].instrument_id): _series(instrument=INSTRUMENTS[0])}
        gaps = MagicMock()
        gaps.detect.return_value = [(INSTRUMENTS[0], [(START, END)])]
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            phf.return_value = _patched_fetcher(results, [])
            result = simple_sync(
                MagicMock(), _store(10), INSTRUMENTS, "1m", START, END, gaps=gaps,
            )
        assert result.fetched == 1
        _, kw = phf.return_value.fetch.call_args
        assert kw.get("ranges") == {str(INSTRUMENTS[0].instrument_id): [(START, END)]}
```

(`_series` already accepts `n=0`; `pandas` import from Task 1 is reused.)

Run: `uv run pytest trading/tests/datalake/test_simple_sync.py -q`
Expected: FAIL — `TypeError: simple_sync() got an unexpected keyword argument 'gaps'` is NOT expected here; the failures are `test_gaps_short_circuit_before_fetching` and `test_partial_gaps_fetch_only_ranged_symbols` fetching everything (old `skip_existing=False` default) — assertion errors.

- [ ] **Step 2: Rewrite `simple_sync` (GREEN)**

Replace the whole body of `simple_sync` between the docstring and `_plan_gaps` with:

```python
def simple_sync(
    broker: Any,
    store: Any,
    instruments: list[Any],
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    batch_size: int = 20,
    max_workers: int = 5,
    gaps: Any = None,
    failover_brokers: dict[str, Any] | None = None,
) -> SyncResult:
    """Fetch in parallel, convert, store.

    One flat executor per batch: concurrency is exactly *max_workers*, never
    chunks*workers, so the rate-limiter bucket is not drained. No-data (a
    recent IPO) is silently skipped — only real errors (429/network) are
    failed.

    A ``gaps`` detector plans the run: complete symbols are skipped and
    partial ones fetch only their missing ranges. ``failover_brokers`` lets
    a caller hand in the other live brokers; an uncovered tail or a dead
    primary is then served from whichever broker can.
    """
    tf = Timeframe(timeframe) if isinstance(timeframe, str) else timeframe
    if not instruments:
        return SyncResult(0, 0, 0, [])

    to_fetch = instruments
    ranges: dict[str, list[tuple[datetime, datetime]]] | None = None
    if gaps is not None:
        to_fetch, ranges = _plan_gaps(instruments, tf, start, end, gaps)
        if not to_fetch:
            log.info("simple_sync: all %d symbols complete — nothing to fetch", len(instruments))
            return SyncResult(len(instruments), 0, 0, [])

    # The single fetch path: primary broker, plus any others for failover and
    # clipped-tail repair. Both are invisible to the caller, which still sees
    # (results, errors) — but the errors now exclude tails another broker filled.
    brokers: dict[str, Any] = {"primary": broker}
    if failover_brokers:
        brokers.update(failover_brokers)
    fetcher = ParallelHistoryFetcher(brokers, max_workers=max_workers)

    fetched = written = 0
    failed: list[str] = []
    for bi, batch in enumerate(batched(to_fetch, batch_size), 1):
        log.info("simple_sync: batch %d (%d symbols)", bi, len(batch))

        results, fetch_errors = fetcher.fetch(
            list(batch), tf, start, end, ranges=ranges,
        )

        frames: list[pd.DataFrame] = []
        for inst in batch:
            inst_id = str(inst.instrument_id)
            series = results.get(inst_id)
            if series is None or not series.candles:
                # No entry = unlisted (IPO) when the only complaint is an
                # "empty" series; 429/network failures are real failures.
                if any(inst_id in e and "empty" not in e for e in fetch_errors):
                    failed.append(inst.symbol)
                else:
                    log.debug("simple_sync: no data for %s (skipped)", inst.symbol)
                continue
            frames.append(series_to_frame(series, inst.symbol))
            fetched += 1

        if frames:
            written += store.upsert(pd.concat(frames, ignore_index=True))

        log.info("simple_sync: batch %d done (fetched=%d written=%d failed=%d)",
                 bi, fetched, written, len(failed))

    if failed:
        log.warning("simple_sync: %d failed: %s", len(failed), failed[:5])
    log.info("simple_sync: %d/%d fetched, %d rows written", fetched, len(instruments), written)

    return SyncResult(len(instruments), fetched, written, failed)


def _plan_gaps(
    instruments: list[Any],
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    gaps: Any,
) -> tuple[list[Any], dict[str, list[tuple[datetime, datetime]]] | None]:
    """Detect gaps and return (to_fetch, ranges)."""
    gap_results = gaps.detect(
        instruments, start, end,
        timeframe=str(timeframe.value), bar_freq=_bar_freq(timeframe),
    )
    to_fetch = [inst for inst, inst_ranges in gap_results if inst_ranges]
    ranges = {str(inst.instrument_id): r for inst, r in gap_results if r}
    return to_fetch, ranges or None
```

Delete the old `_plan_gaps` and the old batch/error blocks. Add `from itertools import batched` to the imports. Update the docstring line "One flat executor per batch" stays as shown above.

- [ ] **Step 3: Retarget the four callers**

`trading/scripts/fill_gaps.py` (in the cluster loop):

```python
        result = simple_sync(
            primary, store, insts, "1m", c_start, c_end,
            gaps=detector,
            failover_brokers=failover or None,
        )
```

`trading/scripts/repair_gaps.py` (in the chunk loop) — same change, plus delete the `_chunks` helper (lines 86-87) and change `for chunk in _chunks(insts, args.chunk):` to:

```python
        for chunk in batched(insts, args.chunk):
```

with `from itertools import batched` added to the stdlib import block.

`trading/scripts/sync_today.py`:

```python
    result = simple_sync(
        primary, store, instruments, "1m", day_start, now,
        gaps=GapDetector(store),
        failover_brokers=failover or None,
    )
```

`trading/src/tradex_trading/interface/cli.py` — keep the `--no-skip-existing` flag and the `gaps = GapDetector(store) if args.skip_existing else None` line exactly; only remove the `skip_existing=args.skip_existing,` kwarg from the `simple_sync(...)` call (the flag now maps to `gaps=None`, which is the same full-fetch behavior).

- [ ] **Step 4: Run tests**

Run: `uv run pytest trading/tests/datalake/ trading/tests/scripts/ trading/tests/interface/test_interface_modules.py -q`
Expected: PASS — parser tests (`test_parser_sync_no_skip_existing` etc.) still pass because the flag itself is unchanged.

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/datalake/simple_sync.py trading/scripts/fill_gaps.py trading/scripts/repair_gaps.py trading/scripts/sync_today.py trading/src/tradex_trading/interface/cli.py trading/tests/datalake/test_simple_sync.py
git commit -m "refactor(datalake): simple_sync derives gap planning from gaps, drops dead flexibility"
```

---

### Task 3: Delete `SyncOrchestrator`, `historical_sync.py`, and the benchmark

The orchestrator is kept alive only by its own tests and a benchmark that measures the superseded design (mocked 20µs fetches — no real signal). `git history` preserves both if perf numbers are ever needed again.

**Files:**
- Delete: `trading/src/tradex_trading/datalake/historical_sync.py`
- Delete: `trading/tests/datalake/test_historical_sync.py` (only `TestSyncFacade` remains after Task 1)
- Delete: `benchmarks/bench_historical_sync.py`
- Delete: `tests/test_benchmark_historical_sync.py`
- Modify: `trading/src/tradex_trading/datalake/__init__.py`

**Interfaces:**
- Consumes: nothing (Task 1 moved the shared types out; nothing outside `.kilo/worktrees/*` imports `SyncOrchestrator` — confirmed by grep).
- Produces: package exports `SyncResult`, `series_to_frame`, `simple_sync` only.

- [ ] **Step 1: Verify no live references**

Run: `rg -n "SyncOrchestrator|historical_sync" --glob '!.kilo/**' --glob '!docs/**' --glob '!*.html'`
Expected: only `trading/src/tradex_trading/datalake/__init__.py` and `trading/src/tradex_trading/datalake/historical_sync.py` itself. If any other hit appears, stop and extend this task before deleting.

- [ ] **Step 2: Delete and re-export**

```bash
rm trading/src/tradex_trading/datalake/historical_sync.py trading/tests/datalake/test_historical_sync.py benchmarks/bench_historical_sync.py tests/test_benchmark_historical_sync.py
```

In `__init__.py`, drop the `SyncOrchestrator` import and its `__all__` entry.

- [ ] **Step 3: Run tests**

Run: `uv run pytest trading/tests/datalake/ trading/tests/scripts/ -q`
Expected: PASS with no collection errors (proves no hidden import of the deleted module).

- [ ] **Step 4: Commit**

```bash
git add -A trading/src/tradex_trading/datalake trading/tests/datalake benchmarks/bench_historical_sync.py tests/test_benchmark_historical_sync.py
git commit -m "refactor(datalake): delete superseded SyncOrchestrator, its facade tests and benchmark"
```

---

### Task 4: Route `topup_gaps.py` through `simple_sync`

Removes the private `_series_to_frame` re-roll and the hand-rolled batch/fetch/upsert loop — the exact loop `simple_sync` owns. Behavior preserved: filler broker serves the gapped spans, `--workers` and `--min-gap-stamps` still respected, gap scan still gates the run.

**Files:**
- Modify: `trading/scripts/topup_gaps.py`
- Test: `trading/tests/scripts/` (existing coverage must stay green)

**Interfaces:**
- Consumes: `simple_sync(broker, store, instruments, timeframe, start, end, *, gaps, max_workers) -> SyncResult` (from Task 2).
- Produces: same CLI surface; log line changes from "Top-up complete: N rows across M batches" to "Top-up complete: N rows written, F failed".

- [ ] **Step 1: Rewrite the fill tail (RED is not reachable offline — this script has no unit test of its own; the gate is the existing script-import tests plus a compile check)**

In `topup_gaps.py`:

1. Delete `_series_to_frame` (lines 52-73) and the imports that die with it: `pandas as pd`, `ParallelHistoryFetcher`, `Timeframe` from `tradex_domain`.
2. Replace the section from `filler.connect()` to the final log lines with:

```python
    filler = build_broker_from_env(args.filler)
    filler.connect()
    targets = [instruments[inst.symbol] for inst, _ in gaps]
    result = simple_sync(
        filler, store, targets, args.timeframe, start, end,
        gaps=detector, max_workers=args.workers,
    )
    log.info("=" * 60)
    log.info("Top-up complete: %d rows written, %d failed: %s",
             result.written, len(result.failed), result.failed[:5])
    return 0
```

3. Add the import alongside the other datalake imports: `from tradex_trading.datalake.simple_sync import simple_sync  # noqa: E402 — sys.path setup above`.

Note for the reviewer: `simple_sync` re-detects with detector defaults (`min_gap_stamps=1`, no holiday grid) to scope the fetch ranges, exactly as `fill_gaps`/`repair_gaps` already do after their own tuned scan. The pre-scan with `--min-gap-stamps`/`--include-open-stamps` remains the gate for *which symbols* run; range scoping comes from the second detect. Same pattern, one fill path.

- [ ] **Step 2: Verify**

Run: `uv run python -m py_compile trading/scripts/topup_gaps.py && uv run pytest trading/tests/scripts/ trading/tests/datalake/ -q`
Expected: compile OK, tests PASS.

- [ ] **Step 3: Commit**

```bash
git add trading/scripts/topup_gaps.py
git commit -m "refactor(scripts): topup_gaps fills through simple_sync instead of a private loop"
```

---

### Task 5: Full verification and LOC accounting

**Files:** none (verification only).

- [ ] **Step 1: Full relevant suites**

Run: `uv run pytest trading/tests/datalake/ trading/tests/scripts/ trading/tests/interface/test_interface_modules.py -q`
Expected: PASS (baseline before this plan: 171 datalake + script tests).

- [ ] **Step 2: Lint and types**

Run: `uv run ruff check trading/src/tradex_trading/datalake/ trading/scripts/topup_gaps.py trading/scripts/fill_gaps.py trading/scripts/repair_gaps.py trading/scripts/sync_today.py trading/src/tradex_trading/interface/cli.py`
Run: `uv run mypy trading/src/tradex_trading/datalake/simple_sync.py`
Expected: no new findings vs. main (pre-existing warnings unchanged).

- [ ] **Step 3: LOC accounting**

Run: `git diff --stat main`
Expected: clearly negative net for the touched Python files (plan target ≈ −200; review ceiling −248). Record the number in the commit-free final report.

- [ ] **Step 4: Report**

Summarize: tasks done, tests green, net LOC, any deviations from the plan (executing-plans requires stopping and asking rather than improvising).
