"""Realized (historical) volatility from close prices."""

from __future__ import annotations

import math
import statistics


def realized_vol(prices: list[float], periods_per_year: int = 252) -> float:
    """Annualized realized volatility from a list of close prices."""
    if len(prices) < 2:
        return 0.0
    log_returns = [
        math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))
    ]
    if len(log_returns) < 2:
        return 0.0
    return statistics.stdev(log_returns) * math.sqrt(periods_per_year)


__all__ = ["realized_vol"]
