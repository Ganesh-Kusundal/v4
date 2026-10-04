"""Read-only analytics view over the cached runtime."""

from __future__ import annotations

from typing import Any, Protocol

from tradex_analytics.cache import BarStore, IndicatorCache


class AnalyticsView(Protocol):
    """What a strategy sees: indicator values for one (instrument, timeframe)."""

    def indicator(self, name: str, **params: Any) -> dict[str, list]:
        """Latest computed indicator result (dict of plot -> list)."""
        ...


class CachedAnalyticsView:
    """View bound to one (instrument, timeframe), reading the shared cache."""

    def __init__(
        self, store: BarStore, cache: IndicatorCache, instrument_id, timeframe,
    ) -> None:
        self._store = store
        self._cache = cache
        self._instrument_id = instrument_id
        self._timeframe = timeframe

    def indicator(self, name: str, **params: Any) -> dict[str, list]:
        series = self._store.series(self._instrument_id, self._timeframe)
        if series is None:
            return {}
        version = self._store.version(self._instrument_id, self._timeframe)
        key = (
            str(self._instrument_id),
            self._timeframe,
            name.lower(),
            tuple(sorted(params.items())),
        )
        return self._cache.get_or_compute(
            key, version, lambda: self._compute(name, series, params),
        )

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

    def ingest(self, candle) -> None:
        self._store.ingest(candle)

    def view(self, instrument_id, timeframe) -> CachedAnalyticsView:
        return CachedAnalyticsView(self._store, self._cache, instrument_id, timeframe)
