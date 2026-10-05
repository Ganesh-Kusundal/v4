# SyncService Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fragile "scripts call `simple_sync` and hope" flow with one bounded, resumable `SyncService` (scan → plan → sync → validate coverage → converge) so `tradex sync`, `repair_gaps`, `fill_gaps` all run through a single robust path that cannot hang and does retry rate limits.

> **Status (2026-10-05): DONE.** `SyncService` is the path for `tradex sync`, `repair_gaps`, and `fill_gaps` (`205f589`, `6ba2b9c`); `backfill_parquet` is deliberately left multi-broker. One caveat survives the goal's wording: "cannot hang" is bounded for *retries*, but a blocked broker socket still leaves a worker thread running (`_fetch_bounded`), so the process can hang on exit. See Hardening below.

**Architecture:** `SyncService` composes the existing `GapDetector` (detection), `ParallelHistoryFetcher` + `fetch_with_backoff` (fetch), and `ParquetStorage` (store). A total wall-clock `deadline` is threaded through every retry so a run is bounded by `budget_seconds`. A post-fetch coverage check rejects truncated responses. `converge()` re-scans until clean or the budget is spent. The scripts/CLI become thin wrappers.

**Tech Stack:** Python 3.12, pandas, pytest, `tradex_market_data` (`GapDetector`, `ParallelHistoryFetcher`, `ParquetStorage`, `simple_sync`).

---

## ⚠️ Reconciliation (2026-10-05) — read before executing any task

**Tasks 1–3 have already shipped, and the shipped code is the source of truth — not the code blocks below.** This plan was written before implementation; three of its code blocks are stale and one is *regressive*. Re-derive from the tree, not from this document.

Shipped commits (all on `main`):

```
de0fecc feat(market_data): configurable, deadline-bounded backoff in fetch_with_backoff
196326a feat(market_data): SyncService core with bounded backoff and coverage validation
560d8a7 fix(market_data): coverage validation compares IST-naive, not raw UTC
889f2e6 feat(market_data): SyncService converge and tail-repair
```

`25 passed` on `trading/tests/datalake/test_sync_service.py trading/tests/datalake/test_parallel_fetcher.py`.

### Divergences — the plan's blocks are wrong

1. **`_validate_coverage` (Task 2, Step 3) is a real bug; shipped code fixed it.** The plan strips tzinfo (`series.candles[0].timestamp.replace(tzinfo=None)`) and pairs it with a test fixture that tags IST wall-clock as UTC. Real Dhan/Upstox candles are tz-aware UTC *instants*; `.replace(tzinfo=None)` shifts every session 5.5 h and would mark **every real symbol** `partial_response` — sync writes nothing while reporting a failure it blames on the broker. Shipped code compares via `to_ist_naive` (`market_data/src/tradex_market_data/sync_service.py:92-95`). **Do not restore the plan's version.**
2. **`_fetch_bounded` (not in the plan) is the actual no-hang enforcement.** `fetch_with_backoff`'s `deadline` only clamps *sleeps between attempts*; one blocking `fetcher.fetch(...)` still runs to completion, so the plan's `sync()` would blow a 1 s budget on a 5 s broker. Shipped code wraps the batch in a `ThreadPoolExecutor` and `future.result(timeout=remaining)` (`sync_service.py:99-134`).
3. **Task 3's `converge` test could never pass against Task 3's `converge` code.** The plan's test uses `_Store` (whose `read` always returns empty), so `converge` re-scans, re-sees the same gaps, loops to the 10-pass cap, and returns `converged=False` — the `is True` assertion fails. Shipped test added `_MemStore` so the second scan observes the first pass's writes. An internally inconsistent headline test is the tell that this plan was never executed as written.
4. **Task 1's snippet would delete shipped robustness.** The real `fetch_with_backoff` also carries `zero_progress_streak` (give up after 2 no-progress attempts) and `dead_symbols` / `_has_transient_errors` fail-fast (`parallel_fetcher.py:112-131`). The plan's replacement omits all three.

### Still open, and why it matters

**Task 4 is the only task that delivers the goal, and it is not done.** `SyncService` has zero production callers — it appears only in its own module, its test, and `__init__.py`. The drivers still run the old, unbounded path:

- `trading/scripts/repair_gaps.py:210` → `simple_sync(...)`
- `trading/scripts/fill_gaps.py:180` → `simple_sync(...)`
- `trading/scripts/backfill_parquet.py:236-310` → `GapDetector` + a bespoke requeue/backoff loop
- `interfaces/src/tradex_interfaces/cli.py:618` → `simple_sync(...)`

So the blind spot this plan diagnosed is still open at runtime.

### Two constraints the plan's Task 4 understates

- **`SyncService` is not yet a superset of `simple_sync`.** `simple_sync` accepts `ranges=` (range-scoped fetch, the thing that stops a tail-only gap from becoming a full-session Dhan request that returns the wrong bars); `SyncService.sync` does not. `SyncService.scan` also drops `holidays=`, which `repair_gaps`/`backfill_parquet` pass. Routing the range-scoped drivers **without** adding these regresses them. See the rewritten Task 4.
- **`repair_tail` has no broker to route to.** It currently calls `self.sync`, i.e. Dhan — the one venue whose 1m series stops at 15:14 and so cannot serve the tail it exists to repair (`sync_service.py:251-254` admits this). No standalone Upstox tail-repair script exists (`rg -l "repair_tail|tail repair"` finds only these three modules), despite `simple_sync`'s docstring claiming "Upstox tail repair is a separate script." `repair_tail` is therefore a no-op with a misleading name; Task 3's version is **removed** in the reconciliation (see Task 4, Step 0).

### Hardening (2 of 3 fixed in `4bb551a`)

- ✅ **`converge` no longer spins on deterministic failures.** It tracks symbols a pass already failed and stops re-selecting them, so a reproducible `partial_response` is tried once, not ten times. (`SyncReport.under_floored` added alongside.)
- ✅ **`converged=True` no longer ignores floored holes.** When `scan.gaps` is empty, `converge` consults `scan.sub_threshold_symbols`; a hole under `min_gap_stamps` returns `converged=False` with `under_floored=(symbols)`. Note this makes `converged` strictly stricter for every driver — a lake with sub-floor holes now reports "not converged" where it previously said clean.
- ✅ **A broker without history is no longer retried** (`cea5ce6`). Found by running the real dry-run: `PaperBroker.history` raises `PaperHistoryNotImplemented`, which `_is_transient_error` classified as transient, so `fetch_with_backoff` burned the full 480 s budget retrying an impossible fetch. `sync` now honours the broker's own `supports_history` guard (skips without calling), and "no historical data source" / "not implemented" are permanent. `converge` also stops when a pass makes no progress. Dry-run: 480 s → 1.2 s.
- ⚠️ **`_fetch_bounded` still leaks worker threads.** A fresh `ThreadPoolExecutor` per batch with `shutdown(wait=False)` on timeout (`sync_service.py:113, 134`) leaves the hung broker call running in a non-daemon thread; over many batches the *process* can hang on exit even though each pass "returned in budget." There is no clean cancellation for a blocked socket read — this is a stated ceiling, not a solved problem. **Unresolved; do not claim "cannot hang" without it.**

---

## Global Constraints

- Test command from repo root: `.venv/bin/python -m pytest <paths> -q`
- Import-linter: `tradex_market_data` may import `{tradex_domain, tradex_shared}`; `tradex_domain` only stdlib. No new cross-package edges.
- Ruff ratchet: no new C901/PLR0912/PLR0915 violations; line length ≤ 100.
- **Contracts unchanged:** `simple_sync` and `GapDetector` public signatures stay as they are. `SyncService` is additive.
- **No hang:** every retry sleep is clamped to the remaining budget; a pass cannot exceed `budget_seconds`.
- **No silent partial write:** a response that does not cover the requested window is a `failed[symbol]`, never stored.
- Money/price values follow the existing `series_to_frame` (float in parquet) — unchanged.
- Each task ends with a green test run and a single commit.

---

### Task 1: Bounded backoff in `fetch_with_backoff` — ✅ SHIPPED (`de0fecc`)

> **Superseded — do not execute.** The shipped `fetch_with_backoff` matches the *interfaces* below but not the *snippet*: it retains `zero_progress_streak` and `dead_symbols`/`_has_transient_errors` (divergence 4). Tests at `test_parallel_fetcher.py:287` and `:306` are green.

**Files:**
- Modify: `market_data/src/tradex_market_data/parallel_fetcher.py:70-136`
- Test: `trading/tests/datalake/test_parallel_fetcher.py` (extend)

**Interfaces:**
- Consumes: existing `fetch_with_backoff(fetcher, batch, tf, c_start, c_end, *, batch_size, max_retries, ranges, dead_symbols)`.
- Produces: `fetch_with_backoff(..., initial_backoff_s: float = 30.0, deadline: float | None = None)` — `initial_backoff_s` sets the first sleep (0.0 in tests), `deadline` is a `time.monotonic()` value; when the next sleep would pass it, the loop breaks and returns what it has.

- [ ] **Step 1: Write the failing tests**

Add to `trading/tests/datalake/test_parallel_fetcher.py`:

```python
def test_fetch_with_backoff_uses_configurable_initial_sleep(monkeypatch) -> None:
    """initial_backoff_s replaces the hardcoded 30s first sleep."""
    slept: list[float] = []
    monkeypatch.setattr(
        "tradex_market_data.parallel_fetcher.time.sleep", slept.append
    )

    class _Fetcher:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, pending, tf, s, e, ranges=None):
            self.calls += 1
            if self.calls == 1:
                return {}, ["NSE:X: 429 rate limited"]
            return {"NSE:X": _series()}, []

    out = fetch_with_backoff(
        _Fetcher(), [_eq("X")], "1m", START, END,
        initial_backoff_s=0.5, max_retries=3,
    )
    assert "NSE:X" in out
    assert slept == [0.5]  # the configurable base, not 30.0


def test_fetch_with_backoff_stops_at_deadline(monkeypatch) -> None:
    """A deadline in the past makes the loop give up without sleeping."""
    slept: list[float] = []
    monkeypatch.setattr(
        "tradex_market_data.parallel_fetcher.time.sleep", slept.append
    )

    class _Fetcher:
        def fetch(self, pending, tf, s, e, ranges=None):
            return {}, ["NSE:X: 429 rate limited"]

    out = fetch_with_backoff(
        _Fetcher(), [_eq("X")], "1m", START, END,
        initial_backoff_s=30.0, max_retries=6,
        deadline=time.monotonic() - 1.0,
    )
    assert out == {}
    assert slept == []  # deadline already passed => no sleep
```

(`_series`, `_eq`, `START`, `END` already exist in that test module — reuse them; add `import time` at the top if absent.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest trading/tests/datalake/test_parallel_fetcher.py -q -k "configurable_initial_sleep or stops_at_deadline"`
Expected: FAIL — `TypeError: fetch_with_backoff() got an unexpected keyword argument 'initial_backoff_s'`.

- [ ] **Step 3: Implement**

In `parallel_fetcher.py`, change the signature (line ~70):

```python
def fetch_with_backoff(
    fetcher: ParallelHistoryFetcher,
    batch: list[Instrument],
    tf: Timeframe | str,
    c_start: datetime,
    c_end: datetime,
    *,
    batch_size: int = 20,
    max_retries: int = 6,
    ranges: dict[str, list[tuple[datetime, datetime]]] | None = None,
    dead_symbols: set[str] | None = None,
    initial_backoff_s: float = 30.0,
    deadline: float | None = None,
) -> dict[str, HistoricalSeries]:
```

Replace `backoff = 30.0` (line 85) with `backoff = initial_backoff_s`, and the reset at line 111 with `backoff = initial_backoff_s`. Replace the sleep block (lines 129-134) with:

```python
        if deadline is not None and time.monotonic() >= deadline:
            log.warning(
                "fetch_with_backoff: budget exhausted — giving up on %d symbol(s)",
                len(still_pending),
            )
            break
        sleep_for = backoff
        if deadline is not None:
            sleep_for = min(backoff, max(0.0, deadline - time.monotonic()))
        log.warning(
            "fetch_with_backoff: rate-limited (attempt %d/%d) — sleeping %.1fs",
            attempt, max_retries, sleep_for,
        )
        if sleep_for > 0:
            time.sleep(sleep_for)
        backoff = min(backoff * 2, 480.0)
        pending = still_pending
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest trading/tests/datalake/test_parallel_fetcher.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add market_data/src/tradex_market_data/parallel_fetcher.py trading/tests/datalake/test_parallel_fetcher.py
git commit -m "feat(market_data): configurable, deadline-bounded backoff in fetch_with_backoff"
```

---

### Task 2: `SyncService` core — scan / sync with coverage validation — ✅ SHIPPED (`196326a`, fixed by `560d8a7`)

> **Superseded — do not execute.** The shipped `_validate_coverage` uses `to_ist_naive` (divergence 1), and the shipped `sync` adds `_fetch_bounded` (divergence 2). The code block in Step 3 below would re-introduce a 5.5 h coverage bug and drop the only real no-hang guarantee.

**Files:**
- Create: `market_data/src/tradex_market_data/sync_service.py`
- Modify: `market_data/src/tradex_market_data/__init__.py` (export `SyncConfig`, `SyncReport`, `SyncService`)
- Test: `trading/tests/datalake/test_sync_service.py` (create)

**Interfaces:**
- Consumes: `GapDetector`, `ParallelHistoryFetcher`, `fetch_with_backoff`, `ParquetStorage`, `series_to_frame`, `Timeframe`, `HistoricalSeries`.
- Produces: `SyncConfig`, `SyncReport`, `SyncService(broker, store, config)` with `.scan(instruments, start, end) -> GapScan` and `.sync(instruments, start, end) -> SyncReport`.

- [ ] **Step 1: Write the failing tests**

Create `trading/tests/datalake/test_sync_service.py`:

```python
"""SyncService — bounded, coverage-validated datalake sync."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
from tradex_domain import Candle, Equity, HistoricalSeries, OHLC, Price, Quantity, Timeframe
from tradex_market_data.sync_service import SyncConfig, SyncService


START = datetime(2026, 8, 3, 9, 15)
END = datetime(2026, 8, 3, 15, 30)


def _eq(symbol: str) -> Equity:
    return Equity.of("NSE", symbol)


def _candle(inst, ts: datetime) -> Candle:
    return Candle(
        instrument=inst,
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=Decimal("100")), high=Price(value=Decimal("101")),
            low=Price(value=Decimal("99")), close=Price(value=Decimal("100")),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts.replace(tzinfo=UTC),
    )


def _full_series(inst, start: datetime, end: datetime) -> HistoricalSeries:
    stamps, t = [], start
    while t <= end:
        stamps.append(t)
        t += timedelta(minutes=1)
    return HistoricalSeries(
        instrument=inst, timeframe=Timeframe.M1,
        candles=[_candle(inst, s) for s in stamps], start=start, end=end,
    )


class _Store:
    def __init__(self) -> None:
        self.rows = 0

    def upsert(self, df: pd.DataFrame) -> int:
        self.rows += len(df)
        return len(df)

    def read(self, **kw) -> pd.DataFrame:
        return pd.DataFrame()

    def date_range(self, symbol):
        return None


class FakeBroker:
    """Programmable broker: rate-limit N times, truncate, sleep, or serve."""

    def __init__(self, *, fail_first=0, partial=False, delay_s=0.0) -> None:
        self._fail_first = fail_first
        self._partial = partial
        self._delay_s = delay_s
        self._calls: dict[str, int] = {}
        self.rate_limiter = None

    def history(self, inst, timeframe, start, end):
        sym = inst.symbol
        self._calls[sym] = self._calls.get(sym, 0) + 1
        if self._delay_s:
            time.sleep(self._delay_s)
        if self._calls[sym] <= self._fail_first:
            raise RuntimeError("429 rate limited")
        if self._partial:
            half = start + (end - start) / 2
            return _full_series(inst, start, half)
        return _full_series(inst, start, end)


def _service(broker, *, budget_s=30.0, backoff_s=0.0, validate=True) -> SyncService:
    return SyncService(
        broker, _Store(),
        SyncConfig(
            timeframe="1m", budget_seconds=budget_s,
            initial_backoff_s=backoff_s, validate_coverage=validate,
        ),
    )


def test_sync_fetches_and_writes_normal() -> None:
    svc = _service(FakeBroker())
    report = svc.sync([_eq("X")], START, END)
    assert report.fetched == 1
    assert report.written > 0
    assert report.failed == {}


def test_sync_retries_rate_limit_then_succeeds() -> None:
    svc = _service(FakeBroker(fail_first=1), backoff_s=0.0)
    report = svc.sync([_eq("X"), _eq("Y")], START, END)
    assert report.failed == {}
    assert report.fetched == 2


def test_sync_respects_budget() -> None:
    svc = _service(FakeBroker(delay_s=5.0), budget_s=1.0)
    t0 = time.monotonic()
    report = svc.sync([_eq("X"), _eq("Y")], START, END)
    assert time.monotonic() - t0 < 3.0  # never hangs
    assert report.resumable is True


def test_sync_detects_partial_response() -> None:
    svc = _service(FakeBroker(partial=True))
    report = svc.sync([_eq("X")], START, END)
    assert "X" in report.failed
    assert report.failed["X"] == "partial_response"
    assert report.written == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest trading/tests/datalake/test_sync_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradex_market_data.sync_service'`.

- [ ] **Step 3: Implement `sync_service.py`**

```python
"""SyncService — one bounded, resumable datalake sync path.

Composes GapDetector (detect) + ParallelHistoryFetcher (fetch) +
ParquetStorage (store). Threads a wall-clock deadline through every retry
so a run cannot hang, and validates that each fetched series covers the
window it was asked for before writing it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from itertools import batched
from typing import Any

import pandas as pd
from tradex_domain import Timeframe
from tradex_domain.market import HistoricalSeries

from tradex_market_data.gap_detector import GapDetector, GapScan
from tradex_market_data.parallel_fetcher import (
    ParallelHistoryFetcher,
    fetch_with_backoff,
)
from tradex_market_data.simple_sync import series_to_frame

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncConfig:
    timeframe: str = "1m"
    batch_size: int = 20
    max_workers: int = 2
    min_gap_stamps: int = 1
    include_open_stamps: bool = False
    tail_days: int | None = None
    budget_seconds: float = 480.0
    max_attempts: int = 6
    initial_backoff_s: float = 30.0
    validate_coverage: bool = True


@dataclass(frozen=True)
class SyncReport:
    requested: int
    fetched: int
    written: int
    failed: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    batches: int = 0
    elapsed_s: float = 0.0
    converged: bool = False
    resumable: bool = False


class SyncService:
    def __init__(self, broker: Any, store: Any, config: SyncConfig) -> None:
        self._broker = broker
        self._store = store
        self._config = config

    def scan(self, instruments, start, end) -> GapScan:
        return GapDetector(self._store).scan(
            instruments, start=start, end=end,
            timeframe=self._config.timeframe,
            bar_freq="1min",
            min_gap_stamps=self._config.min_gap_stamps,
            tail_days=self._config.tail_days,
            include_open_stamps=self._config.include_open_stamps,
        )

    def _validate_coverage(
        self, series: HistoricalSeries, start: datetime, end: datetime,
    ) -> bool:
        if not series.candles:
            return False
        tf = Timeframe(self._config.timeframe)
        step = tf.timedelta if hasattr(tf, "timedelta") else None
        # Allow one bar of slack at each edge (broker session conventions).
        first = series.candles[0].timestamp.replace(tzinfo=None)
        last = series.candles[-1].timestamp.replace(tzinfo=None)
        return first <= start + (step or __import__("datetime").timedelta(minutes=1)) \
            and last >= end - (step or __import__("datetime").timedelta(minutes=1))

    def sync(self, instruments, start, end) -> SyncReport:
        inst_list = list(instruments)
        if not inst_list:
            return SyncReport(0, 0, 0)
        t0 = time.monotonic()
        deadline = t0 + self._config.budget_seconds
        fetcher = ParallelHistoryFetcher(
            {"dhan": self._broker}, max_workers=self._config.max_workers,
        )
        fetched = written = batches = 0
        failed: dict[str, str] = {}
        skipped: list[str] = []
        batches_list = list(batched(inst_list, self._config.batch_size))
        for bi, batch in enumerate(batches_list, 1):
            if time.monotonic() >= deadline:
                break
            batches = bi
            results = fetch_with_backoff(
                fetcher, list(batch), self._config.timeframe, start, end,
                max_retries=self._config.max_attempts,
                initial_backoff_s=self._config.initial_backoff_s,
                deadline=deadline,
            )
            frames: list[pd.DataFrame] = []
            for inst in batch:
                series = results.get(str(inst.instrument_id))
                if series is None or not series.candles:
                    skipped.append(inst.symbol)
                    continue
                if self._config.validate_coverage and not self._validate_coverage(
                    series, start, end
                ):
                    failed[inst.symbol] = "partial_response"
                    continue
                frames.append(series_to_frame(series, inst.symbol))
                fetched += 1
            if frames:
                written += self._store.upsert(pd.concat(frames, ignore_index=True))
            log.info(
                "sync: batch %d/%d fetched=%d written=%d failed=%d elapsed=%.0fs",
                bi, len(batches_list), fetched, written, len(failed),
                time.monotonic() - t0,
            )
        elapsed = time.monotonic() - t0
        exhausted = time.monotonic() >= deadline and len(failed) + len(skipped) > 0
        return SyncReport(
            requested=len(inst_list), fetched=fetched, written=written,
            failed=failed, skipped=skipped, batches=batches, elapsed_s=elapsed,
            resumable=exhausted,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest trading/tests/datalake/test_sync_service.py -q`
Expected: 4 passed.

- [ ] **Step 5: Run the datalake regression**

Run: `.venv/bin/python -m pytest trading/tests/datalake -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add market_data/src/tradex_market_data/sync_service.py market_data/src/tradex_market_data/__init__.py trading/tests/datalake/test_sync_service.py
git commit -m "feat(market_data): SyncService core with bounded backoff and coverage validation"
```

---

### Task 3: `converge` + `repair_tail` — ⚠️ PARTIAL (`889f2e6`)

> **Superseded — do not execute.** `converge` shipped (with `_MemStore` in its test, divergence 3). `repair_tail` shipped but is a no-op with a misleading name and is **removed** in the reconciled Task 4 (Step 0). Also owed: the `converge` spin on deterministic failures, and consulting `sub_threshold_symbols` before declaring `converged` (see Hardening above).

**Files:**
- Modify: `market_data/src/tradex_market_data/sync_service.py`
- Test: `trading/tests/datalake/test_sync_service.py` (extend)

**Interfaces:**
- Produces: `SyncService.converge(instruments, start, end) -> SyncReport` (scan→sync→re-scan until clean or budget; sets `converged`), `SyncService.repair_tail(instruments, start, end) -> SyncReport` (routes the classified edge stamps to the Upstox pass).

- [ ] **Step 1: Write the failing tests**

```python
def test_converge_terminates_when_clean() -> None:
    svc = _service(FakeBroker())
    report = svc.converge([_eq("X")], START, END)
    assert report.converged is True


def test_converge_stops_at_budget() -> None:
    svc = _service(FakeBroker(delay_s=5.0), budget_s=1.0)
    t0 = time.monotonic()
    report = svc.converge([_eq("X"), _eq("Y")], START, END)
    assert time.monotonic() - t0 < 4.0
    assert report.converged is False
    assert report.resumable is True
```

- [ ] **Step 2: Run to verify failure** — `AttributeError: 'SyncService' object has no attribute 'converge'`.

- [ ] **Step 3: Implement**

Add to `SyncService`:

```python
    def converge(self, instruments, start, end) -> SyncReport:
        inst_list = list(instruments)
        t0 = time.monotonic()
        deadline = t0 + self._config.budget_seconds
        total = SyncReport(0, 0, 0)
        fetched = written = 0
        failed: dict[str, str] = {}
        for _ in range(10):  # bounded passes; budget is the real cap
            scan = self.scan(inst_list, start, end)
            if not scan.gaps:
                return SyncReport(
                    requested=len(inst_list), fetched=fetched, written=written,
                    failed=failed, elapsed_s=time.monotonic() - t0, converged=True,
                )
            gapped = [inst for inst, _ in scan.gaps]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            saved = self._config
            object.__setattr__(self, "_config", replace(saved, budget_seconds=remaining))
            try:
                part = self.sync(gapped, start, end)
            finally:
                object.__setattr__(self, "_config", saved)
            fetched += part.fetched
            written += part.written
            failed.update(part.failed)
            if part.resumable:
                break
        return SyncReport(
            requested=len(inst_list), fetched=fetched, written=written,
            failed=failed, elapsed_s=time.monotonic() - t0, converged=False,
            resumable=True,
        )

    def repair_tail(self, instruments, start, end) -> SyncReport:
        # The detector classifies the unrepairable close; the open is
        # repairable. Compose the Upstox pass over the edge-only symbols.
        scan = GapDetector(self._store).scan(
            instruments, start=start, end=end,
            timeframe=self._config.timeframe, bar_freq="1min",
            include_open_stamps=True,
        )
        edge_symbols = {s for s in scan.edge_only} | set(scan.sub_threshold_symbols)
        targets = [i for i in instruments if i.symbol in edge_symbols]
        if not targets:
            return SyncReport(0, 0, 0, converged=True)
        # The tail pass uses the same sync machinery against the Upstox broker
        # when one is configured; here it reuses self._broker so the service
        # stays single-broker until the tail broker is wired at the driver.
        return self.sync(targets, start, end)
```

(Add `from dataclasses import replace` to the imports.)

- [ ] **Step 4: Run tests** — all pass.
- [ ] **Step 5: Commit** — `feat(market_data): SyncService converge and tail-repair`.

---

### Task 4: Route the drivers through `SyncService` — ❌ NOT STARTED

**This is the only task that delivers the goal.** It is not a mechanical delegation: `SyncService` is not yet a superset of `simple_sync`, so routing the range-scoped drivers first regresses them.

**Files:**
- Modify: `market_data/src/tradex_market_data/sync_service.py` (Steps 0–1)
- Modify: `trading/scripts/repair_gaps.py`, `trading/scripts/fill_gaps.py`, `trading/scripts/backfill_parquet.py`, `interfaces/src/tradex_interfaces/cli.py` (Step 2)
- Test: `trading/tests/datalake/test_sync_service.py` (extend), existing script/CLI tests stay green

**Interfaces to add first (Step 1):**
- `SyncService.sync(..., *, ranges: dict[str, list[tuple[datetime, datetime]]] | None = None)` — forwarded to `fetch_with_backoff(..., ranges=ranges)`, so range-scoped drivers keep asking Dhan only for the hole.
- `SyncService.scan(..., holidays: set[date] | None = None)` — forwarded to `GapDetector.scan`, so holiday-aware drivers keep their grid.
- `SyncConfig.max_workers` (already present) is the scan's `max_workers` — confirm `scan` forwards it (it currently hardcodes nothing; it omits it, defaulting to 1).

---

- [x] **Step 0: Delete `repair_tail`.** — ✅ `8a7ba84`. It routed through `self.sync` (Dhan), which cannot serve the tail it exists to repair; no Upstox tail broker is wired and no standalone script exists. Docstrings in `simple_sync.py` / `parallel_fetcher.py` corrected — the "Upstox tail repair is a separate script" line described a script that does not exist.

- [x] **Step 1: Extend `SyncService` to a superset of `simple_sync`** — ✅ `badd663`. `sync(..., ranges=)` forwards ranges to the fetcher and validates coverage against those ranges (not the full window) via `_covers_ranges`; `scan(..., holidays=)` forwards holidays. Tests: `test_sync_range_scoped_fetch_asks_only_for_the_ranges`, `test_scan_honours_holidays`.

- [x] **Step 2: Route the drivers.** — ✅ `205f589` (cli), `6ba2b9c` (scripts). `backfill_parquet` deliberately **not** routed (see below). Map each driver's existing args onto `SyncConfig` — do **not** drop flags:
  - `repair_gaps`: `--budget`→`budget_seconds`; `--timeframe`→`timeframe`; `--min-gap-stamps`→`min_gap_stamps`; `--no-open-stamps`→`include_open_stamps=False`; `--chunk`→`batch_size`; `--tail-days`→`tail_days`; pass `holidays=NSE_HOLIDAYS_2026`; build `ranges` from `gap_map`; keep the cluster ordering (`_ORDER`, `_kind`) in the driver — it is policy, not sync.
  - `fill_gaps`: same mapping; `--tail-days`→`tail_days`; keep the cluster loop; pass `ranges=`.
  - `backfill_parquet`: the bespoke requeue/backoff loop (lines ~258–310) is exactly what `fetch_with_backoff` already does — replace it with `SyncService.sync`. Keep `--skip-existing` → `converge`, `--no-skip-existing` → `sync`. Note this driver currently supports **two** brokers via `_make_fetchers`; `SyncService` is single-broker, so either pass one fetcher at a time or leave this driver's multi-broker path until `SyncService` grows a broker pair.
  - `cli.cmd_sync`: `--skip-existing` (default true) → `converge`, `--no-skip-existing` → `sync`; `--min-gap-stamps`, `--batch-size`, `--workers`→`max_workers`, `--backoff-base`→`initial_backoff_s`. `--dry-run` uses `PaperBroker`, which has no `rate_limiter` attribute — `ParallelHistoryFetcher` already falls back to the paper table (`parallel_fetcher.py:206-215`), so confirm the dry-run path still works.

- [x] **Step 3: Verify no driver changed behavior.** — ✅ `851 passed, 1 skipped` on `trading/tests/datalake trading/tests/interface market_data/tests`; both scripts `--help` cleanly. The `backfill_parquet` multi-broker path is unchanged by design.

- [x] **Step 4: Commit** — ✅ `205f589`, `6ba2b9c`.

---

## Self-Review (post-reconciliation)

**1. Spec coverage:** F1 backoff wired → ✅ Task 1+2 shipped; F2 converge → ✅ Task 3 shipped (but spins on deterministic failures — hardening owed); F3 tail composition → ❌ `repair_tail` shipped as a no-op and is deleted in Task 4 Step 0; F4 coverage → ✅ shipped via `to_ist_naive`, **not** the plan's buggy `.replace(tzinfo=None)`; F5–F7 progress/resumable → ✅ `SyncReport` shipped. Drivers → **❌ Task 4, not started — this is the whole remaining job.**

**2. Placeholder scan:** Tasks 1–3 code blocks are superseded (see Reconciliation). Task 4 is deliberately prose + interfaces, because each driver's edit depends on its current arg parsing and the mapping must be checked flag-by-flag.

**3. Type consistency:** `SyncConfig`/`SyncReport` names match between definition, tests, and `sync`/`converge`. `fetch_with_backoff`'s `initial_backoff_s`/`deadline` are consistent between Task 1 and Task 2's call site.

**Behavior-preservation:** `simple_sync`/`GapDetector` signatures unchanged; `SyncService` is additive. **But `SyncService` is not yet a behavioral superset** — it lacks `ranges`/`holidays`, so Task 4 Step 1 must land before Step 2 or the range-scoped drivers regress.

**Testing note:** the `FakeBroker` is the key — the old tests mock `ParallelHistoryFetcher` wholesale, so nothing exercised the real backoff/budget path. Each shipped test has a negative control. The coverage test (`test_coverage_accepts_utc_candles_at_ist_instants`) exists specifically because the plan's original fixture hid the 5.5 h bug.

---

## Execution Handoff

**State: Tasks 1–3 shipped; Task 4 is the remaining work.** Do not re-execute Tasks 1–3 — the tree is ahead of this document.

Recommended order:

1. ~~**Harden `converge`**~~ — ✅ `4bb551a`.
2. ~~**Task 4 Step 1**~~ — ✅ `badd663` (`ranges`/`holidays`); the venue-tail coverage fix `cbf3efa` landed with it, without which routing would have marked every symbol `partial_response`.
3. ~~**Task 4 Step 0**~~ — ✅ `8a7ba84`.
4. ~~**Task 4 Steps 2–4**~~ — ✅ `205f589`, `6ba2b9c`. `tradex sync`, `repair_gaps`, `fill_gaps` all run through `SyncService`.

**Not routed, by decision:** `backfill_parquet` stays on its own path. It is genuinely multi-broker (`_make_fetchers` builds one fetcher per broker) and `SyncService` is single-broker; unifying it means either looping brokers per batch or giving `SyncService` a broker pair — both larger than this task. Its bespoke requeue/backoff loop is now the only copy of that logic left outside `fetch_with_backoff`; fold it in if that driver is ever consolidated.

Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per step, review between steps.

**2. Inline Execution** — execute in this session with checkpoints.
