"""Performance reports — analytics for trading performance."""

from __future__ import annotations

import math
from decimal import Decimal

NumericValue = float | Decimal

# Annualisation basis. Every Sharpe, volatility, Sortino, Calmar and
# annualised-return figure the platform produces is scaled by these, so they
# live here once and are imported elsewhere rather than restated.
#   TRADING_DAYS_PER_YEAR: ~252 NYSE/NSE trading sessions per calendar year.
#   SESSION_MINUTES_PER_DAY: 375 NSE minutes per session (6h15m, 09:15-15:30).
# Changing either changes every annualised metric, so treat as fixed.
TRADING_DAYS_PER_YEAR: int = 252
SESSION_MINUTES_PER_DAY: int = 375

__all__ = [
    "SESSION_MINUTES_PER_DAY",
    "TRADING_DAYS_PER_YEAR",
    "max_drawdown",
    "sharpe_ratio",
    "sortino_ratio",
    "total_return",
]


def _to_float(value: NumericValue) -> float:
    """Convert Decimal or float to float."""
    if isinstance(value, Decimal):
        return float(value)
    return value


def _periods_per_year(frequency: str) -> int:
    """Periods per year for annualisation — ponytail: NSE 375 min/day.

    Derived from :data:`TRADING_DAYS_PER_YEAR` and
    :data:`SESSION_MINUTES_PER_DAY`; sub-daily bars multiply the day count by
    the bars per session (375 / bar-minutes). Unknown frequencies fall back to
    the daily count.
    """
    days = TRADING_DAYS_PER_YEAR
    minutes = SESSION_MINUTES_PER_DAY
    f = str(frequency).lower().strip()
    mapping: dict[str, int] = {
        "daily": days,
        "1d": days,
        "d1": days,
        "1w": 52,
        "w1": 52,
        "weekly": 52,
        "hour": int(days * (minutes / 60)),  # 375/60
        "1h": int(days * (minutes / 60)),
        "h1": int(days * (minutes / 60)),
        "60m": int(days * (minutes / 60)),
        "30m": int(days * (minutes / 30)),
        "m30": int(days * (minutes / 30)),
        "15m": int(days * (minutes / 15)),
        "m15": int(days * (minutes / 15)),
        "5m": int(days * (minutes / 5)),
        "m5": int(days * (minutes / 5)),
        "1m": int(days * minutes),
        "m1": int(days * minutes),
    }
    return mapping.get(f, TRADING_DAYS_PER_YEAR)


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


def sortino_ratio(
    returns: list,
    risk_free_rate: float = 0.0,
    frequency: str = "daily",
) -> float:
    """Calculate the Sortino Ratio (annualized).

    Same numerator as :func:`sharpe_ratio`, but the denominator is downside
    deviation about a zero minimum-acceptable-return, so upside volatility is
    not penalised. Returns ``0.0`` when there is no downside at all (a
    perfectly monotonic winner has no downside risk to divide by).

    Args:
        returns: List of returns (Decimal or float)
        risk_free_rate: Risk-free rate (annualized, default: 0.0)
        frequency: Bar frequency for annualisation (e.g. "daily", "1m").
    """
    # ponytail: mirror sharpe_ratio's positional-frequency shorthand.
    if isinstance(risk_free_rate, str):
        frequency = risk_free_rate
        risk_free_rate = 0.0
    if not returns:
        return 0.0

    floats = [_to_float(r) for r in returns]
    n = len(floats)
    if n < 2:
        return 0.0

    periods = _periods_per_year(frequency)
    annualized_return = (sum(floats) / n) * periods

    downside = [r for r in floats if r < 0]
    if not downside:
        return 0.0

    # Downside deviation divides by n (total periods), not len(downside) —
    # the periods without a loss still count as periods with no downside.
    downside_dev = math.sqrt(sum(r * r for r in downside) / n) * math.sqrt(periods)
    if downside_dev == 0:
        return 0.0

    return (annualized_return - risk_free_rate) / downside_dev


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


