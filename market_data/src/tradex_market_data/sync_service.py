"""SyncService — one bounded, resumable datalake sync path.

Scan for missing session minutes, fetch those minutes, write those minutes.
Minutes after Dhan's 1-minute ceiling are not requested. The budget stops
the next call; a socket already in flight runs on a daemon thread so
process exit drops it.
"""

from __future__ import annotations

import fcntl
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from itertools import batched
from pathlib import Path
from typing import Any

import pandas as pd
from tradex_config.broker_limits import BROKER_LIMITS
from tradex_domain.market import HistoricalSeries
from tradex_domain.market_calendar import IST, MARKET_CLOSE, MARKET_OPEN, to_ist_naive

from tradex_market_data.gap_detector import GapDetector, GapScan
from tradex_market_data.parallel_fetcher import (
    ParallelHistoryFetcher,
    fetch_with_backoff,
)

log = logging.getLogger(__name__)

_LAST_1M = BROKER_LIMITS.dhan_last_1m
_STEP = timedelta(minutes=1)
#: A run this long is a whole missing session, not a hole inside one.
_SESSION_RUN = timedelta(minutes=300)


def servable_bounds(
    start: datetime,
    end: datetime,
    now: datetime,
    holidays: set | None = None,
) -> tuple[datetime, datetime] | None:
    """Session minutes Dhan can already have sent.

    ``start`` is the open of its calendar day. A midnight ``end`` means that
    calendar day, inclusive. Any other ``end`` is a clock time. The result
    never goes past 15:14, and never includes the minute that is still forming.
    """
    holiday_set = holidays or set()
    start_n = to_ist_naive(start)
    end_n = to_ist_naive(end)
    cap = _last_closed(now, holiday_set)
    lo = datetime.combine(start_n.date(), MARKET_OPEN)
    if end_n.time() == datetime.min.time():
        hi = datetime.combine(end_n.date(), _LAST_1M)
    else:
        hi = end_n.replace(second=0, microsecond=0)
    if hi.time() > _LAST_1M:
        hi = datetime.combine(hi.date(), _LAST_1M)
    hi = min(hi, cap)
    if hi.time() > _LAST_1M:
        hi = datetime.combine(hi.date(), _LAST_1M)
    if lo > hi:
        return None
    return lo, hi


def _last_closed(now: datetime, holidays: set) -> datetime:
    """The latest 1-minute stamp whose bar has finished."""
    closed = to_ist_naive(now).replace(second=0, microsecond=0) - _STEP
    for _ in range(10):
        if closed.weekday() >= 5 or closed.date() in holidays:
            closed = datetime.combine(closed.date() - timedelta(days=1), _LAST_1M)
            continue
        if closed.time() > _LAST_1M:
            return datetime.combine(closed.date(), _LAST_1M)
        if closed.time() < MARKET_OPEN:
            closed = datetime.combine(closed.date() - timedelta(days=1), _LAST_1M)
            continue
        return closed
    return closed


class SyncLocked(RuntimeError):
    """Another sync already holds the lake."""


@contextmanager
def sync_lock(root: Path | str):
    """One sync per lake.

    Two sync processes would each hold their own rate limiter, so together they
    exceed Dhan's 5 requests per second, and ``ParquetStorage`` locks
    partitions only inside one process, so they could overwrite each other's
    partition. Refuse the second run instead of racing.

    shortcut: POSIX ``flock``, no Windows support. A live session that writes
    to the same lake is not covered by this lock.
    """
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    with open(path / ".sync.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncLocked(f"another sync is running on {path}") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _priority(ranges: list[tuple[datetime, datetime]]) -> tuple[int, int]:
    """Repair order: partial-day holes, whole missing sessions, bare opens.

    A whole session is an hours-long run, and it must not queue ahead of a
    hole inside a day that is otherwise present. Within a kind, the symbol
    missing the most minutes goes first.
    """
    stamps = sum(int((end - start) / _STEP) + 1 for start, end in ranges)
    if all(s == e and s.time() == MARKET_OPEN for s, e in ranges):
        return (2, -stamps)
    if any(end - start >= _SESSION_RUN for start, end in ranges):
        return (1, -stamps)
    return (0, -stamps)


def _merge_ranges(
    ranges: list[tuple[datetime, datetime]],
    step: timedelta,
) -> list[tuple[datetime, datetime]]:
    """Join ranges that touch, so 09:15 and 09:16–15:14 are one request."""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + step:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def series_to_frame(series: HistoricalSeries, symbol: str) -> pd.DataFrame:
    """Convert HistoricalSeries to storage DataFrame (tz-naive IST)."""
    if not series.candles:
        return pd.DataFrame()

    candles = series.candles
    timestamps = [to_ist_naive(c.timestamp) for c in candles]
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

@dataclass(frozen=True)
class SyncConfig:
    timeframe: str = "1m"
    batch_size: int = 20
    max_workers: int = 2
    min_gap_stamps: int = 1
    include_open_stamps: bool = True
    tail_days: int | None = None
    budget_seconds: float = 480.0
    max_attempts: int = 6
    initial_backoff_s: float = 2.0
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
    #: Symbols whose only remaining shortfall is a hole under ``min_gap_stamps``.
    #: The scan cannot see them, so ``converged`` must not be declared over
    #: them: a rerun at the same floor is a no-op, which is why this is
    #: reported rather than retried.
    under_floored: tuple[str, ...] = ()


class SyncService:
    def __init__(self, broker: Any, store: Any, config: SyncConfig) -> None:
        if config.timeframe != "1m":
            raise ValueError("sync is 1-minute Dhan bars only")
        self._broker = broker
        self._store = store
        self._config = config

    def scan(self, instruments, start, end, *, holidays=None, now=None) -> GapScan:
        holiday_set = set(holidays or ())
        bounds = servable_bounds(start, end, now or datetime.now(IST), holiday_set)
        if bounds is None:
            return GapScan()
        lo, hi = bounds
        return GapDetector(self._store).scan(
            instruments,
            start=lo,
            end=hi,
            timeframe="1m",
            bar_freq="60s",
            holidays=holidays,
            min_gap_stamps=self._config.min_gap_stamps,
            tail_days=self._config.tail_days,
            include_open_stamps=self._config.include_open_stamps,
        )

    def _clip_ranges(
        self,
        ranges: list[tuple[datetime, datetime]],
        cap: datetime,
    ) -> list[tuple[datetime, datetime]]:
        """Weekday session minutes Dhan can return, split per day."""
        kept: list[tuple[datetime, datetime]] = []
        for start, end in ranges:
            day = start.date()
            while day <= end.date():
                if day.weekday() < 5:
                    lo = max(start, datetime.combine(day, MARKET_OPEN))
                    hi = min(end, datetime.combine(day, _LAST_1M), cap)
                    if lo <= hi:
                        kept.append((lo, hi))
                day += timedelta(days=1)
        return _merge_ranges(kept, _STEP)

    def _covers(
        self,
        series: HistoricalSeries,
        ranges: list[tuple[datetime, datetime]],
        holidays: set,
    ) -> bool:
        """Every servable minute of every range is present.

        Compared in IST-naive minutes. A post-market phantom cannot stand in
        for a session minute, and a hole with both endpoints but a missing
        middle does not pass.
        """
        have = {
            to_ist_naive(c.timestamp).replace(second=0, microsecond=0)
            for c in series.candles
        }
        required = 0
        for start, end in ranges:
            t = start.replace(second=0, microsecond=0)
            end_m = end.replace(second=0, microsecond=0)
            while t <= end_m:
                if (
                    t.weekday() < 5
                    and t.date() not in holidays
                    and MARKET_OPEN <= t.time() <= _LAST_1M
                ):
                    required += 1
                    if t not in have:
                        return False
                t += _STEP
        return required > 0

    def _fetch_bounded(
        self,
        fetcher: ParallelHistoryFetcher,
        batch: list,
        start: datetime,
        end: datetime,
        deadline: float,
        ranges: dict[str, list[tuple[datetime, datetime]]] | None = None,
    ) -> dict[str, HistoricalSeries]:
        """Fetch one batch, giving up when the budget is spent.

        A blocked socket cannot be cancelled. The call runs on a daemon
        thread so a timeout returns and process exit drops the socket.
        shortcut: one in-flight request can outlive the budget, upgrade
        when the HTTP client grows a cancel token.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {}
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["result"] = fetch_with_backoff(
                    fetcher,
                    batch,
                    self._config.timeframe,
                    start,
                    end,
                    max_retries=self._config.max_attempts,
                    initial_backoff_s=self._config.initial_backoff_s,
                    deadline=deadline,
                    ranges=ranges,
                )
            except Exception as exc:  # noqa: BLE001 — surfaced to the caller
                box["error"] = exc

        thread = threading.Thread(target=run, name="sync-batch", daemon=True)
        thread.start()
        thread.join(remaining)
        if thread.is_alive():
            log.warning(
                "sync: budget exhausted mid-batch — abandoning %d symbol(s)",
                len(batch),
            )
            return {}
        if "error" in box:
            raise box["error"]
        return box.get("result", {})

    def _keep(
        self,
        series: HistoricalSeries,
        ranges: list[tuple[datetime, datetime]],
    ) -> list:
        """Candles inside the asked ranges and inside the session."""
        kept = []
        for candle in series.candles:
            ts = to_ist_naive(candle.timestamp)
            if ts.weekday() >= 5 or not (MARKET_OPEN <= ts.time() <= MARKET_CLOSE):
                continue
            if any(start <= ts <= end for start, end in ranges):
                kept.append(candle)
        return kept

    def sync(
        self,
        instruments,
        start,
        end,
        *,
        ranges: dict[str, list[tuple[datetime, datetime]]] | None = None,
        holidays=None,
        now=None,
    ) -> SyncReport:
        inst_list = list(instruments)
        if not inst_list:
            return SyncReport(0, 0, 0)
        holiday_set = set(holidays or ())
        t0 = time.monotonic()
        deadline = t0 + self._config.budget_seconds
        bounds = servable_bounds(start, end, now or datetime.now(IST), holiday_set)
        if bounds is None:
            return SyncReport(
                requested=len(inst_list), fetched=0, written=0,
                elapsed_s=time.monotonic() - t0,
            )
        lo, hi = bounds
        window = self._clip_ranges([(lo, hi)], hi)
        # One span for a full-window fetch. Per-day clips only when the
        # caller handed us the holes — otherwise a 30-day sync becomes
        # one request per session.
        span = [(window[0][0], window[-1][1])] if window else []
        clipped: dict[str, list[tuple[datetime, datetime]]] = {}
        work = []
        for inst in inst_list:
            key = str(inst.instrument_id)
            if ranges is not None:
                inst_ranges = self._clip_ranges(ranges.get(key) or [], hi)
            else:
                inst_ranges = span
            if not inst_ranges:
                continue
            clipped[key] = inst_ranges
            work.append(inst)
        if not work:
            return SyncReport(
                requested=len(inst_list), fetched=0, written=0,
                elapsed_s=time.monotonic() - t0,
            )
        fetcher = ParallelHistoryFetcher(
            {"dhan": self._broker},
            max_workers=self._config.max_workers,
        )
        fetched = written = batches = 0
        failed: dict[str, str] = {}
        skipped: list[str] = []
        # A broker that advertises it cannot serve history (PaperBroker) is
        # asked nothing: calling history() would raise per symbol and the retry
        # loop would spend the entire budget discovering it cannot work.
        if getattr(self._broker, "supports_history", True) is False:
            log.warning(
                "sync: broker %s does not support history — %d symbol(s) skipped",
                type(self._broker).__name__,
                len(work),
            )
            return SyncReport(
                requested=len(inst_list),
                fetched=0,
                written=0,
                skipped=[i.symbol for i in work],
                elapsed_s=time.monotonic() - t0,
            )
        fetch_start = min(r[0] for rs in clipped.values() for r in rs)
        fetch_end = max(r[1] for rs in clipped.values() for r in rs)
        batches_list = list(batched(work, self._config.batch_size))
        for bi, batch in enumerate(batches_list, 1):
            if time.monotonic() >= deadline:
                for rest in batches_list[bi - 1:]:
                    skipped.extend(i.symbol for i in rest)
                break
            batches = bi
            batch_ranges = {str(i.instrument_id): clipped[str(i.instrument_id)] for i in batch}
            results = self._fetch_bounded(
                fetcher, list(batch), fetch_start, fetch_end, deadline, batch_ranges,
            )
            abandoned = time.monotonic() >= deadline and not results
            frames: list[pd.DataFrame] = []
            for inst in batch:
                key = str(inst.instrument_id)
                series = results.get(key)
                if series is None or not series.candles:
                    if abandoned:
                        skipped.append(inst.symbol)
                    else:
                        failed[inst.symbol] = "no_data"
                    continue
                inst_ranges = clipped[key]
                if self._config.validate_coverage:
                    covered = [
                        r for r in inst_ranges
                        if self._covers(series, [r], holiday_set)
                    ]
                    short = len(covered) != len(inst_ranges)
                else:
                    covered = inst_ranges
                    short = False
                kept = self._keep(series, covered) if covered else []
                if short or not kept:
                    failed[inst.symbol] = "partial_response"
                if not kept:
                    continue
                frames.append(series_to_frame(
                    HistoricalSeries(
                        instrument=series.instrument,
                        timeframe=series.timeframe,
                        candles=kept,
                        start=series.start,
                        end=series.end,
                    ),
                    inst.symbol,
                ))
                fetched += 1
            if frames:
                written += self._store.upsert(pd.concat(frames, ignore_index=True))
            log.info(
                "sync: batch %d/%d fetched=%d written=%d failed=%d elapsed=%.0fs",
                bi,
                len(batches_list),
                fetched,
                written,
                len(failed),
                time.monotonic() - t0,
            )
        elapsed = time.monotonic() - t0
        exhausted = time.monotonic() >= deadline and len(failed) + len(skipped) > 0
        return SyncReport(
            requested=len(inst_list),
            fetched=fetched,
            written=written,
            failed=failed,
            skipped=skipped,
            batches=batches,
            elapsed_s=elapsed,
            resumable=exhausted,
        )

    def converge(self, instruments, start, end, *, holidays=None, now=None) -> SyncReport:
        inst_list = list(instruments)
        clock = now or datetime.now(IST)
        t0 = time.monotonic()
        deadline = t0 + self._config.budget_seconds
        fetched = written = 0
        failed: dict[str, str] = {}
        #: Symbols a pass has already tried and left still-short. A truncated
        #: response reproduces on every attempt, so re-fetching it only burns
        #: the budget; track it and stop re-selecting it.
        retried: set[str] = set()
        for _ in range(10):  # bounded passes; budget is the real cap
            scan = self.scan(inst_list, start, end, holidays=holidays, now=clock)
            if not scan.gaps:
                # A floored hole is invisible to ``gaps`` but is not a clean
                # lake. Do not declare convergence over it — a rerun at the
                # same floor changes nothing, so it is reported, not retried.
                under_floored = tuple(scan.sub_threshold_symbols)
                if under_floored:
                    log.warning(
                        "converge: %d symbol(s) have holes under min_gap_stamps=%d "
                        "(%s) — not converged",
                        len(under_floored),
                        self._config.min_gap_stamps,
                        ", ".join(under_floored[:5]),
                    )
                return SyncReport(
                    requested=len(inst_list),
                    fetched=fetched,
                    written=written,
                    failed=failed,
                    elapsed_s=time.monotonic() - t0,
                    converged=not under_floored,
                    under_floored=under_floored,
                )
            fresh = [
                (inst, rngs) for inst, rngs in scan.gaps
                if inst.symbol not in retried
            ]
            if not fresh:
                break  # everything still gapped is a deterministic failure
            fresh.sort(key=lambda item: _priority(item[1]))
            gapped = [inst for inst, _ in fresh]
            # Fetch only the detected holes. Widening to the whole window would
            # re-request 30 days per symbol to fill a five-minute gap, and (on
            # Dhan) turns a tail fetch into a full-session request that returns
            # the wrong bars.
            gap_map = {str(inst.instrument_id): rngs for inst, rngs in fresh}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            saved = self._config
            object.__setattr__(self, "_config", replace(saved, budget_seconds=remaining))
            try:
                part = self.sync(
                    gapped, start, end, ranges=gap_map, holidays=holidays, now=clock,
                )
            finally:
                object.__setattr__(self, "_config", saved)
            fetched += part.fetched
            written += part.written
            failed.update(part.failed)
            retried.update(part.failed)  # failed symbols are not re-selected
            if part.resumable:
                break
            if not part.fetched and not part.written and not part.failed:
                # The pass achieved nothing and blamed nothing (e.g. a broker
                # with no history, or a symbol the broker serves empty). The
                # next scan would find the same gaps and do the same nothing,
                # so stop rather than re-scanning to the pass cap.
                log.warning(
                    "converge: pass made no progress (%d skipped) — stopping",
                    len(part.skipped),
                )
                break
        return SyncReport(
            requested=len(inst_list),
            fetched=fetched,
            written=written,
            failed=failed,
            elapsed_s=time.monotonic() - t0,
            converged=False,
            resumable=True,
        )

__all__ = [
    "SyncConfig",
    "SyncLocked",
    "SyncReport",
    "SyncService",
    "servable_bounds",
    "sync_lock",
]
