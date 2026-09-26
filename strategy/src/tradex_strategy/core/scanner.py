"""Scanner engine — condition evaluation + ranking over a market provider."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from tradex_domain import IndicatorComputer, ScannerDefinition, ScannerResult
from tradex_domain.enums import Timeframe
from tradex_domain.errors import SDKError
from tradex_domain.market import Candle, HistoricalSeries
from tradex_domain.strategy import Condition

_OPS = {
    ">": lambda v, t: v > t,
    ">=": lambda v, t: v >= t,
    "<": lambda v, t: v < t,
    "<=": lambda v, t: v <= t,
    "==": lambda v, t: v == t,
    "!=": lambda v, t: v != t,
}

# Default cap for the per-instrument streaming buffer.  30 bars matches the
# default ``window_days`` (D1) and keeps memory bounded at ~30 candles × N
# instruments.  Callers can override via ``max_bars``.
_DEFAULT_MAX_BARS = 30


class ScannerEngine:
    """Evaluates ``Condition`` values over a default D1 window per instrument.

    Supports two data paths:

    * **Snapshot** (legacy): ``_history()`` fetches from the market provider
      on every scan.  Still used as fallback when no bars have been streamed.
    * **Streaming** (G7): call ``consume(candle)`` to append bars into a
      per-instrument rolling buffer.  Once the buffer has data, ``_history()``
      returns it directly — no market round-trip.
    """

    def __init__(
        self,
        market: Any,
        analytics: IndicatorComputer | None = None,
        window_days: int = 30,
        max_bars: int = _DEFAULT_MAX_BARS,
    ) -> None:
        self._market = market
        if analytics is None:
            from tradex_analytics.engine import AnalyticsEngine
            analytics = AnalyticsEngine()  # type: ignore[assignment]
        self._analytics: IndicatorComputer = analytics  # type: ignore[assignment]
        self._window_days = window_days
        self._max_bars = max_bars
        # Per-instrument rolling buffer keyed by instrument symbol.
        self._buffers: dict[str, deque[Candle]] = defaultdict(
            lambda: deque(maxlen=self._max_bars),
        )

    # -- streaming entry point ---------------------------------------------------

    def consume(self, candle: Candle) -> None:
        """Append *candle* to the per-instrument rolling buffer.

        This is the public entry point for the bar stream (bus subscription,
        aggregator callback, etc.).  The buffer is bounded by ``max_bars``;
        oldest bars are dropped automatically by the underlying ``deque``.
        """
        key = _instrument_key(candle.instrument)
        self._buffers[key].append(candle)

    def run(self, definition: ScannerDefinition) -> list[ScannerResult]:
        """Evaluate all conditions and return ranked results."""
        results: list[ScannerResult] = []
        for instrument in definition.universe:
            series = self._history(instrument)
            result = self._evaluate(instrument, series, definition.conditions)
            results.append(result)
        results.sort(key=lambda r: (-r.score, r.instrument.symbol))
        return [
            ScannerResult(
                instrument=r.instrument,
                score=r.score,
                matched_conditions=r.matched_conditions,
                indicator_values=r.indicator_values,
                rank=i + 1,
                metadata=r.metadata,
            )
            for i, r in enumerate(results)
        ]

    def top(self, definition: ScannerDefinition, limit: int = 20) -> list[ScannerResult]:
        """Return the top-*limit* results from ``run()``."""
        return self.run(definition)[:limit]

    # -- internals ---------------------------------------------------------------

    def _history(self, instrument: Any) -> HistoricalSeries:
        key = _instrument_key(instrument)
        buf = self._buffers.get(key)
        if buf:
            # Streaming path — return buffered candles directly.
            candles = list(buf)
            return HistoricalSeries(
                instrument=instrument,
                timeframe=Timeframe.D1,
                candles=candles,
                start=candles[0].timestamp,
                end=candles[-1].timestamp,
            )
        # Snapshot fallback — no bars streamed yet; hit the market provider.
        end = datetime.now(UTC)
        start = end - timedelta(days=self._window_days)
        try:
            return self._market.history(instrument, Timeframe.D1, start, end)
        except SDKError:
            return HistoricalSeries(
                instrument=instrument,
                timeframe=Timeframe.D1,
                candles=[],
                start=start,
                end=end,
            )

    def _evaluate(
        self,
        instrument: Any,
        series: HistoricalSeries,
        conditions: list[Condition],
    ) -> ScannerResult:
        values = {cond.name: self._value(series, cond.name, cond.params) for cond in conditions}
        matched = [cond.name for cond in conditions if self._matches(cond, values.get(cond.name))]
        score = len(matched) / len(conditions) if conditions else 1.0
        return ScannerResult(
            instrument=instrument,
            score=score,
            matched_conditions=matched,
            indicator_values={k: float(v) for k, v in values.items()},
            rank=0,  # assigned by run()
        )

    def _value(self, series: HistoricalSeries, name: str, params: dict[str, Any]) -> float:
        if not series.candles:
            return 0.0
        if name == "close":
            return float(series.candles[-1].ohlc.close.value)
        # Raw-values surface: signed indicators (roc, macd) are legitimate
        # condition inputs but cannot ride the Price-typed close of a
        # HistoricalSeries. Prefer indicator_values when the engine provides
        # it; fall back to close-wrapping for engines that only implement the
        # base protocol (their price-ranged indicators still work).
        get_values = getattr(self._analytics, "indicator_values", None)
        if callable(get_values):
            values = cast(
                "list[float | None]",
                get_values(series, name, **params),
            )
            tail = next((v for v in reversed(values) if v is not None), None)
            return float(tail) if tail is not None else 0.0
        indicator = cast(HistoricalSeries, self._analytics.indicator(series, name, **params))
        if not indicator.candles:
            return 0.0
        return float(indicator.candles[-1].ohlc.close.value)

    @staticmethod
    def _matches(cond: Condition, value: float | None) -> bool:
        if value is None or cond.threshold is None:
            return False
        op = _OPS.get(cond.operator)
        if op is None:
            return False
        return bool(op(value, cond.threshold))


def _instrument_key(instrument: Any) -> str:
    """Stable hashable key for per-instrument buffer lookup."""
    return getattr(instrument, "symbol", None) or str(instrument)


