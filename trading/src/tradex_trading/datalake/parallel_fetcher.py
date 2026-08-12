"""Parallel history fetcher — concurrent multi-instrument, multi-broker.

Routes by date range:
  < 30 days → split instruments across all brokers (both serve fast)
  >= 30 days → Dhan only (90-day chunks = fewer API calls)

Each broker's existing rate limiter handles throttling — the fetcher just
fans out work across ThreadPoolExecutor.  If a broker fails for a symbol,
the remaining brokers get a chance (failover).

Failover fan-out is bounded: once a broker fails for any symbol in a batch
it is skipped as a failover target for the rest of the batch, so a
broker-wide outage costs ~M + N calls instead of N × M.  Trade-off: a
per-symbol failure (e.g. an instrument missing from one broker's registry)
also blacklists that broker for the batch — later symbols lose that
failover path.  Accepted for a backfill tool, where GapDetector re-checks
missed symbols on the next run.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Instrument
from tradex_domain.market import HistoricalSeries

log = logging.getLogger(__name__)

# ponytail: 30-day threshold.  Dhan serves 90 days/call, Upstox 1 month.
# Below 30 days both brokers are equally efficient per-call, so splitting
# instruments across N brokers gives ~Nx throughput.  Above 30 days Dhan's
# larger chunk size means fewer total API calls.
_DUAL_BROKER_THRESHOLD_DAYS = 30


def _split(items: list, n: int) -> list[list]:
    """Split *items* into *n* roughly-equal chunks."""
    if n <= 0:
        return [items]
    k, m = divmod(len(items), n)
    return [items[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n)]


class ParallelHistoryFetcher:
    """Fetch historical data for many instruments concurrently.

    Parameters
    ----------
    brokers : dict[str, Any]
        Named broker adapters, e.g. ``{"dhan": dhan_broker, "upstox": upstox_broker}``.
        Each must implement ``history(instrument, timeframe, start, end)``.
    max_workers : int
        Total concurrent fetch threads (default 4 — matches Dhan DATA quota
        of 5/s with one slot free for gate overhead).
    """

    def __init__(self, brokers: dict[str, Any], max_workers: int = 4) -> None:
        self._brokers = brokers
        self._max_workers = max_workers

    # ------------------------------------------------------------------ public

    def fetch(
        self,
        instruments: list[Instrument],
        timeframe: Timeframe | str,
        start: datetime,
        end: datetime,
    ) -> dict[str, HistoricalSeries]:
        """Fetch history for all instruments.  Returns ``{symbol: HistoricalSeries}``.

        Routing:
          - date range < 30 days → split instruments across all brokers
          - date range >= 30 days → Dhan only (fewer API calls via 90-day chunks)
        """
        if not instruments:
            return {}
        if isinstance(timeframe, str):
            timeframe = Timeframe(timeframe)

        days = (end - start).days
        broker_names = self._pick_brokers(days)
        log.info("ParallelHistoryFetcher: %d instruments, %d days, brokers=%s",
                 len(instruments), days, broker_names)

        # Split instruments across selected brokers
        active_brokers = {n: self._brokers[n] for n in broker_names}
        chunks = _split(instruments, len(active_brokers))
        broker_assignments: list[tuple[str, Any, list[Instrument]]] = []
        for i, name in enumerate(broker_names):
            if i < len(chunks) and chunks[i]:
                broker_assignments.append((name, active_brokers[name], chunks[i]))

        # Parallel fetch
        results: dict[str, HistoricalSeries] = {}
        lock = threading.Lock()
        errors: list[str] = []
        #: Brokers that raised for at least one symbol this batch. Once a
        #: broker proves broken we stop routing failover work to it, so a
        #: broker-wide outage costs ~M + N calls instead of N x M.
        failed_brokers: set[str] = set()

        def _fetch_one(broker_name: str, broker: Any, inst: Instrument) -> None:
            first_exc: Exception | None = None
            try:
                series = broker.history(inst, timeframe, start, end)
                if series is not None and len(series.candles) > 0:
                    with lock:
                        results[str(inst.instrument_id)] = series
                    return
            except Exception as exc:
                first_exc = exc
                with lock:
                    failed_brokers.add(broker_name)
            # Failover: try remaining brokers, skipping any already known-failed
            for other_name, other_broker in active_brokers.items():
                if other_name == broker_name:
                    continue
                with lock:
                    if other_name in failed_brokers:
                        continue
                try:
                    series = other_broker.history(inst, timeframe, start, end)
                    if series is not None and len(series.candles) > 0:
                        with lock:
                            results[str(inst.instrument_id)] = series
                        return
                except Exception as exc:
                    first_exc = first_exc or exc
                    with lock:
                        failed_brokers.add(other_name)
            with lock:
                errors.append(f"{inst.instrument_id}: all brokers failed ({first_exc})")

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = []
            for broker_name, broker, chunk in broker_assignments:
                for inst in chunk:
                    futures.append(pool.submit(_fetch_one, broker_name, broker, inst))
            for f in as_completed(futures):
                f.result()  # propagate unexpected exceptions

        if errors:
            log.warning("ParallelHistoryFetcher: %d failures: %s", len(errors), errors[:5])
        log.info("ParallelHistoryFetcher: %d/%d succeeded", len(results), len(instruments))
        return results

    # ------------------------------------------------------------------ routing

    def _pick_brokers(self, days: int) -> list[str]:
        """Select brokers based on date range.

        < 30 days → all available brokers (split instruments for speed)
        >= 30 days → Dhan only (90-day chunks = fewer calls)
        """
        names = list(self._brokers.keys())
        if days < _DUAL_BROKER_THRESHOLD_DAYS:
            return names  # use all brokers
        # Prefer dhan for longer ranges (90-day chunks vs Upstox 1-month)
        if "dhan" in names:
            return ["dhan"]
        return names  # fallback: whatever we have

    # ------------------------------------------------------------------ info

    @property
    def broker_names(self) -> list[str]:
        return list(self._brokers.keys())


__all__ = ["ParallelHistoryFetcher"]
