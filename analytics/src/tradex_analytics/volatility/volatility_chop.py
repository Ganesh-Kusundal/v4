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

from collections.abc import Sequence
from typing import cast

import math

from tradex_analytics.indicators import (  # noqa: F401
    IndicatorSpec,
    _highest,
    _lowest,
    _rolling_sum,
    _shift,
    _sma_seeded_ema,
    _to_float,
    atr,
    sma,
    true_ranges,
)


def _closes(candles: list) -> list[float]:
    return [_to_float(c.ohlc.close.value) for c in candles]


def _highs(candles: list) -> list[float]:
    return [_to_float(c.ohlc.high.value) for c in candles]


def _lows(candles: list) -> list[float]:
    return [_to_float(c.ohlc.low.value) for c in candles]


def _stdev_gapped(values: Sequence[float | None], period: int) -> list[float | None]:
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
        acc = sum((cast(float, v) - m) ** 2 for v in window)
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


# ---------------------------------------------------------------------------
# Standard Deviation / Standard Error / Chaikin Volatility
# ---------------------------------------------------------------------------


def standard_deviation(candles: list, periods: int = 5, deviations: float = 1.0) -> dict[str, list]:
    """Population stdev of closes × multiplier (openalgo-charts parity).

    Matches ``STDDEV`` (src/indicators/volatility.ts): same band width a
    Bollinger set would draw. None before index ``periods - 1``.
    """
    closes = _closes(candles)
    return {"stdDev": [None if v is None else v * float(deviations) for v in _stdev_gapped(closes, int(periods))]}


def standard_error(candles: list, length: int = 14) -> dict[str, list]:
    """Standard error of the linear-regression fit (openalgo-charts parity).

    Matches ``STDERR`` (src/indicators/volatility.ts):
    ``sqrt((Syy - Sxy^2/Sxx)/(len-2))`` with ``x = 1..len`` spacing,
    ``xBar = (len+1)/2``, ``Sxx`` loop-invariant. Length clamped to >= 3.
    None before index ``len - 1``.
    """
    length = max(3, int(length))
    closes = _closes(candles)
    n = len(closes)
    out: list[float | None] = [None] * n
    x_bar = (length + 1) / 2
    sxx = sum((x_bar - k - 1) ** 2 for k in range(length))
    for i in range(length - 1, n):
        mean = sum(closes[i - k] for k in range(length)) / length
        syy = 0.0
        sxy = 0.0
        for k in range(length):
            dy = mean - closes[i - k]
            syy += dy * dy
            sxy += (x_bar - k - 1) * dy
        radicand = (syy - (sxy * sxy) / sxx) / (length - 2)
        out[i] = math.sqrt(max(0.0, radicand))
    return {"stdErr": out}


def chaikin_volatility(candles: list, periods: int = 10, roc_lookback: int = 10) -> dict[str, list]:
    """Chaikin Volatility — ROC of the EMA-smoothed high-low range.

    Matches ``CHAIKINVOL`` (src/indicators/volatility.ts): SMA-seeded EMA of
    ``high - low``, then ``100*(em[i]-em[i-lookback])/em[i-lookback]`` with a
    zero denominator yielding None (never ±Inf). Total warmup
    ``(periods-1) + lookback`` Nones.
    """
    periods = int(periods)
    lookback = int(roc_lookback)
    hl = [_highs(candles)[i] - _lows(candles)[i] for i in range(len(candles))]
    em = _sma_seeded_ema(hl, periods)
    n = len(hl)
    out: list[float | None] = [None] * n
    for i in range(n):
        cur, base = em[i], em[i - lookback] if i - lookback >= 0 else None
        if cur is None or base is None or base == 0:
            continue
        out[i] = 100.0 * (cur - base) / base
    return {"chaikinVolatility": out}


def _fn_stddev(candles, periods, deviations):
    return standard_deviation(candles, int(periods), float(deviations))


def _fn_stderr(candles, length):
    return standard_error(candles, int(length))


def _fn_chaikin_vol(candles, periods, rocLookback):
    return chaikin_volatility(candles, int(periods), int(rocLookback))


SPEC_STANDARD_DEVIATION = IndicatorSpec(
    id="standard-deviation",
    name="Standard Deviation",
    category="Volatility",
    placement="pane",
    params=(("periods", "int", 5), ("deviations", "float", 1.0)),
    plots=(("stdDev", "line", "StdDev"),),
    fn=_fn_stddev,
)

SPEC_STANDARD_ERROR = IndicatorSpec(
    id="standard-error",
    name="Standard Error",
    category="Volatility",
    placement="pane",
    params=(("length", "int", 14),),
    plots=(("stdErr", "line", "StdErr"),),
    fn=_fn_stderr,
)

SPEC_CHAIKIN_VOLATILITY = IndicatorSpec(
    id="chaikin-volatility",
    name="Chaikin Volatility",
    category="Volatility",
    placement="pane",
    params=(("periods", "int", 10), ("rocLookback", "int", 10)),
    plots=(("chaikinVolatility", "line", "Chaikin Volatility"),),
    levels=({"value": 0},),
    fn=_fn_chaikin_vol,
)


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

