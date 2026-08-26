"""Choppiness family + Average Daily Range + Historical Volatility (openalgo-charts parity).

Batch 3 parallel split: this module stands alone so it can be developed
beside ``indicators.py`` without merge conflicts. A later merge task wires
the SPEC_* objects below into the registry in ``indicators.py``; until then
they are exported but NOT registered.

TS sources
- ``src/indicators/volatility.ts`` — CHOPPINESS_INDEX, HISTORICAL_VOLATILITY,
  AVERAGE_DAILY_RANGE, CHOP_ZONE
- ``src/indicators/calc.ts`` — helpers sma, stdev, highest, lowest, rollingSum, nulls

Helpers are imported from ``indicators.py`` — never redefined here.
"""

from __future__ import annotations

import math
from typing import Any

from tradex_trading.analytics.indicators import IndicatorSpec, atr, sma, true_ranges  # noqa: F401
from tradex_trading.analytics.indicators import _sma_seeded_ema


def _to_float(value: Any) -> float:
    return float(value)


def _closes(candles: list) -> list[float]:
    return [_to_float(c.ohlc.close.value) for c in candles]


def _highs(candles: list) -> list[float]:
    return [_to_float(c.ohlc.high.value) for c in candles]


def _lows(candles: list) -> list[float]:
    return [_to_float(c.ohlc.low.value) for c in candles]


def _highest(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        out[i] = max(values[i - period + 1 : i + 1])
    return out


def _lowest(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        out[i] = min(values[i - period + 1 : i + 1])
    return out


def _rolling_sum(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s
    for i in range(period, n):
        s += values[i] - values[i - period]
        out[i] = s
    return out


def _shift(values: list[float | None], k: int) -> list[float | None]:
    """Displace by k bars: positive draws later (TS ``shift`` parity)."""
    n = len(values)
    out: list[float | None] = [None] * n
    if k == 0:
        return values[:]
    for i in range(n):
        j = i + k
        if 0 <= j < n:
            out[j] = values[i]
    return out


def _stdev_gapped(values: list[float | None], period: int) -> list[float | None]:
    """Population stdev, None if any window entry is None (TS NaN carry-through)."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        # all finite
        m = sum(window) / period  # type: ignore[arg-type]
        acc = sum((v - m) ** 2 for v in window)  # type: ignore[operator]
        out[i] = math.sqrt(acc / period)
    return out


# ---------------------------------------------------------------------------
# Choppiness Index
# ---------------------------------------------------------------------------

def choppiness_index(candles: list, length: int = 14, offset: int = 0) -> list[float | None]:
    """Choppiness Index (openalgo-charts ``CHOPPINESS_INDEX`` parity).

    CHOP = 100 * log10(sumTR / range) / log10(len)
    where sumTR = rolling sum of true range over ``length`` and
    range = highest(high,len) - lowest(low,len). First prints at index
    ``length - 1``; length==1 yields all None (log10(1)==0). Offset
    displaces the plotted series (positive draws later).
    """
    if length <= 0:
        raise ValueError("length must be positive")
    n = len(candles)
    if n == 0:
        return []
    highs = _highs(candles)
    lows = _lows(candles)
    trs = true_ranges(candles)
    travel = _rolling_sum(trs, int(length))
    hi = _highest(highs, int(length))
    lo = _lowest(lows, int(length))
    scale = math.log10(length) if length > 0 else float("nan")
    out: list[float | None] = [None] * n
    if scale != 0 and math.isfinite(scale):
        for i in range(n):
            h = hi[i]
            lo_v = lo[i]
            tv = travel[i]
            if h is None or lo_v is None or tv is None:
                continue
            span = h - lo_v
            if not (span > 0):
                continue
            ratio = tv / span
            if not (ratio > 0):
                continue
            out[i] = (100.0 * math.log10(ratio)) / scale
    k = int(round(offset))
    return _shift(out, k)


# ---------------------------------------------------------------------------
# Historical Volatility
# ---------------------------------------------------------------------------

def historical_volatility(candles: list, length: int = 10, per: int = 1) -> list[float | None]:
    """Historical Volatility — annualised stdev of log returns (parity).

    Matches openalgo-charts ``HISTORICAL_VOLATILITY`` (volatility.ts):
    returns[0] is None (no prior close), returns[i]=log(close[i]/close[i-1])
    when both >0 else None; dev=population stdev over ``length``; factor=
    sqrt(365/per); hv=100*dev*factor. First at index ``length`` (because
    returns[0] is None, the window needs length real returns starting at 1).
    """
    if length <= 0:
        raise ValueError("length must be positive")
    if per <= 0:
        raise ValueError("per must be positive")
    n = len(candles)
    if n == 0:
        return []
    closes = _closes(candles)
    returns: list[float | None] = [None] * n
    for i in range(1, n):
        prev = closes[i - 1]
        cur = closes[i]
        if prev > 0 and cur > 0:
            returns[i] = math.log(cur / prev)
        else:
            returns[i] = None
    dev = _stdev_gapped(returns, int(length))
    annual = 365
    factor = math.sqrt(annual / float(per)) if per > 0 else float("nan")
    out: list[float | None] = [None] * n
    for i in range(n):
        d = dev[i]
        if d is None or not math.isfinite(d) or not math.isfinite(factor):
            continue
        out[i] = 100.0 * d * factor
    return out


# ---------------------------------------------------------------------------
# Average Daily Range
# ---------------------------------------------------------------------------

def average_daily_range(candles: list, length: int = 14) -> list[float | None]:
    """Average Daily Range: SMA of (high - low) over ``length``.

    Matches openalgo-charts ``AVERAGE_DAILY_RANGE``; first at index
    ``length - 1``.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    n = len(candles)
    if n == 0:
        return []
    ranges = [_to_float(c.ohlc.high.value) - _to_float(c.ohlc.low.value) for c in candles]
    return sma(ranges, int(length))


# ---------------------------------------------------------------------------
# Chop Zone
# ---------------------------------------------------------------------------

_CHOP_ZONE_PERIODS = 30
_CHOP_ZONE_EMA = 34
_CHOP_ZONE_PI = math.atan(1) * 4


def chop_zone(candles: list) -> dict[str, list]:
    """Chop Zone (openalgo-charts ``CHOP_ZONE`` parity).

    Constant 1 plotted on every bar; colour encodes EMA-34 slope angle
    normalised against the 30-bar high-low range (see volatility.ts).
    Returns dict keyed 'chopZone' and 'angle' (angle is None during warmup
    and colour maps to yellow there, matching TS ``colorBy`` fallback).
    """
    n = len(candles)
    if n == 0:
        return {"chopZone": [], "angle": []}
    highs = _highs(candles)
    lows = _lows(candles)
    closes = _closes(candles)
    hi = _highest(highs, _CHOP_ZONE_PERIODS)
    lo = _lowest(lows, _CHOP_ZONE_PERIODS)
    ema34 = _sma_seeded_ema(closes, _CHOP_ZONE_EMA)
    angle: list[float | None] = [None] * n
    for i in range(1, n):
        h = hi[i]
        l = lo[i]
        e0 = ema34[i - 1]
        e1 = ema34[i]
        if h is None or l is None or e0 is None or e1 is None:
            continue
        rng = h - l
        avg = (highs[i] + lows[i] + closes[i]) / 3.0
        if not (rng > 0) or avg == 0:
            continue
        span = (25.0 / rng) * l
        dy = ((e0 - e1) / avg) * span
        if not math.isfinite(dy):
            continue
        hyp = math.sqrt(1 + dy * dy)
        # 1/hyp in [-1,1] already; acos safe
        cosv = 1.0 / hyp
        # clamp numeric
        if cosv > 1.0:
            cosv = 1.0
        elif cosv < -1.0:
            cosv = -1.0
        degrees = round((180.0 * math.acos(cosv)) / _CHOP_ZONE_PI)
        angle[i] = -degrees if dy > 0 else degrees
    chop_zone_vals: list[float | None] = [1.0] * n
    return {"chopZone": chop_zone_vals, "angle": angle}


# ---------------------------------------------------------------------------
# Specs — ready for registry import; NOT registered here (merge task wires them).
# ---------------------------------------------------------------------------

def _fn_chop(candles, length, offset):
    return choppiness_index(candles, int(length), int(offset))


def _fn_hv(candles, length, per):
    return historical_volatility(candles, int(length), int(per))


def _fn_adr(candles, length):
    return average_daily_range(candles, int(length))


def _fn_chop_zone(candles):
    return chop_zone(candles)


SPEC_CHOPPINESS_INDEX = IndicatorSpec(
    id="choppiness-index",
    name="Choppiness Index",
    category="Volatility",
    placement="pane",
    params=(("length", "int", 14), ("offset", "int", 0)),
    plots=(("value", "line", "CHOP"),),
    levels=({"value": 61.8}, {"value": 50}, {"value": 38.2}),
    fn=_fn_chop,
)

SPEC_HISTORICAL_VOLATILITY = IndicatorSpec(
    id="historical-volatility",
    name="Historical Volatility",
    category="Volatility",
    placement="pane",
    params=(("length", "int", 10), ("per", "int", 1)),
    plots=(("hv", "line", "HV"),),
    fn=_fn_hv,
)

SPEC_AVERAGE_DAILY_RANGE = IndicatorSpec(
    id="average-daily-range",
    name="Average Daily Range",
    category="Volatility",
    placement="pane",
    params=(("length", "int", 14),),
    plots=(("adr", "line", "ADR"),),
    fn=_fn_adr,
)

SPEC_CHOP_ZONE = IndicatorSpec(
    id="chop-zone",
    name="Chop Zone",
    category="Volatility",
    placement="pane",
    params=(),
    plots=(
        ("chopZone", "column", "Chop Zone"),
        ("angle", "line", "Angle"),
    ),
    fn=_fn_chop_zone,
)

__all__ = [
    "average_daily_range",
    "choppiness_index",
    "chop_zone",
    "historical_volatility",
    "SPEC_AVERAGE_DAILY_RANGE",
    "SPEC_CHOPPINESS_INDEX",
    "SPEC_CHOP_ZONE",
    "SPEC_HISTORICAL_VOLATILITY",
]
