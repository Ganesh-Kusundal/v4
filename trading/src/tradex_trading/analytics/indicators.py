"""Technical indicators — stdlib implementation with optional numpy acceleration."""

from __future__ import annotations

from decimal import Decimal

NumericValue = float | Decimal


def _to_float(value: NumericValue) -> float:
    """Convert Decimal or float to float."""
    if isinstance(value, Decimal):
        return float(value)
    return value


def sma(values: list, period: int) -> list:
    """Simple Moving Average.

    Args:
        values: List of numeric values (Decimal or float)
        period: Window size

    Returns:
        List of SMA values (same length as input, None-padded at start)
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return [None] * len(values)

    floats = [_to_float(v) for v in values]
    result: list[float | None] = [None] * (period - 1)

    window_sum = sum(floats[:period])
    result.append(window_sum / period)

    for i in range(period, len(floats)):
        window_sum += floats[i] - floats[i - period]
        result.append(window_sum / period)

    return result


def ema(values: list, period: int) -> list:
    """Exponential Moving Average.

    Args:
        values: List of numeric values (Decimal or float)
        period: Window size

    Returns:
        List of EMA values (same length as input, None-padded at start)
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return [None] * len(values)

    floats = [_to_float(v) for v in values]
    result: list[float | None] = [None] * (period - 1)

    # Initial SMA for first EMA value
    initial_sma = sum(floats[:period]) / period
    result.append(initial_sma)

    multiplier = 2.0 / (period + 1)
    prev_ema = initial_sma

    for i in range(period, len(floats)):
        current_ema = (floats[i] - prev_ema) * multiplier + prev_ema
        result.append(current_ema)
        prev_ema = current_ema

    return result


def rsi(values: list, period: int = 14) -> list:
    """Relative Strength Index.

    Args:
        values: List of numeric values (Decimal or float)
        period: Lookback period (default: 14)

    Returns:
        List of RSI values (0-100, None-padded at start)
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period + 1:
        return [None] * len(values)

    floats = [_to_float(v) for v in values]
    result: list[float | None] = [None] * period

    # Calculate gains and losses
    gains = []
    losses = []
    for i in range(1, len(floats)):
        change = floats[i] - floats[i - 1]
        gains.append(max(0, change))
        losses.append(max(0, -change))

    # Initial average gain/loss
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        result.append(100.0)
    else:
        rs = avg_gain / avg_loss
        result.append(100.0 - (100.0 / (1.0 + rs)))

    # Smoothed averages
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100.0 - (100.0 / (1.0 + rs)))

    return result


__all__ = ["sma", "ema", "rsi"]
