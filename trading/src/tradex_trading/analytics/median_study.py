"""Median study — nearest-rank median of hl2 banded by ATR, with a chained EMA.

Batch 1 parallel split: this module lives beside ``indicators.py`` so three
Batch-1 ports could be built concurrently without colliding on one file.
It imports the shared primitives (``atr``, SMA-seeded EMA helpers) from
``indicators.py`` rather than redefining them, and exposes ``SPEC_MEDIAN``
ready for registry registration by the merge task.
"""

from __future__ import annotations

import math
from typing import Any

from tradex_trading.analytics.indicators import (
    IndicatorSpec,
    _ema_of_gapped,
    _to_float,
    atr,
)

__all__ = ["median_study", "SPEC_MEDIAN"]


def _percentile_nearest_rank(
    values: list[float | None], period: int, percentage: float = 50.0
) -> list[float | None]:
    """Rolling nearest-rank percentile (openalgo-charts ``percentileNearestRank``).

    Matches src/indicators/calc.ts exactly: rank = max(1, ceil(pct/100 * period)),
    so on an even-length window the 50th percentile is sorted[rank - 1] — the
    lower of the two middles per the source's arithmetic (never an interpolation).
    None before index ``period - 1``; a None anywhere in a window nulls that slot.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    rank = max(1, math.ceil((percentage / 100.0) * period))
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sorted(window)[rank - 1]
    return out


def median_study(
    candles: list,
    length: int = 3,
    atr_length: int = 14,
    atr_mult: float = 2.0,
) -> dict[str, list]:
    """Median study (openalgo-charts ``MEDIAN``, src/indicators/averages.ts).

    median = nearest-rank percentile 50 of hl2 over ``length``;
    upper/lower = median +/- atr_mult * ATR(atr_length);
    median_ema = EMA chained over the median's warmup gap, so it first
    prints at ``2 * length - 2``.

    Returns dict keyed 'median' / 'upper' / 'lower' / 'median_ema'.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    if atr_length <= 0:
        raise ValueError("atr_length must be positive")
    values: list[float | None] = [
        (_to_float(c.ohlc.high.value) + _to_float(c.ohlc.low.value)) / 2.0
        for c in candles
    ]
    length = int(length)
    atr_length = int(atr_length)
    median = _percentile_nearest_rank(values, length, 50.0)
    ranges = atr(candles, atr_length)
    upper: list[float | None] = [None] * len(median)
    lower: list[float | None] = [None] * len(median)
    for i, m in enumerate(median):
        r = ranges[i]
        if m is None or r is None:
            continue
        upper[i] = m + atr_mult * r
        lower[i] = m - atr_mult * r
    return {
        "median": median,
        "upper": upper,
        "lower": lower,
        "median_ema": _ema_of_gapped(median, length),
    }


SPEC_MEDIAN = IndicatorSpec(
    id="median",
    name="Median",
    category="Trend",
    placement="overlay",
    params=(
        ("length", "int", 3),
        ("atr_length", "int", 14),
        ("atr_mult", "float", 2.0),
    ),
    plots=(
        ("median", "line", "Median"),
        ("upper", "line", "Upper Band"),
        ("lower", "line", "Lower Band"),
        ("median_ema", "line", "Median EMA"),
    ),
    fn=median_study,
)
