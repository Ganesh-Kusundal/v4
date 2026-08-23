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


def _periods_per_year(frequency: str) -> int:
    """Periods per year for annualisation — ponytail: NSE 375 min/day."""
    f = str(frequency).lower().strip()
    mapping: dict[str, int] = {
        "daily": 252,
        "1d": 252,
        "d1": 252,
        "1w": 52,
        "w1": 52,
        "weekly": 52,
        "hour": int(252 * 6.25),  # 375/60
        "1h": int(252 * 6.25),
        "h1": int(252 * 6.25),
        "60m": int(252 * 6.25),
        "30m": int(252 * 12.5),
        "m30": int(252 * 12.5),
        "15m": int(252 * 25),
        "m15": int(252 * 25),
        "5m": int(252 * 75),
        "m5": int(252 * 75),
        "1m": int(252 * 375),
        "m1": int(252 * 375),
    }
    return mapping.get(f, 252)


def sharpe_ratio(
    returns: list,
    risk_free_rate: float = 0.0,
    frequency: str = "daily",
) -> float:
    """Calculate Sharpe Ratio.

    Args:
        returns: List of returns (Decimal or float)
        risk_free_rate: Risk-free rate (annualized, default: 0.0)
        frequency: Bar frequency for annualisation (e.g. "daily", "1m", "hour").
            Ponytail: accepts Timeframe-inferred strings; defaults to daily.

    Returns:
        Sharpe ratio (annualized)
    """
    # ponytail: allow frequency as second positional arg (sharpe_ratio(rets, "1m"))
    if isinstance(risk_free_rate, str):
        frequency = risk_free_rate
        risk_free_rate = 0.0
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

    periods = _periods_per_year(frequency)
    annualized_return = mean_return * periods
    annualized_std = std_dev * math.sqrt(periods)

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
