"""GapDetector — detect missing symbol/time ranges in the Parquet store.

Compares a requested instrument universe + date range against what already
exists in ``ParquetStorage``, returning the set of instruments that need
their gaps backfilled.

Adapted from nTrade's GapDetector.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

import pandas as pd

from tradex_trading.datalake.parquet_storage import ParquetStorage


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
    ) -> list[tuple]:
        """Return instruments with their missing date ranges.

        ``bar_freq`` defines the expected cadence for gap detection.
        """
        results: list[tuple] = []

        for inst in instruments:
            symbol = inst.symbol
            existing = self._store.read(
                symbols=[symbol], start=start, end=end, timeframe=timeframe,
            )

            if existing.empty:
                results.append((inst, [(start, end)]))
                continue

            expected = pd.date_range(start=start, end=end, freq=bar_freq)
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
