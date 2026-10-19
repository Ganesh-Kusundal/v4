"""SyncService — one bounded, resumable datalake sync path.

Composes GapDetector (detect) + ParallelHistoryFetcher (fetch) +
ParquetStorage (store). Threads a wall-clock deadline through every retry
so a run cannot hang, and validates that each fetched series covers the
window it was asked for before writing it.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from itertools import batched
from typing import Any

import pandas as pd
from tradex_domain.market import HistoricalSeries
from tradex_domain.market_calendar import MARKET_CLOSE, to_ist_naive

from tradex_market_data.gap_detector import GapDetector, GapScan
from tradex_market_data.parallel_fetcher import (
    ParallelHistoryFetcher,
    fetch_with_backoff,
)

log = logging.getLogger(__name__)

_EDGE_SLACK = timedelta(minutes=1)


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

#: Dhan's NSE 1m series stops at 15:14 (measured, see CLAUDE.md); Upstox serves
#: to 15:29. A window ending at the session close can therefore never be
#: covered to its last minute, and demanding that would mark every symbol
#: partial_response and write nothing. 16 min = the close minus 15:14.
_VENUE_TAIL_SLACK = timedelta(minutes=16)


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
    #: Symbols whose only remaining shortfall is a hole under ``min_gap_stamps``.
    #: The scan cannot see them, so ``converged`` must not be declared over
    #: them: a rerun at the same floor is a no-op, which is why this is
    #: reported rather than retried.
    under_floored: tuple[str, ...] = ()


class SyncService:
    def __init__(self, broker: Any, store: Any, config: SyncConfig) -> None:
        self._broker = broker
        self._store = store
        self._config = config

    def scan(self, instruments, start, end, *, holidays=None) -> GapScan:
        return GapDetector(self._store).scan(
            instruments,
            start=start,
            end=end,
            timeframe=self._config.timeframe,
            bar_freq="1min",
            holidays=holidays,
            min_gap_stamps=self._config.min_gap_stamps,
            tail_days=self._config.tail_days,
            include_open_stamps=self._config.include_open_stamps,
        )

    def _validate_coverage(
        self,
        series: HistoricalSeries,
        start: datetime,
        end: datetime,
    ) -> bool:
        if not series.candles:
            return False
        # Broker candles are tz-aware UTC; the window is IST-naive. Compare in
        # IST-naive space (the datalake contract) — stripping tzinfo from a
        # UTC stamp would shift every session 5.5h and fail coverage.
        # Allow one bar of slack at each edge (broker session conventions).
        first = to_ist_naive(series.candles[0].timestamp)
        last = to_ist_naive(series.candles[-1].timestamp)
        # Only demand the venue's unservable tail when the window actually
        # reaches the session close; a window ending mid-session must still be
        # covered to its own end.
        required_end = end
        if end.time() >= MARKET_CLOSE:
            required_end = end - _VENUE_TAIL_SLACK
        return first <= start + _EDGE_SLACK and last >= required_end - _EDGE_SLACK

    def _covers_ranges(
        self,
        series: HistoricalSeries,
        ranges: list[tuple[datetime, datetime]],
    ) -> bool:
        """Every requested range is reached at both ends.

        A hole's endpoints are interior session stamps (the unservable 15:30
        close never enters ``gaps``), so both are required — checking only the
        start would accept a response truncated halfway through the hole.
        """
        if not series.candles:
            return False
        stamps = [to_ist_naive(c.timestamp) for c in series.candles]
        return all(
            any(t <= r_start + _EDGE_SLACK for t in stamps)
            and any(t >= r_end - _EDGE_SLACK for t in stamps)
            for r_start, r_end in ranges
        )

    def _fetch_bounded(
        self,
        fetcher: ParallelHistoryFetcher,
        batch: list,
        start: datetime,
        end: datetime,
        deadline: float,
        ranges: dict[str, list[tuple[datetime, datetime]]] | None = None,
    ) -> dict[str, HistoricalSeries]:
        """Fetch one batch, abandoning it at the wall-clock deadline.

        The pre-batch deadline check in ``sync`` cannot bound an in-flight
        fetch — a broker call that blocks past the budget would hang the
        run. So the fetch runs in a worker thread and is given at most the
        remaining budget; on timeout its results are discarded and the
        batch's symbols land in ``skipped`` (resumable on the next run).
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {}
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sync-batch")
        try:
            future = pool.submit(
                fetch_with_backoff,
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
            return future.result(timeout=remaining)
        except TimeoutError:
            log.warning(
                "sync: budget exhausted mid-batch — abandoning %d symbol(s)",
                len(batch),
            )
            return {}
        finally:
            pool.shutdown(wait=False)

    def sync(
        self,
        instruments,
        start,
        end,
        *,
        ranges: dict[str, list[tuple[datetime, datetime]]] | None = None,
    ) -> SyncReport:
        inst_list = list(instruments)
        if not inst_list:
            return SyncReport(0, 0, 0)
        t0 = time.monotonic()
        deadline = t0 + self._config.budget_seconds
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
                len(inst_list),
            )
            return SyncReport(
                requested=len(inst_list),
                fetched=0,
                written=0,
                skipped=[i.symbol for i in inst_list],
                elapsed_s=time.monotonic() - t0,
            )
        batches_list = list(batched(inst_list, self._config.batch_size))
        for bi, batch in enumerate(batches_list, 1):
            if time.monotonic() >= deadline:
                break
            batches = bi
            results = self._fetch_bounded(
                fetcher, list(batch), start, end, deadline, ranges,
            )
            frames: list[pd.DataFrame] = []
            for inst in batch:
                series = results.get(str(inst.instrument_id))
                if series is None or not series.candles:
                    skipped.append(inst.symbol)
                    continue
                if self._config.validate_coverage:
                    # A ranged fetch returns only the requested hole, so
                    # coverage is checked against those ranges — not the whole
                    # window, which it deliberately never asked for.
                    inst_ranges = (ranges or {}).get(str(inst.instrument_id))
                    covered = (
                        self._covers_ranges(series, inst_ranges)
                        if inst_ranges
                        else self._validate_coverage(series, start, end)
                    )
                    if not covered:
                        failed[inst.symbol] = "partial_response"
                        continue
                frames.append(series_to_frame(series, inst.symbol))
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

    def converge(self, instruments, start, end, *, holidays=None) -> SyncReport:
        inst_list = list(instruments)
        t0 = time.monotonic()
        deadline = t0 + self._config.budget_seconds
        fetched = written = 0
        failed: dict[str, str] = {}
        #: Symbols a pass has already tried and left still-short. A truncated
        #: response reproduces on every attempt, so re-fetching it only burns
        #: the budget; track it and stop re-selecting it.
        retried: set[str] = set()
        for _ in range(10):  # bounded passes; budget is the real cap
            scan = self.scan(inst_list, start, end, holidays=holidays)
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
                part = self.sync(gapped, start, end, ranges=gap_map)
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

__all__ = ["SyncConfig", "SyncReport", "SyncService"]
