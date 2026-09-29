"""Analytics engine — coordinates analytics computations."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.market import Candle, HistoricalSeries
from tradex_domain.value_objects import Price

from tradex_analytics.registry import IndicatorRegistry
from tradex_analytics.reports import tearsheet as reports_tearsheet


class AnalyticsEngine:
    """Coordinates analytics computations."""

    def __init__(
        self,
        warmup_bars: int = 0,
        registry: IndicatorRegistry | None = None,
    ) -> None:
        self._warmup_bars = warmup_bars
        self._registry = registry

    def indicator(
        self,
        series: HistoricalSeries,
        name: str,
        **params: Any,
    ) -> HistoricalSeries:
        """Compute a single indicator over a HistoricalSeries.

        The indicator output replaces the close of the trailing
        ``len(values)`` candles of the input series. Closes are ``Price``
        (non-negative by domain invariant), so this surface fits
        price-ranged indicators; signed outputs (roc, macd) must go through
        :meth:`indicator_values`, which returns raw floats without wrapping.
        """
        return self._wrap_in_series(
            series, name, self.indicator_values(series, name, **params),
        )

    def indicator_values(
        self,
        series: HistoricalSeries,
        name: str,
        **params: Any,
    ) -> list[float | None]:
        """Raw indicator values aligned to the series tail (None = warmup).

        Single computation path for every consumer; :meth:`indicator`
        delegates here before re-wrapping into candles. Signed values pass
        through untouched — no Price validation on this surface.

        Resolution order:
          1. The ``IndicatorRegistry`` — the single source of truth. Every
             indicator registered there is automatically available to scanners
             and callers.
          2. ``CapabilityNotSupportedError`` for truly unknown names.
        """
        key = name.lower()
        from tradex_analytics.indicators import (
            compute_indicator,
            get_indicator_spec,
        )

        if get_indicator_spec(key, self._registry) is None:
            raise CapabilityNotSupportedError(
                f"indicator {name!r} is not implemented"
            )
        result = compute_indicator(
            key, series.candles, params or {}, self._registry
        )
        # compute_indicator always returns a dict of per-plot lists.
        if "value" in result:
            raw_values = result["value"]
        else:
            raw_values = result[next(iter(result))]

        if self._warmup_bars > 0:
            padded: list[float | None] = [None] * self._warmup_bars
            padded.extend(float(v) for v in raw_values if v is not None)
            return padded
        return [float(v) if v is not None else None for v in raw_values]

    def _wrap_in_series(
        self,
        series: HistoricalSeries,
        name: str,
        values: list[float | None],
    ) -> HistoricalSeries:
        """Wrap raw values as trailing-close replacements (Price-wrapped).

        Routing mirrors the pre-refactor contract exactly: explicit warmup
        mode keeps every input candle (Nones hold originals); default mode
        strips Nones and replaces ONLY the trailing computed candles.
        """
        if self._warmup_bars > 0:
            return self._with_close_nullable(series, values)
        # Strip None padding — only non-None values replace trailing closes
        non_none = [v for v in values if v is not None]
        return self._with_close(series, non_none)

    def indicators(
        self,
        series: HistoricalSeries,
        names: list[str],
        **params: Any,
    ) -> HistoricalSeries:
        """Compute multiple indicators sequentially over a HistoricalSeries."""
        result = series
        for name in names:
            result = self.indicator(result, name, **params)
        return result

    @staticmethod
    def _with_close_nullable(
        series: HistoricalSeries, values: list[float | None]
    ) -> HistoricalSeries:
        """Return a new series with candle closes replaced by *values*, preserving None."""
        if not values:
            return HistoricalSeries(
                instrument=series.instrument,
                timeframe=series.timeframe,
                candles=[],
                start=series.start,
                end=series.end,
            )
        # Trim values to candle count when warmup padding exceeds input length
        if len(values) > len(series.candles):
            values = values[-len(series.candles):]
        trailing = series.candles[-len(values):]
        candles: list[Candle] = []
        for candle, value in zip(trailing, values, strict=True):
            ohlc = candle.ohlc
            if value is None:
                candles.append(candle)
            else:
                candles.append(
                    Candle(
                        instrument=candle.instrument,
                        timeframe=candle.timeframe,
                        ohlc=type(ohlc)(
                            open=ohlc.open,
                            high=ohlc.high,
                            low=ohlc.low,
                            close=Price(value=Decimal(str(value))),
                        ),
                        volume=candle.volume,
                        timestamp=candle.timestamp,
                    )
                )
        return HistoricalSeries(
            instrument=series.instrument,
            timeframe=series.timeframe,
            candles=candles,
            start=trailing[0].timestamp,
            end=trailing[-1].timestamp,
        )

    @staticmethod
    def _with_close(series: HistoricalSeries, values: list[float]) -> HistoricalSeries:
        """Return a new series with trailing candle closes replaced by *values*."""
        if not values:
            return HistoricalSeries(
                instrument=series.instrument,
                timeframe=series.timeframe,
                candles=[],
                start=series.start,
                end=series.end,
            )
        trailing = series.candles[-len(values):]
        candles: list[Candle] = []
        for candle, value in zip(trailing, values, strict=True):
            ohlc = candle.ohlc
            candles.append(
                Candle(
                    instrument=candle.instrument,
                    timeframe=candle.timeframe,
                    ohlc=type(ohlc)(
                        open=ohlc.open,
                        high=ohlc.high,
                        low=ohlc.low,
                        close=Price(value=Decimal(str(value))),
                    ),
                    volume=candle.volume,
                    timestamp=candle.timestamp,
                )
            )
        return HistoricalSeries(
            instrument=series.instrument,
            timeframe=series.timeframe,
            candles=candles,
            start=trailing[0].timestamp,
            end=trailing[-1].timestamp,
        )


    tearsheet = staticmethod(reports_tearsheet)

    def summary(self, returns: Sequence[float] | None = None) -> str:
        """Generate a human-readable performance summary.

        Parameters
        ----------
        returns : Sequence[float] | None
            Series of returns.

        Returns
        -------
        str
            Formatted summary string.
        """
        sheet = self.tearsheet(returns)
        lines = [
            "=" * 50,
            "PERFORMANCE SUMMARY",
            "=" * 50,
            f"Total Return:     {sheet['total_return']:>10.2%}",
            f"Sharpe Ratio:     {sheet['sharpe_ratio']:>10.2f}",
            f"Max Drawdown:     {sheet['max_drawdown']:>10.2%}",
            f"Volatility:       {sheet['volatility']:>10.2%}",
            f"Sortino Ratio:    {sheet['sortino_ratio']:>10.2f}",
            f"Calmar Ratio:     {sheet['calmar_ratio']:>10.2f}",
            f"Win Rate:         {sheet['win_rate']:>10.2%}",
            f"Profit Factor:    {sheet['profit_factor']:>10.2f}",
            f"Num Trades:       {sheet['num_trades']:>10d}",
            "=" * 50,
        ]
        return "\n".join(lines)


def _returns(prices: list[float]) -> list[float]:
    """Compute simple period-over-period returns from a price series."""
    if len(prices) < 2:
        return []
    return [
        (prices[i] - prices[i - 1]) / prices[i - 1]
        for i in range(1, len(prices))
        if prices[i - 1] != 0
    ]


