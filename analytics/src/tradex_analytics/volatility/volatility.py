"""Realized (historical) volatility from close prices."""

from __future__ import annotations

import math
import statistics

from tradex_analytics.reports import TRADING_DAYS_PER_YEAR


def realized_vol(
    prices: list[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Annualized realized volatility from a list of close prices.

    ``periods_per_year`` defaults to the platform's trading-day count, so a
    daily price series annualises on the same basis as every other metric.
    """
    if len(prices) < 2:
        return 0.0
    log_returns = [
        math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))
    ]
    if len(log_returns) < 2:
        return 0.0
    return statistics.stdev(log_returns) * math.sqrt(periods_per_year)


