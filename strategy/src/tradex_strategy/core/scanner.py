"""Scanner engine — condition evaluation + ranking over a market provider."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
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

# Storage bound for the per-instrument streaming buffer until a definition is
# known, and the size an explicit ``max_bars`` starts from.  It is deliberately
# NOT the warmup window: the first definition run resizes the buffer to
# ``warmup_bars(conditions)``, so a period longer than 30 bars is no longer
# truncated.  30 keeps memory bounded at ~30 candles x N instruments until then.
_DEFAULT_MAX_BARS = 30

# Bars held beyond the longest declared indicator period.  A declared period
# of P needs P + 1 bars before the indicator emits its first value (every
# period-based indicator needs one bar to compute a change from), so a buffer
# of exactly P bars holds nothing but warmup.  This is the same quantity the
# walk-forward warmup carry uses (lane 1d), deliberately: "warm" means the same
# thing in both places.
_WARMUP_MARGIN_BARS = 1


def warmup_bars(conditions: Iterable[Condition]) -> int:
    """Bars an instrument must hold before every condition in *conditions* can match.

    Derived from the longest indicator period the conditions declare, plus
    ``_WARMUP_MARGIN_BARS``.  It was a flat 30 before, which silently
    truncated any longer period into a permanently empty history.

    Deliberately conservative.  It takes the largest ``int`` the indicator's
    registry spec declares (or the condition supplies), which over-reads the
    few params that are not periods (a table width) and under-reads chained
    ones (a smoothing stage is a sum, not a max).  Over-reading costs a few
    bars of buffer; under-reading is covered by the other half of this lane —
    an indicator that is still not warm returns no match rather than a number.
    """
    longest = max((_declared_period(c.name, c.params) for c in conditions), default=0)
    return longest + _WARMUP_MARGIN_BARS


def _declared_period(name: str, params: Mapping[str, Any]) -> int:
    """Longest int period the indicator *name* declares, given ``params``."""
    from tradex_analytics.indicators import get_indicator_spec

    spec = get_indicator_spec(name.lower())
    if spec is None:
        # A raw price field (``close``), not an indicator: it declares no
        # period at all, and the margin alone is its whole warmup.
        return 0
    declared = [
        default
        for _param, kind, default in spec.params
        if kind == "int" and isinstance(default, int) and not isinstance(default, bool)
    ]
    supplied = [v for v in params.values() if isinstance(v, int) and not isinstance(v, bool)]
    return max(declared + supplied, default=0)


class ScannerEngine:
    """Evaluates ``Condition`` values over a default D1 window per instrument.

    Supports two data paths:

    * **Snapshot** (legacy): ``_history()`` fetches from the market provider
      on every scan.  Still used as fallback when no bars have been streamed.
    * **Streaming** (G7): call ``consume(candle)`` to append bars into a
      per-instrument rolling buffer.  Once the buffer has data, ``_history()``
      returns it directly — no market round-trip.

    The streaming buffer is sized from the definition it is asked to evaluate
    (:func:`warmup_bars`), not from a constant: a buffer shorter than an
    indicator's warmup is what turns "unknown" into a value that passes a
    threshold, and no caller-supplied ``max_bars`` can be small enough to
    reintroduce that.
    """

    def __init__(
        self,
        market: Any,
        analytics: IndicatorComputer | None = None,
        window_days: int = 30,
        max_bars: int | None = _DEFAULT_MAX_BARS,
    ) -> None:
        self._market = market
        if analytics is None:
            from tradex_analytics.engine import AnalyticsEngine
            analytics = AnalyticsEngine()  # type: ignore[assignment]
        self._analytics: IndicatorComputer = analytics  # type: ignore[assignment]
        self._window_days = window_days
        # Buffer size until the first definition run sizes it from the warmup.
        self._buffer_cap = _DEFAULT_MAX_BARS if max_bars is None else max_bars
        self._sized = False
        # Per-instrument rolling buffer keyed by instrument symbol.
        self._buffers: dict[str, deque[Candle]] = defaultdict(self._new_buffer)

    def _new_buffer(self) -> deque[Candle]:
        return deque(maxlen=self._buffer_cap)

    def _size_buffers(self, bars: int) -> None:
        """Size every buffer to a *bars* bar warmup, from the first run on.

        The first definition run replaces the pre-scan storage bound: from
        then on the definition decides how many bars matter, so the derived
        number is the buffer size rather than a floor under a constant.  Later
        runs only grow it — a bar the deque has already dropped is not
        recoverable, and a shorter requirement must not shrink a buffer another
        definition has already sized.
        """
        if self._sized and bars <= self._buffer_cap:
            return
        for key in list(self._buffers):
            self._buffers[key] = deque(self._buffers[key], maxlen=bars)
        self._buffer_cap = bars
        self._sized = True

    # -- streaming entry point ---------------------------------------------------

    def consume(self, candle: Candle) -> None:
        """Append *candle* to the per-instrument rolling buffer.

        This is the public entry point for the bar stream (bus subscription,
        aggregator callback, etc.).  The buffer is bounded by ``max_bars``
        until the first definition run resizes it to that definition's warmup
        (and grows for a later, longer one); oldest bars are dropped
        automatically by the underlying ``deque``.
        """
        key = _instrument_key(candle.instrument)
        self._buffers[key].append(candle)

    def run(self, definition: ScannerDefinition) -> list[ScannerResult]:
        """Evaluate all conditions and return the ranked matches.

        An instrument that matched nothing is not returned: a scan whose
        result set is the whole universe is indistinguishable from a scan that
        found candidates everywhere, which is how an unwarmed indicator used
        to report success.  A definition with no conditions has nothing to
        match, so every instrument is returned.
        """
        conditions = definition.conditions
        self._size_buffers(warmup_bars(conditions))
        results: list[ScannerResult] = []
        for instrument in definition.universe:
            series = self._history(instrument)
            result = self._evaluate(instrument, series, conditions)
            if conditions and not result.matched_conditions:
                continue
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
            # An unwarmed indicator is unknown, not 0.0.  It is left out of
            # the values rather than reported as a number, because 0.0 is a
            # value that passes a "below" threshold: an unwarmed ``rsi`` read
            # as 0.0 matched every instrument in the universe.
            indicator_values={k: float(v) for k, v in values.items() if v is not None},
            rank=0,  # assigned by run()
        )

    def _value(self, series: HistoricalSeries, name: str, params: dict[str, Any]) -> float | None:
        """Tail value of *name*, or ``None`` when it has not warmed up.

        ``None`` means unknown, and unknown must never be handed to a
        threshold comparison — ``_matches`` treats it as no match.
        """
        if not series.candles:
            return None
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
            return None if tail is None else float(tail)
        indicator = cast(HistoricalSeries, self._analytics.indicator(series, name, **params))
        if not indicator.candles:
            return None
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


