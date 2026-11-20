"""Read-only analytics view over the cached runtime.

The view is the single source a strategy reads indicator values from. For the
indicators that have an incremental implementation (SMA/EMA/RSI/Bollinger/MACD)
it extends a cached series **incrementally** — one new value per bar — instead of
recomputing the whole series on every bar. That is the difference between O(1)
and O(n) per bar (O(n^2) over a session), and it is what lets a live/paper loop
and a backtest share one code path without the backtest slowing as history grows.

Indicators without an incremental implementation, or called with a non-close
``source``, fall back to a full (version-cached) compute via ``compute_indicator``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from tradex_analytics.cache import BarStore, IndicatorCache, _freeze
from tradex_analytics.incremental import (
    StreamingBollinger,
    StreamingMacd,
    StreamingRSI,
    StreamingSMA,
    StreamingSmaSeededEma,
)


class _SingleValue:
    """Adapt a single-value incremental indicator to the multi-plot interface."""

    def __init__(self, indicator: Any) -> None:
        self._indicator = indicator

    def update(self, value: float) -> dict[str, float | None]:
        return {"value": self._indicator.update(value)}


def _period(params: dict) -> int:
    # Strategies pass the legacy alias ``period``; the registry's canonical name
    # is ``length``. Accept either.
    return int(params.get("period", params.get("length")))


def _make_sma(params: dict):
    return _SingleValue(StreamingSMA(_period(params)))


def _make_ema(params: dict):
    # The registry's ``ema`` is SMA-seeded (openalgo-charts smaSeededEma), NOT
    # the first-value EMA. Match it exactly or the view diverges from batch.
    return _SingleValue(StreamingSmaSeededEma(_period(params)))


def _make_rsi(params: dict):
    return _SingleValue(StreamingRSI(_period(params)))


def _make_bollinger(params: dict):
    return StreamingBollinger(_period(params), float(params["num_std"]))


def _make_macd(params: dict):
    return StreamingMacd(
        int(params.get("fast", 12)),
        int(params.get("slow", 26)),
        int(params.get("signal", 9)),
    )


#: Indicator id -> factory(params) -> object with ``update(value) -> dict``.
_INCREMENTAL = {
    "sma": _make_sma,
    "ema": _make_ema,
    "rsi": _make_rsi,
    "bollinger": _make_bollinger,
    "macd": _make_macd,
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

        # Only the close source is incremental; anything else falls back so the
        # result matches the batch compute exactly.
        if params.get("source", "close") != "close":
            return self._cache.get_or_compute(
                key, version, lambda: self._compute(name, series, params)
            )

        candles = series.candles
        if entry is None:
            try:
                indicator = _INCREMENTAL[name](params)
            except (KeyError, ValueError, TypeError):
                # Missing/invalid params (e.g. bollinger without num_std): fall back.
                return self._cache.get_or_compute(
                    key, version, lambda: self._compute(name, series, params)
                )
            result: dict[str, list] = {}
            for candle in candles:
                update = indicator.update(float(candle.ohlc.close.value))
                for plot, value in update.items():
                    result.setdefault(plot, []).append(value)
            entry = _IncrementalEntry(
                version=version, processed=len(candles), result=result,
                indicator=indicator,
            )
            self._incremental[key] = entry
            return entry.result

        # Same version handled above; here the series grew — extend each plot.
        for candle in candles[entry.processed:]:
            update = entry.indicator.update(float(candle.ohlc.close.value))
            for plot, values in entry.result.items():
                values.append(update.get(plot))
        entry.processed = len(candles)
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
