"""Canonical bar store and indicator cache for shared analytics."""

from __future__ import annotations

from collections.abc import Callable

from tradex_domain.enums import Timeframe
from tradex_domain.market import Candle, HistoricalSeries
from tradex_domain.value_objects import InstrumentId


class BarStore:
    """One canonical candle series per (instrument, timeframe).

    Fed by the engine as candles arrive. A version counter per key
    invalidates cached indicator results when the series changes (a new
    bar, or a late-tick revision — the version bumps on every ingest).
    """

    def __init__(self) -> None:
        self._series: dict[tuple[InstrumentId, Timeframe], HistoricalSeries] = {}
        self._versions: dict[tuple[InstrumentId, Timeframe], int] = {}

    def ingest(self, candle: Candle) -> None:
        key = (candle.instrument.instrument_id, candle.timeframe)
        self._versions[key] = self._versions.get(key, 0) + 1
        series = self._series.get(key)
        if series is None:
            self._series[key] = HistoricalSeries(
                instrument=candle.instrument,
                timeframe=candle.timeframe,
                candles=[candle],
                start=candle.timestamp,
                end=candle.timestamp,
            )
        else:
            self._series[key] = HistoricalSeries(
                instrument=series.instrument,
                timeframe=series.timeframe,
                candles=[*series.candles, candle],
                start=series.start,
                end=candle.timestamp,
            )

    def series(self, instrument_id: InstrumentId, timeframe: Timeframe) -> HistoricalSeries | None:
        return self._series.get((instrument_id, timeframe))

    def version(self, instrument_id: InstrumentId, timeframe: Timeframe) -> int:
        return self._versions.get((instrument_id, timeframe), 0)


class IndicatorCache:
    """Caches the latest indicator result per key, keyed by bar-version.

    ``get_or_compute`` returns the cached result when the version matches,
    else recomputes and overwrites (only the latest version is kept, so
    memory is bounded by the result size, not the version count).
    """

    def __init__(self) -> None:
        self._entries: dict[tuple, tuple[int, dict]] = {}

    def get_or_compute(
        self, key: tuple, version: int, compute_fn: Callable[[], dict],
    ) -> dict:
        entry = self._entries.get(key)
        if entry is not None and entry[0] == version:
            return entry[1]
        result = compute_fn()
        self._entries[key] = (version, result)
        return result
