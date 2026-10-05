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
from tradex_domain.market_calendar import to_ist_naive

from tradex_market_data.gap_detector import GapDetector, GapScan
from tradex_market_data.parallel_fetcher import (
    ParallelHistoryFetcher,
    fetch_with_backoff,
)
from tradex_market_data.simple_sync import series_to_frame

log = logging.getLogger(__name__)

_EDGE_SLACK = timedelta(minutes=1)


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
            instruments,
            start=start,
            end=end,
            timeframe=self._config.timeframe,
            bar_freq="1min",
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
        return first <= start + _EDGE_SLACK and last >= end - _EDGE_SLACK

    def _fetch_bounded(
        self,
        fetcher: ParallelHistoryFetcher,
        batch: list,
        start: datetime,
        end: datetime,
        deadline: float,
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

    def sync(self, instruments, start, end) -> SyncReport:
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
        batches_list = list(batched(inst_list, self._config.batch_size))
        for bi, batch in enumerate(batches_list, 1):
            if time.monotonic() >= deadline:
                break
            batches = bi
            results = self._fetch_bounded(fetcher, list(batch), start, end, deadline)
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

    def converge(self, instruments, start, end) -> SyncReport:
        inst_list = list(instruments)
        t0 = time.monotonic()
        deadline = t0 + self._config.budget_seconds
        fetched = written = 0
        failed: dict[str, str] = {}
        for _ in range(10):  # bounded passes; budget is the real cap
            scan = self.scan(inst_list, start, end)
            if not scan.gaps:
                return SyncReport(
                    requested=len(inst_list),
                    fetched=fetched,
                    written=written,
                    failed=failed,
                    elapsed_s=time.monotonic() - t0,
                    converged=True,
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
            requested=len(inst_list),
            fetched=fetched,
            written=written,
            failed=failed,
            elapsed_s=time.monotonic() - t0,
            converged=False,
            resumable=True,
        )

    def repair_tail(self, instruments, start, end) -> SyncReport:
        # The detector classifies the unrepairable close; the open is
        # repairable. Compose the Upstox pass over the edge-only symbols.
        scan = GapDetector(self._store).scan(
            instruments,
            start=start,
            end=end,
            timeframe=self._config.timeframe,
            bar_freq="1min",
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


__all__ = ["SyncConfig", "SyncReport", "SyncService"]
