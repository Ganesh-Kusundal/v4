"""Performance reports — analytics for trading performance."""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

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
    "tearsheet",
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


def periods_per_year(frequency: str) -> int:
    """Public annualisation factor for a bar frequency.

    A thin public wrapper over the canonical frequency map, so a caller that
    needs to annualise its own series (e.g. volatility, Calmar) reads the same
    periods the Sharpe/Sortino ratios use rather than re-deriving the mapping.
    """
    return _periods_per_year(frequency)


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


def drawdown_episodes(equity_curve: Sequence[float]) -> dict[str, Any]:
    """Decompose an equity curve into discrete drawdown episodes + aggregates.

    An *episode* is one peak-to-recovery decline. Ported from Vibe-Trading's
    ``drawdown_distribution_analysis`` (MIT) into a pure-Python, dependency-free
    form so a live/backtest loop can build the same risk-x-ray without pandas.

    Args:
        equity_curve: Equity/wealth values (Decimal or float), strictly positive.

    Returns:
        dict with ``max_drawdown`` (positive fraction), ``max_drawdown_duration``
        (bars), ``avg_drawdown_depth``, ``avg_drawdown_duration``, ``ulcer_index``
        (RMS drawdown), ``pain_index`` (mean drawdown), ``episode_count``, and
        ``episodes`` — each ``{peak_index, trough_index, recovery_index,
        max_depth, duration, recovered}``.

    Raises:
        ValueError: if the curve is empty or any value is not strictly positive.
    """
    values = [_to_float(e) for e in equity_curve]
    if not values:
        raise ValueError("equity_curve is empty")
    if any(v <= 0 for v in values):
        raise ValueError("equity_curve must be strictly positive")

    n = len(values)
    # Per-bar drawdown fraction (positive): 1 - value / running_peak.
    dd = [0.0] * n
    running_peak = values[0]
    for i, value in enumerate(values):
        if value > running_peak:
            running_peak = value
        dd[i] = 1.0 - value / running_peak if running_peak > 0 else 0.0

    episodes: list[dict] = []
    in_drawdown = False
    peak_idx = 0
    trough_idx = 0
    max_depth = 0.0
    for i in range(n):
        if values[i] >= values[peak_idx]:
            if in_drawdown:
                episodes.append(
                    {
                        "peak_index": peak_idx,
                        "trough_index": trough_idx,
                        "recovery_index": i,
                        "max_depth": max_depth,
                        "duration": i - peak_idx,
                        "recovered": True,
                    }
                )
                in_drawdown = False
            peak_idx = i
            trough_idx = i
            max_depth = 0.0
        else:
            in_drawdown = True
            if dd[i] > max_depth:
                max_depth = dd[i]
                trough_idx = i
    if in_drawdown:
        episodes.append(
            {
                "peak_index": peak_idx,
                "trough_index": trough_idx,
                "recovery_index": None,
                "max_depth": max_depth,
                "duration": (n - 1) - peak_idx,
                "recovered": False,
            }
        )

    depths = [e["max_depth"] for e in episodes]
    durations = [e["duration"] for e in episodes]
    return {
        "max_drawdown": max(depths) if depths else 0.0,
        "max_drawdown_duration": max(durations) if durations else 0,
        "avg_drawdown_depth": sum(depths) / len(depths) if depths else 0.0,
        "avg_drawdown_duration": sum(durations) / len(durations) if durations else 0.0,
        "ulcer_index": (sum(d * d for d in dd) / n) ** 0.5,
        "pain_index": sum(dd) / n,
        "episode_count": len(episodes),
        "episodes": episodes,
    }


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


def tearsheet(returns: Sequence[float] | None = None) -> dict[str, Any]:
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

    equity_curve = [1.0]
    for r in ret_list:
        equity_curve.append(equity_curve[-1] * (1.0 + r))

    tr = total_return(equity_curve)
    sr = sharpe_ratio(ret_list)
    md = max_drawdown(equity_curve)

    mean_r = sum(ret_list) / len(ret_list) if ret_list else 0
    var_r = sum((r - mean_r) ** 2 for r in ret_list) / max(len(ret_list) - 1, 1)
    vol = math.sqrt(var_r * TRADING_DAYS_PER_YEAR)

    downside = [r for r in ret_list if r < 0]
    if downside:
        downside_var = sum(r**2 for r in downside) / len(downside)
        downside_dev = math.sqrt(downside_var * TRADING_DAYS_PER_YEAR)
        sortino = (
            (mean_r * TRADING_DAYS_PER_YEAR) / downside_dev
            if downside_dev > 0
            else 0
        )
    else:
        sortino = 0.0

    calmar = (mean_r * TRADING_DAYS_PER_YEAR) / abs(md) if md != 0 else 0.0

    wins = sum(1 for r in ret_list if r > 0)
    win_rate = wins / len(ret_list) if ret_list else 0.0

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


