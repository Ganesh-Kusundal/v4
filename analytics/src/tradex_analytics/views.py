"""Read-only analytics view over the cached runtime.

The view is the single source a strategy reads indicator values from. For the
streaming indicators (SMA/EMA/RSI) it extends a cached series **incrementally**
— one new value per bar — instead of recomputing the whole series on every bar.
That is the difference between O(1) and O(n) per bar (O(n²) over a session), and
it is what lets a live/paper loop and a backtest share one code path without the
backtest slowing to a crawl as history grows. Indicators without an incremental
implementation fall back to a full (version-cached) compute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from tradex_analytics.cache import BarStore, IndicatorCache, _freeze
from tradex_analytics.incremental import StreamingEMA, StreamingRSI, StreamingSMA

#: Indicators with an incremental implementation, mapped to their state class.
#: Each keeps O(1) state and emits one value per ``update`` (the plot key is
#: always ``"value"`` for these three).
_INCREMENTAL: dict[str, type] = {
    "sma": StreamingSMA,
    "ema": StreamingEMA,
    "rsi": StreamingRSI,
}


@dataclass
class _IncrementalEntry:
    """One indicator's incremental state plus the series it has consumed."""

    version: int
    processed: int
    result: dict[str, list]
    indicator: Any = field(repr=False)


class AnalyticsView(Protocol):
    """What a strategy sees: indicator values for one (instrument, timeframe)."""

    def indicator(self, name: str, **params: Any) -> dict[str, list]:
        """Latest computed indicator result (dict of plot -> list)."""
        ...


class CachedAnalyticsView:
    """View bound to one (instrument, timeframe), reading the shared cache."""

    def __init__(
        self, store: BarStore, cache: IndicatorCache, instrument_id, timeframe,
        incremental: dict[tuple, _IncrementalEntry] | None = None,
    ) -> None:
        self._store = store
        self._cache = cache
        self._instrument_id = instrument_id
        self._timeframe = timeframe
        self._incremental = incremental if incremental is not None else {}

    def indicator(self, name: str, **params: Any) -> dict[str, list]:
        series = self._store.series(self._instrument_id, self._timeframe)
        if series is None:
            return {}
        version = self._store.version(self._instrument_id, self._timeframe)
        key = (
            str(self._instrument_id),
            self._timeframe,
            name.lower(),
            tuple(sorted((k, _freeze(v)) for k, v in params.items())),
        )
        if name.lower() in _INCREMENTAL:
            return self._incremental_indicator(name.lower(), key, version, series, params)
        return self._cache.get_or_compute(
            key, version, lambda: self._compute(name, series, params),
        )

    def _incremental_indicator(
        self, name: str, key: tuple, version: int, series, params: dict,
    ) -> dict[str, list]:
        """Extend (or build) an incremental series; O(1) per new bar."""
        entry = self._incremental.get(key)
        if entry is not None and entry.version == version:
            return entry.result

        period = params.get("period")
        if entry is None:
            if period is None:
                # No period to build an incremental indicator with — fall back.
                return self._cache.get_or_compute(
                    key, version, lambda: self._compute(name, series, params)
                )
            indicator = _INCREMENTAL[name](int(period))
            values: list = [
                indicator.update(float(c.ohlc.close.value)) for c in series.candles
            ]
            entry = _IncrementalEntry(
                version=version,
                processed=len(series.candles),
                result={"value": values},
                indicator=indicator,
            )
            self._incremental[key] = entry
            return entry.result

        # Same version already handled above; here the series grew.
        for candle in series.candles[entry.processed:]:
            entry.result["value"].append(
                entry.indicator.update(float(candle.ohlc.close.value))
            )
        entry.processed = len(series.candles)
        entry.version = version
        return entry.result

    @staticmethod
    def _compute(name: str, series, params: dict) -> dict:
        from tradex_analytics.indicators import compute_indicator
        from tradex_analytics.registry import REGISTRY

        return compute_indicator(name, series.candles, params, REGISTRY)


class AnalyticsRuntime:
    """Composes BarStore + IndicatorCache and feeds them from candles."""

    def __init__(self) -> None:
        self._store = BarStore()
        self._cache = IndicatorCache()
        #: Incremental indicator state, shared across views so a new view (one
        #: per bar) extends the same series rather than restarting it.
        self._incremental: dict[tuple, _IncrementalEntry] = {}

    def ingest(self, candle) -> None:
        self._store.ingest(candle)

    def view(self, instrument_id, timeframe) -> CachedAnalyticsView:
        return CachedAnalyticsView(
            self._store, self._cache, instrument_id, timeframe, self._incremental
        )
