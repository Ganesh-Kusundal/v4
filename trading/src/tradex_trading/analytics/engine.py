"""Analytics engine — coordinates analytics computations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.market import Candle, HistoricalSeries
from tradex_domain.value_objects import Price

from tradex_trading.analytics.breadth import advance_decline
from tradex_trading.analytics.indicators import ema, macd, roc, rsi, sma
from tradex_trading.analytics.probability import win_rate
from tradex_trading.analytics.reports import max_drawdown, sharpe_ratio, total_return
from tradex_trading.analytics.volatility import realized_vol


class AnalyticsEngine:
    """Coordinates analytics computations."""

    _INDICATORS: dict[str, Callable[[list, int], list]] = {
        "sma": sma, "ema": ema, "rsi": rsi, "roc": roc, "macd": macd,
    }
    _DEFAULTS = {"sma": 20, "ema": 20, "rsi": 14, "roc": 10, "macd": 26}

    def __init__(self, warmup_bars: int = 0) -> None:
        self._warmup_bars = warmup_bars

    def compute(self, series, indicators: list[str]) -> dict:
        """Compute requested indicators on a series.

        Args:
            series: List of numeric values
            indicators: List of indicator names ('sma', 'ema', 'rsi')

        Returns:
            Dictionary mapping indicator names to computed values
        """
        result = {}

        for indicator in indicators:
            if indicator == 'sma':
                result['sma'] = sma(series, period=20)
            elif indicator == 'ema':
                result['ema'] = ema(series, period=20)
            elif indicator == 'rsi':
                result['rsi'] = rsi(series, period=14)
            elif indicator == 'roc':
                result['roc'] = roc(series, period=10)
            elif indicator == 'macd':
                result['macd'] = macd(series, period=26)
            else:
                raise ValueError(f"Unknown indicator: {indicator}")

        return result

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
        """
        key = name.lower()
        func = self._INDICATORS.get(key)
        if func is None:
            raise CapabilityNotSupportedError(f"indicator {name!r} is not implemented")
        period = int(params.get("period", self._DEFAULTS[key]))
        closes = [float(c.ohlc.close.value) for c in series.candles]
        values = func(closes, period)
        if self._warmup_bars > 0:
            padded: list[float | None] = [None] * self._warmup_bars
            padded.extend(float(v) for v in values if v is not None)
            return padded
        return [float(v) if v is not None else None for v in values]

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

    def report(self, name: str, series: HistoricalSeries, **params: Any) -> dict[str, Any]:
        """Compute a named report over a HistoricalSeries."""
        closes = [float(c.ohlc.close.value) for c in series.candles]
        key = name.lower()
        if key == "max_drawdown":
            return {"max_drawdown": max_drawdown(closes)}
        if key == "sharpe":
            returns = _returns(closes)
            return {
                "sharpe": sharpe_ratio(
                    returns,
                    risk_free_rate=float(params.get("risk_free", 0.0)),
                )
            }
        if key == "realized_vol":
            return {
                "realized_vol": realized_vol(
                    closes,
                    periods_per_year=int(params.get("periods_per_year", 252)),
                )
            }
        if key == "win_rate":
            return {"win_rate": win_rate(closes)}
        if key == "advance_decline":
            adv, dec, ratio = advance_decline(closes)
            return {"advances": adv, "declines": dec, "adv_dec_ratio": ratio}
        raise CapabilityNotSupportedError(f"report {name!r} is not implemented")

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


    def tearsheet(self, returns: Sequence[float] | None = None) -> dict[str, Any]:
        """Generate a comprehensive performance tearsheet.

        Parameters
        ----------
        returns : Sequence[float] | None
            Series of returns. If None, uses internal data.

        Returns
        -------
        dict[str, Any]
            Performance metrics dictionary.
        """
        if returns is None or len(returns) == 0:
            return {
                "total_return": 0.0,
                "sharpe_ratio": 0.0,
                "max_drawdown": 0.0,
                "volatility": 0.0,
                "sortino_ratio": 0.0,
                "calmar_ratio": 0.0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "num_trades": 0,
            }

        ret_list = list(returns)

        # Build equity curve from returns for functions that expect prices
        equity_curve = [1.0]
        for r in ret_list:
            equity_curve.append(equity_curve[-1] * (1.0 + r))

        tr = total_return(equity_curve)
        sr = sharpe_ratio(ret_list)
        md = max_drawdown(equity_curve)

        # Volatility (annualized)
        import math

        mean_r = sum(ret_list) / len(ret_list) if ret_list else 0
        var_r = sum((r - mean_r) ** 2 for r in ret_list) / max(len(ret_list) - 1, 1)
        vol = math.sqrt(var_r * 252)  # annualized

        # Sortino (downside deviation)
        downside = [r for r in ret_list if r < 0]
        if downside:
            downside_var = sum(r**2 for r in downside) / len(downside)
            downside_dev = math.sqrt(downside_var * 252)
            sortino = (mean_r * 252) / downside_dev if downside_dev > 0 else 0
        else:
            sortino = 0.0

        # Calmar
        calmar = (mean_r * 252) / abs(md) if md != 0 else 0.0

        # Win rate
        wins = sum(1 for r in ret_list if r > 0)
        win_rate = wins / len(ret_list) if ret_list else 0.0

        # Profit factor
        gross_profit = sum(r for r in ret_list if r > 0)
        gross_loss = abs(sum(r for r in ret_list if r < 0))
        profit_factor = (
            gross_profit / gross_loss
            if gross_loss > 0
            else float("inf") if gross_profit > 0 else 0.0
        )

        return {
            "total_return": tr,
            "sharpe_ratio": sr,
            "max_drawdown": md,
            "volatility": vol,
            "sortino_ratio": sortino,
            "calmar_ratio": calmar,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "num_trades": len(ret_list),
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
        }

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


__all__ = ["AnalyticsEngine"]
