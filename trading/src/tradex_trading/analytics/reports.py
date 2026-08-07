"""Performance reports — analytics for trading performance."""

from __future__ import annotations

import math
from decimal import Decimal

NumericValue = float | Decimal


def _to_float(value: NumericValue) -> float:
    """Convert Decimal or float to float."""
    if isinstance(value, Decimal):
        return float(value)
    return value


def sharpe_ratio(returns: list, risk_free_rate: float = 0.0) -> float:
    """Calculate Sharpe Ratio.

    Args:
        returns: List of returns (Decimal or float)
        risk_free_rate: Risk-free rate (annualized, default: 0.0)

    Returns:
        Sharpe ratio (annualized)
    """
    if not returns:
        return 0.0

    floats = [_to_float(r) for r in returns]
    n = len(floats)

    if n < 2:
        return 0.0

    # Mean return
    mean_return = sum(floats) / n

    # Standard deviation
    variance = sum((r - mean_return) ** 2 for r in floats) / (n - 1)
    std_dev = math.sqrt(variance) if variance > 0 else 0.0

    if std_dev == 0:
        return 0.0

    # Annualize (assuming 252 trading days)
    annualized_return = mean_return * 252
    annualized_std = std_dev * math.sqrt(252)

    return (annualized_return - risk_free_rate) / annualized_std


def max_drawdown(equity_curve: list) -> float:
    """Calculate maximum drawdown from equity curve.

    Args:
        equity_curve: List of equity values (Decimal or float)

    Returns:
        Maximum drawdown as a negative percentage
    """
    if not equity_curve:
        return 0.0

    floats = [_to_float(e) for e in equity_curve]

    if len(floats) < 2:
        return 0.0

    peak = floats[0]
    max_dd = 0.0

    for value in floats:
        if value > peak:
            peak = value
        drawdown = (value - peak) / peak if peak > 0 else 0.0
        if drawdown < max_dd:
            max_dd = drawdown

    return max_dd


def total_return(equity_curve: list) -> float:
    """Calculate total return from equity curve.

    Args:
        equity_curve: List of equity values (Decimal or float)

    Returns:
        Total return as a percentage
    """
    if not equity_curve:
        return 0.0

    floats = [_to_float(e) for e in equity_curve]

    if len(floats) < 2:
        return 0.0

    initial = floats[0]
    final = floats[-1]

    if initial == 0:
        return 0.0

    return (final - initial) / initial


__all__ = ["sharpe_ratio", "max_drawdown", "total_return"]
