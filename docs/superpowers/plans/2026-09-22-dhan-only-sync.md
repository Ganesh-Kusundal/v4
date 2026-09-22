# Dhan-only Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make datalake sync Dhan-only: no failover, no clip-merge, caller-owned `ranges=`, visible `skipped`.

**Architecture:** Slim `ParallelHistoryFetcher` to one-broker chunked fetch. `simple_sync` drops `gaps=` / `failover_brokers`, takes `ranges=`, returns `skipped`. Scripts/CLI scan once and pass ranges.

**Tech Stack:** Python 3.13, pytest, existing `GapDetector` / `ParquetStorage`.

**Spec:** `docs/superpowers/specs/2026-09-22-dhan-only-sync-design.md`

## Global Constraints

- One broker key in the fetcher dict (`"dhan"` in production).
- Sync never re-detects gaps; callers pass `ranges=` or omit for full window.
- Empty series → `skipped` (exit 0); 429/network → `failed` (exit 1).
- Dhan ~15:14 ceiling is not a sync error; Upstox repair is out of scope.
- Do not run live broker scripts during verification.
- Pytest: `.venv/bin/python -m pytest <paths> -p no:cacheprovider --import-mode=importlib -c pyproject.toml`

## File map

| File | Role |
|---|---|
| `trading/src/tradex_trading/datalake/simple_sync.py` | Entry: `ranges=`, `skipped`, no gaps/failover |
| `trading/src/tradex_trading/datalake/parallel_fetcher.py` | Chunk + concurrency only |
| `trading/tests/datalake/test_simple_sync.py` | Rewrite contracts |
| `trading/tests/datalake/test_parallel_fetcher.py` | Drop failover/clip tests; keep chunk |
| `trading/scripts/{fill,repair,sync_today,topup}_gaps.py` | Dhan-only + ranges |
| `trading/src/tradex_trading/interface/cli.py` | `cmd_sync` Dhan + ranges |

---

### Task 1: `simple_sync` — ranges + skipped

**Files:**
- Modify: `trading/src/tradex_trading/datalake/simple_sync.py`
- Modify: `trading/tests/datalake/test_simple_sync.py`

**Interfaces:**
- Produces: `SyncResult(requested, fetched, written, failed, skipped)`
- Produces: `simple_sync(broker, store, instruments, timeframe, start, end, *, batch_size=20, max_workers=5, ranges=None) -> SyncResult`
- Consumes: `ParallelHistoryFetcher({"dhan": broker}, max_workers=...)` (key always `"dhan"`)

- [ ] **Step 1: Rewrite failing tests**

Replace failover / `gaps=` tests. Keep series_to_frame tests. New body for `TestSimpleSync`:

```python
class TestSimpleSync:
    def test_empty_universe_requests_nothing(self) -> None:
        store = _store()
        result = simple_sync(MagicMock(), store, [], "1m", START, END)
        assert result == SyncResult(0, 0, 0, [], [])
        store.upsert.assert_not_called()

    def test_syncs_all_symbols(self) -> None:
        results = {str(i.instrument_id): _series(instrument=i) for i in INSTRUMENTS}
        with patch("tradex_trading.datalake.simple_sync.ParallelHistoryFetcher") as phf:
            phf.return_value = _patched_fetcher(results, [])
            result = simple_sync(MagicMock(), _store(10), INSTRUMENTS, "1m", START, END)
        assert result.fetched == 3
        assert result.failed == []
        assert result.skipped == []
        brokers = phf.call_args.args[0]
        assert list(brokers.keys()) == ["dhan"]

    def test_uses_parallel_history_fetcher(self) -> None:
        results = {str(i.instrument_id): _series(instrument=i) for i in INSTRUMENTS}
        with patch("tradex_trading.datalake.simple_sync.ParallelHistoryFetcher") as phf:
            phf.return_value = _patched_fetcher(results, [])
            simple_sync(MagicMock(), _store(10), INSTRUMENTS, "1m", START, END)
        assert phf.call_args.kwargs["max_workers"] == 5

    def test_ranges_passed_to_fetcher(self) -> None:
        results = {str(INSTRUMENTS[0].instrument_id): _series(instrument=INSTRUMENTS[0])}
        ranges = {str(INSTRUMENTS[0].instrument_id): [(START, END)]}
        with patch("tradex_trading.datalake.simple_sync.ParallelHistoryFetcher") as phf:
            phf.return_value = _patched_fetcher(results, [])
            result = simple_sync(
                MagicMock(), _store(10), [INSTRUMENTS[0]], "1m", START, END,
                ranges=ranges,
            )
        assert result.fetched == 1
        _, kw = phf.return_value.fetch.call_args
        assert kw.get("ranges") == ranges

    def test_empty_is_skipped_not_failed(self) -> None:
        empty_err = [
            f"{INSTRUMENTS[0].instrument_id}: empty series for {INSTRUMENTS[0].instrument_id}"
        ]
        with patch("tradex_trading.datalake.simple_sync.ParallelHistoryFetcher") as phf:
            phf.return_value = _patched_fetcher({}, empty_err)
            result = simple_sync(MagicMock(), _store(10), [INSTRUMENTS[0]], "1m", START, END)
        assert result.failed == []
        assert INSTRUMENTS[0].symbol in result.skipped

    def test_transient_failure_lands_in_failed(self) -> None:
        err = [f"{INSTRUMENTS[0].instrument_id}: 429 rate limit"]
        with patch("tradex_trading.datalake.simple_sync.ParallelHistoryFetcher") as phf:
            phf.return_value = _patched_fetcher({}, err)
            result = simple_sync(MagicMock(), _store(10), [INSTRUMENTS[0]], "1m", START, END)
        assert INSTRUMENTS[0].symbol in result.failed
        assert result.skipped == []

    def test_present_but_empty_series_is_skipped(self) -> None:
        results = {
            str(INSTRUMENTS[0].instrument_id): _series(n=0, instrument=INSTRUMENTS[0])
        }
        with patch("tradex_trading.datalake.simple_sync.ParallelHistoryFetcher") as phf:
            phf.return_value = _patched_fetcher(results, [])
            result = simple_sync(MagicMock(), _store(10), [INSTRUMENTS[0]], "1m", START, END)
        assert result.failed == []
        assert INSTRUMENTS[0].symbol in result.skipped
```

Import `SyncResult` from `simple_sync`. Delete `test_failover_*`, `test_gaps_*`, `test_no_gaps_detector_*`, `test_partial_gaps_*`, `test_no_data_is_skipped_*` (replaced).

- [ ] **Step 2: Run tests — expect FAIL**

```bash
.venv/bin/python -m pytest trading/tests/datalake/test_simple_sync.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q
```

Expected: FAIL (`skipped` missing / `failover_brokers` / `gaps=`).

- [ ] **Step 3: Implement `simple_sync`**

Replace module with (keep `series_to_frame` / `_bar_freq` as-is if unused delete `_bar_freq` and `_plan_gaps`):

```python
@dataclass
class SyncResult:
    requested: int
    fetched: int
    written: int
    failed: list[str]
    skipped: list[str]


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
    ranges: dict[str, list[tuple[datetime, datetime]]] | None = None,
) -> SyncResult:
    tf = Timeframe(timeframe) if isinstance(timeframe, str) else timeframe
    if not instruments:
        return SyncResult(0, 0, 0, [], [])

    fetcher = ParallelHistoryFetcher({"dhan": broker}, max_workers=max_workers)
    fetched = written = 0
    failed: list[str] = []
    skipped: list[str] = []
    for bi, batch in enumerate(batched(instruments, batch_size), 1):
        log.info("simple_sync: batch %d (%d symbols)", bi, len(batch))
        results, fetch_errors = fetcher.fetch(
            list(batch), tf, start, end, ranges=ranges,
        )
        frames: list[pd.DataFrame] = []
        for inst in batch:
            inst_id = str(inst.instrument_id)
            series = results.get(inst_id)
            if series is None or not series.candles:
                if any(inst_id in e and "empty" not in e for e in fetch_errors):
                    failed.append(inst.symbol)
                else:
                    skipped.append(inst.symbol)
                    log.debug("simple_sync: no data for %s (skipped)", inst.symbol)
                continue
            frames.append(series_to_frame(series, inst.symbol))
            fetched += 1
        if frames:
            written += store.upsert(pd.concat(frames, ignore_index=True))
    if failed:
        log.warning("simple_sync: %d failed: %s", len(failed), failed[:5])
    if skipped:
        log.info("simple_sync: %d skipped (no data)", len(skipped))
    return SyncResult(len(instruments), fetched, written, failed, skipped)


__all__ = ["SyncResult", "simple_sync", "series_to_frame"]
```

Delete `_plan_gaps`, `_bar_freq`, `gaps`, `failover_brokers`. Update module docstring to match spec.

- [ ] **Step 4: Run tests — expect PASS**

Same pytest command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/datalake/simple_sync.py trading/tests/datalake/test_simple_sync.py
git commit -m "refactor(datalake): simple_sync is Dhan-only with ranges and skipped"
```

---

### Task 2: Slim `ParallelHistoryFetcher`

**Files:**
- Modify: `trading/src/tradex_trading/datalake/parallel_fetcher.py`
- Modify: `trading/tests/datalake/test_parallel_fetcher.py`

**Interfaces:**
- Produces: `fetch(...)` still returns `(dict[str, HistoricalSeries], list[str])`
- No failover; empty raises; single broker serves all instruments

- [ ] **Step 1: Delete dead multi-broker paths**

Remove: `_clipped_tail`, `_merge_candles`, `_BrokerHealth`, `BROKER_HEALTH_THRESHOLD`, `_complete_shortfall`, failover loop in `_fetch_one`, multi-broker `_split` assignment (assign all instruments to the sole broker).

`_try_broker` becomes: windows → stitch or single `history` → return series (raise if empty). No shortfall call.

Update module docstring: single-broker chunked concurrent fetch.

Keep: `_chunk_cap_for`, `_stitch_windows`, `_maybe_acquire`, `fetch_with_backoff`, rate limiters, `ERROR_LOG_CAP`.

- [ ] **Step 2: Trim tests**

Delete classes/tests for failover fan-out, clipped tail, merge candles, multi-broker split. Keep chunking, empty raise, rate-limit gate, single-broker happy path.

- [ ] **Step 3: Run tests**

```bash
.venv/bin/python -m pytest trading/tests/datalake/test_parallel_fetcher.py trading/tests/datalake/test_simple_sync.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add trading/src/tradex_trading/datalake/parallel_fetcher.py trading/tests/datalake/test_parallel_fetcher.py
git commit -m "refactor(datalake): strip failover and clip-merge from ParallelHistoryFetcher"
```

---

### Task 3: Callers — Dhan-only + ranges

**Files:**
- Modify: `trading/scripts/fill_gaps.py`
- Modify: `trading/scripts/repair_gaps.py`
- Modify: `trading/scripts/sync_today.py`
- Modify: `trading/scripts/topup_gaps.py`
- Modify: `trading/src/tradex_trading/interface/cli.py` (`cmd_sync`)

**Helper pattern** (inline, no new module):

```python
def _ranges_from_gaps(gap_pairs) -> dict[str, list[tuple[datetime, datetime]]]:
    return {str(inst.instrument_id): r for inst, r in gap_pairs if r}
```

Each script:
1. Build Dhan only (`build_broker_from_env("dhan")`); no Dhan → return 1.
2. After scan, `insts = [i for i, r in gaps if r]`, `ranges = _ranges_from_gaps(gaps)`.
3. `simple_sync(dhan, store, insts, "1m", c_start, c_end, ranges=ranges)` — no `gaps=`, no `failover_brokers`.
4. Log `result.skipped` if any.

`cmd_sync`:
- Prefer `brokers["dhan"]`; no dhan → error exit 1 (unless `--dry-run` paper).
- If `args.skip_existing`: `scan = GapDetector(store).scan(instruments, start, end, timeframe=..., bar_freq=...)`; filter + `ranges=`; else `ranges=None`.
- Print skipped count; exit 1 iff `result.failed`.

- [ ] **Step 1: Update scripts + CLI**
- [ ] **Step 2: Grep for leftover `failover_brokers` / `gaps=` into simple_sync**

```bash
rg "failover_brokers|gaps=detector|gaps=GapDetector|gaps=gaps" trading --glob '*.py'
```

Expected: no simple_sync call sites with those kwargs.

- [ ] **Step 3: Run datalake + interface smoke**

```bash
.venv/bin/python -m pytest trading/tests/datalake/ trading/tests/interface/test_interface_modules.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q
```

Expected: PASS (or fix call-site breakage only).

- [ ] **Step 4: Commit**

```bash
git add trading/scripts/fill_gaps.py trading/scripts/repair_gaps.py trading/scripts/sync_today.py trading/scripts/topup_gaps.py trading/src/tradex_trading/interface/cli.py
git commit -m "refactor(datalake): scripts and CLI sync Dhan-only with caller ranges"
```

---

## Spec coverage check

| Spec item | Task |
|---|---|
| Drop failover_brokers | 1, 3 |
| Drop gaps= re-detect | 1, 3 |
| SyncResult.skipped | 1 |
| Slim fetcher (no clip/failover/split) | 2 |
| Callers Dhan-only + ranges | 3 |
| Upstox repair | out of scope (spec) |
| Tests listed in spec | 1, 2 |
