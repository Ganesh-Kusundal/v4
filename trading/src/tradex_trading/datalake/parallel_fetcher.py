"""Parallel history fetcher — concurrent single-broker fetch with auto-chunk.

One broker per fetcher. Long intraday ranges are auto-chunked (Dhan 90d /
Upstox 30d) and stitched. No failover, no clipped-tail merge — those
lived here when multi-broker sync was the path; sync is Dhan-only now,
and Upstox tail repair is a separate script.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

from tradex_brokers.common.resilience import (
    MultiBucketRateLimiter,
    limiter_for_provider,
)
from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Instrument
from tradex_domain.market import HistoricalSeries
from tradex_domain.timeframe import DHAN_INTRADAY as _DHAN_INTRADAY_TIMEFRAMES

log = logging.getLogger(__name__)

ACQUIRE_TIMEOUT_S = 30.0

# ponytail: cap kept small — log already truncates to 5; 100 is enough for
# postmortem context. Grow only if ops asks for full failure lists.
ERROR_LOG_CAP = 100

_DHAN_INTRADAY_MAX_DAYS = 90
_UPSTOX_INTRADAY_MAX_DAYS = 30


def _date_windows(
    start: datetime, end: datetime, *, max_days: int,
) -> list[tuple[datetime, datetime]]:
    """Split [start, end] into consecutive windows of at most max_days."""
    if max_days <= 0:
        return [(start, end)]
    out: list[tuple[datetime, datetime]] = []
    cur = start
    while cur < end:
        nxt = min(end, cur + timedelta(days=max_days))
        out.append((cur, nxt))
        cur = nxt
    if not out:
        return [(start, end)]
    return out


def _chunk_cap_for(timeframe: Timeframe | str, broker_name: str) -> int | None:
    """Return max window days if this brokerage+timeframe must be chunked."""
    tf = Timeframe(timeframe) if isinstance(timeframe, str) else timeframe
    if tf not in _DHAN_INTRADAY_TIMEFRAMES:
        return None
    if broker_name == "dhan":
        return _DHAN_INTRADAY_MAX_DAYS
    return _UPSTOX_INTRADAY_MAX_DAYS


def _default_workers(broker_name: str) -> int:
    """Provider-aware concurrency: Dhan burst walls sit below 5/s after ~100 calls."""
    if broker_name == "dhan":
        return 2
    return 4


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
) -> dict[str, HistoricalSeries]:
    """Fetch a batch with exponential backoff on rate-limit bursts."""
    pending = list(batch)
    merged: dict[str, HistoricalSeries] = {}
    backoff = 30.0
    zero_progress_streak = 0
    if dead_symbols is None:
        dead_symbols = set()
    for attempt in range(1, max_retries + 1):
        if not pending:
            break
        pending = [
            i for i in pending
            if str(i.instrument_id) not in dead_symbols
        ]
        if not pending:
            break
        results, errors = fetcher.fetch(
            pending, tf, c_start, c_end, ranges=ranges,
        )
        merged.update(results)
        succeeded_ids = set(results.keys())
        still_pending = [
            inst for inst in pending
            if str(inst.instrument_id) not in succeeded_ids
        ]
        if not still_pending:
            break
        if len(still_pending) < len(pending):
            zero_progress_streak = 0
            backoff = 30.0
        else:
            zero_progress_streak += 1
            if zero_progress_streak >= 2:
                log.warning(
                    "fetch_with_backoff: no progress twice — giving up on "
                    "%d symbol(s)",
                    len(still_pending),
                )
                break
        if not _has_transient_errors(errors):
            log.warning(
                "fetch_with_backoff: permanent failures only — "
                "giving up on %d symbol(s)",
                len(still_pending),
            )
            dead_symbols.update(str(i.instrument_id) for i in still_pending)
            break
        log.warning(
            "fetch_with_backoff: rate-limited (attempt %d/%d) — sleeping %.0fs",
            attempt, max_retries, backoff,
        )
        time.sleep(backoff)
        backoff = min(backoff * 2, 480.0)
        pending = still_pending
    return merged


def _is_transient_error(error_msg: str) -> bool:
    permanent_markers = ("empty stitched", "empty series", "empty failover")
    if any(m in error_msg for m in permanent_markers):
        return False
    return True


def _has_transient_errors(errors: list[str]) -> bool:
    return any(_is_transient_error(e) for e in errors)


def _provider_for(name: str) -> str:
    if name in ("dhan", "upstox", "paper"):
        return name
    log.warning(
        "ParallelHistoryFetcher: broker %r is not a known provider — "
        "using the paper (unthrottled) rate-limit table",
        name,
    )
    return "paper"


class ParallelHistoryFetcher:
    """Fetch historical data for many instruments concurrently from one broker.

    Parameters
    ----------
    brokers : dict[str, Any]
        Exactly one named broker adapter, e.g. ``{"dhan": dhan_broker}``.
    max_workers : int
        Concurrent fetch threads.
    rate_limiter : MultiBucketRateLimiter | None
        Optional override; default uses the broker's ``.rate_limiter`` or a
        provider table built from the dict key.
    """

    def __init__(
        self,
        brokers: dict[str, Any],
        max_workers: int = 4,
        rate_limiter: MultiBucketRateLimiter | None = None,
    ) -> None:
        if len(brokers) != 1:
            raise ValueError(
                "ParallelHistoryFetcher requires exactly one broker "
                f"(got {sorted(brokers)!r})"
            )
        self._brokers = brokers
        self._broker_name, self._broker = next(iter(brokers.items()))
        self._max_workers = max_workers
        self._rate_limiter = rate_limiter
        self._explicit_limiter = rate_limiter is not None
        self._limiters: dict[str, Any] = {}
        self._prefetch_gate: dict[str, bool] = {}

        name, broker = self._broker_name, self._broker
        if rate_limiter is not None:
            self._limiters[name] = rate_limiter
            self._prefetch_gate[name] = True
        else:
            shared = getattr(broker, "rate_limiter", None)
            if shared is not None:
                self._limiters[name] = shared
                self._prefetch_gate[name] = False
            else:
                self._limiters[name] = limiter_for_provider(_provider_for(name))
                self._prefetch_gate[name] = True

    def _maybe_acquire(self, inst_id: str) -> None:
        if not self._prefetch_gate.get(self._broker_name, True):
            return
        limiter = self._limiters[self._broker_name]
        if not limiter.acquire("historical", timeout=ACQUIRE_TIMEOUT_S):
            log.warning(
                "ParallelHistoryFetcher: rate-limit gate timed out for "
                "%s via %s — proceeding anyway",
                inst_id, self._broker_name,
            )

    def fetch(
        self,
        instruments: list[Instrument],
        timeframe: Timeframe | str,
        start: datetime,
        end: datetime,
        ranges: dict[str, list[tuple[datetime, datetime]]] | None = None,
    ) -> tuple[dict[str, HistoricalSeries], list[str]]:
        """Fetch history for all instruments from the sole broker.

        Returns ``(results, errors)`` keyed by instrument_id.
        """
        if not instruments:
            return {}, []
        if isinstance(timeframe, str):
            timeframe = Timeframe(timeframe)
        self._timeframe = timeframe
        self._req_start, self._req_end = start, end

        days = (end - start).days
        cap = _chunk_cap_for(timeframe, self._broker_name)
        if cap is not None and days > cap:
            log.info(
                "ParallelHistoryFetcher: auto-chunking %d-day %s into %d-day windows",
                days, timeframe, cap,
            )
        log.info(
            "ParallelHistoryFetcher: %d instruments (%d ranged), %d days, broker=%s",
            len(instruments), len(ranges or {}), days, self._broker_name,
        )

        def _windows_for(inst: Instrument) -> list[tuple[datetime, datetime]] | None:
            max_days = _chunk_cap_for(self._timeframe, self._broker_name)
            inst_ranges = (ranges or {}).get(str(inst.instrument_id))
            if inst_ranges is not None:
                md = max_days or 0
                return [
                    w for r_start, r_end in inst_ranges
                    for w in _date_windows(r_start, r_end, max_days=md)
                ]
            if max_days and days > max_days:
                return _date_windows(start, end, max_days=max_days)
            return None

        results: dict[str, HistoricalSeries] = {}
        lock = threading.Lock()
        errors: list[str] = []

        def _fetch_one(inst: Instrument) -> None:
            try:
                windows = _windows_for(inst)
                if windows is not None:
                    series = self._stitch_windows(inst, windows)
                    if series is None or not series.candles:
                        raise RuntimeError(
                            f"{self._broker_name}: empty stitched series "
                            f"for {inst.instrument_id}"
                        )
                else:
                    self._maybe_acquire(str(inst.instrument_id))
                    series = self._broker.history(inst, timeframe, start, end)
                    if series is None or not series.candles:
                        raise RuntimeError(
                            f"{self._broker_name}: empty series for {inst.instrument_id}"
                        )
                with lock:
                    results[str(inst.instrument_id)] = series
            except Exception as exc:
                with lock:
                    if len(errors) < ERROR_LOG_CAP:
                        errors.append(f"{inst.instrument_id}: {exc}")

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = [pool.submit(_fetch_one, inst) for inst in instruments]
            for f in as_completed(futures):
                f.result()

        if errors:
            log.warning("ParallelHistoryFetcher: %d failures: %s", len(errors), errors[:5])
        log.info(
            "ParallelHistoryFetcher: %d/%d succeeded",
            len(results), len(instruments),
        )
        return results, errors

    def _stitch_windows(
        self,
        inst: Instrument,
        windows: list[tuple[datetime, datetime]],
    ) -> HistoricalSeries | None:
        stitched: list = []
        for ws, we in windows:
            self._maybe_acquire(str(inst.instrument_id))
            part = self._broker.history(inst, self._timeframe, ws, we)
            if part is not None and part.candles:
                stitched.extend(part.candles)
        if not stitched:
            return None
        seen: set = set()
        deduped: list = []
        for c in sorted(stitched, key=lambda x: x.timestamp):
            if c.timestamp not in seen:
                seen.add(c.timestamp)
                deduped.append(c)
        return HistoricalSeries(
            instrument=inst, timeframe=self._timeframe, candles=deduped,
            start=self._req_start, end=self._req_end,
        )

    @property
    def broker_names(self) -> list[str]:
        return [self._broker_name]


__all__ = [
    "ParallelHistoryFetcher",
    "_default_workers",
    "fetch_with_backoff",
]
