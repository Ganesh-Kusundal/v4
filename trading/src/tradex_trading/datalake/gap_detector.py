"""GapDetector — detect missing symbol/time ranges in the Parquet store.

Compares a requested instrument universe + date range against what already
exists in ``ParquetStorage``, returning the set of instruments that need
their gaps backfilled.

The expected-bar grid is session-aware: only weekday NSE sessions
(09:15–15:30 IST, minus optional exchange holidays) contribute candidate
timestamps, matching what ``ParquetStorage.read`` actually stores/strips.
Without this, nights/weekends/holidays were flagged as gaps and every
symbol looked incomplete (making backfill ``--skip-existing`` a no-op).

Adapted from nTrade's GapDetector.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta

import pandas as pd
from tradex_domain.market_calendar import MARKET_CLOSE, MARKET_OPEN

from tradex_trading.datalake.parquet_storage import ParquetStorage


def _session_grid(
    start: datetime,
    end: datetime,
    bar_freq: str,
    holidays: frozenset,
) -> pd.DatetimeIndex:
    """Expected bar timestamps for weekday sessions clipped to [start, end].

    Each trading day contributes ``bar_freq``-spaced stamps from
    ``MARKET_OPEN`` to ``MARKET_CLOSE`` (both inclusive, matching
    ParquetStorage's session filter), intersected with the requested window.

    Window edges are floored onto the ``bar_freq`` grid first: a caller-passed
    ``start`` like ``now - 90d`` (fractional seconds, mid-session) would
    otherwise generate off-grid stamps (e.g. ``12:17:14.706``) that never
    match stored exact-minute bars — flagging ~200 phantom missing bars per
    symbol on the window's first day, every run.
    """
    step = pd.Timedelta(bar_freq)
    start = pd.Timestamp(start).floor(step).to_pydatetime()
    end = pd.Timestamp(end).floor(step).to_pydatetime()

    stamps: list[pd.Timestamp] = []
    day = start.date()
    while day <= end.date():
        if day.weekday() < 5 and day not in holidays:
            lo = max(datetime.combine(day, MARKET_OPEN), start)
            hi = min(datetime.combine(day, MARKET_CLOSE), end)
            if lo <= hi:
                stamps.extend(pd.date_range(start=lo, end=hi, freq=bar_freq))
        day += timedelta(days=1)
    return pd.DatetimeIndex(stamps)


class GapDetector:
    """Detect gaps between a requested universe and stored data.

    Returns a list of ``(instrument, missing_ranges)`` tuples where
    ``missing_ranges`` is a list of ``(start, end)`` datetime pairs.
    """

    def __init__(self, store: ParquetStorage):
        self._store = store

    def detect(
        self,
        instruments: Iterable,
        start: datetime,
        end: datetime,
        timeframe: str = "5m",
        bar_freq: str = "5min",
        holidays: set[date] | None = None,
        min_gap_stamps: int = 1,
    ) -> list[tuple]:
        """Return instruments with their missing date ranges.

        ``bar_freq`` defines the expected cadence for gap detection.
        ``holidays`` optionally excludes exchange holidays from the expected
        grid (weekday sessions are always included).  ``min_gap_stamps``
        drops detected gaps shorter than N stamps — brokers sometimes serve
        truncated tails or drop stray closing bars; without a floor such
        noise flags every symbol as gapped and ``--skip-existing`` refetches
        the whole universe on every run.
        """
        results: list[tuple] = []
        holiday_set = frozenset(holidays) if holidays else frozenset()

        for inst in instruments:
            symbol = inst.symbol
            existing = self._store.read(
                symbols=[symbol], start=start, end=end, timeframe=timeframe,
            )

            if existing.empty:
                results.append((inst, [(start, end)]))
                continue

            expected = _session_grid(start, end, bar_freq, holiday_set)
            if not len(expected):
                continue
            existing_ts = pd.to_datetime(existing["timestamp"]).sort_values()
            existing_set = set(existing_ts)

            missing_times = [t for t in expected if t not in existing_set]
            if not missing_times:
                continue

            # Group consecutive missing timestamps into contiguous ranges
            gaps: list[tuple[datetime, datetime]] = []
            gap_start = missing_times[0]
            prev = gap_start
            for t in missing_times[1:]:
                if t != prev + pd.Timedelta(bar_freq):
                    gaps.append((gap_start, prev))
                    gap_start = t
                prev = t
            gaps.append((gap_start, prev))

            if min_gap_stamps > 1:
                step = pd.Timedelta(bar_freq)
                gaps = [
                    (gs, ge) for gs, ge in gaps
                    if (ge - gs) // step + 1 >= min_gap_stamps
                ]

            if gaps:
                results.append((inst, gaps))

        return results

    def missing_symbols(
        self,
        instruments: Iterable,
        start: datetime,
        end: datetime,
        timeframe: str = "5m",
    ) -> list:
        """Return just the instruments that have zero stored data."""
        return [
            inst for inst in instruments
            if self._store.read(
                symbols=[inst.symbol], start=start, end=end, timeframe=timeframe,
            ).empty
        ]

    def last_stored(self, symbol: str) -> datetime | None:
        """Latest timestamp stored for a symbol, or None."""
        rng = self._store.date_range(symbol)
        return rng[1] if rng else None

    def first_stored(self, symbol: str) -> datetime | None:
        """Earliest timestamp stored for a symbol, or None."""
        rng = self._store.date_range(symbol)
        return rng[0] if rng else None


__all__ = ["GapDetector"]
