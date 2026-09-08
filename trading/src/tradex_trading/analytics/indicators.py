"""Technical indicators — stdlib implementation with optional numpy acceleration."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

NumericValue = float | Decimal


def _to_float(value: NumericValue) -> float:
    """Convert Decimal or float to float."""
    if isinstance(value, Decimal):
        return float(value)
    return value


def _isfinite(v: float | None) -> bool:
    """True when *v* is a finite float (not None, not NaN/inf)."""
    return v is not None and math.isfinite(v)


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

    # Separate add/subtract matches JS running-sum micro-drift:
    # ``sum += v[i]; sum -= v[i-period]`` (two IEEE-754 round-trips per
    # step) differs from the combined ``sum += v[i] - v[i-period]`` (one
    # round-trip) in the low bits.  The TS golden was generated with the
    # JS two-step form, so we reproduce it here for bit-exact parity.
    window_sum = 0.0
    for _i in range(period):
        window_sum += floats[_i]
    result.append(window_sum / period)

    for i in range(period, len(floats)):
        window_sum += floats[i]
        window_sum -= floats[i - period]
        result.append(window_sum / period)

    return result


def ema(values: list, period: int) -> list:
    """Exponential Moving Average, seeded from values[0] (openalgo-charts parity).

    Emits from index 0 — no warmup padding. k = 2/(period+1).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    floats = [_to_float(v) for v in values]
    if not floats:
        return []
    k = 2.0 / (period + 1)
    prev = floats[0]
    out = [prev]
    for i in range(1, len(floats)):
        prev = floats[i] * k + prev * (1.0 - k)
        out.append(prev)
    return out


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


def roc(values: list, period: int = 10) -> list:
    """Rate of Change — percentage change over *period* bars.

    Args:
        values: List of numeric values (Decimal or float)
        period: Lookback period (default: 10)

    Returns:
        List of ROC percentages (None-padded at start; 0.0 for flat base)
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period + 1:
        return [None] * len(values)

    floats = [_to_float(v) for v in values]
    result: list[float | None] = [None] * period
    for i in range(period, len(floats)):
        base = floats[i - period]
        if base == 0:
            result.append(0.0)
        else:
            result.append(((floats[i] - base) / base) * 100.0)
    return result


def wma(values: list, period: int) -> list[float | None]:
    """Linearly Weighted Moving Average (openalgo-charts parity).

    Matches openalgo-charts ``wma`` (src/indicators/calc.ts): the most
    recent bar carries weight ``period`` and the denominator is
    ``period * (period + 1) / 2``. None before index ``period - 1``; a
    None anywhere in a window nulls that slot (TS NaN carry-through).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(values)
    out: list[float | None] = [None] * n
    floats = [_to_float(v) if v is not None else None for v in values]
    denom = period * (period + 1) / 2
    for i in range(period - 1, n):
        window = floats[i - period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        acc = sum(v * (j + 1) for j, v in enumerate(window))
        out[i] = acc / denom
    return out


def _fractional_wma(values: list, period: float) -> list[float | None]:
    """WMA over a fractional window (openalgo-charts parity).

    Matches ``fractionalWma`` (src/indicators/overlay.ts): ``span =
    ceil(period)``, weight ``period - k`` for ``values[i - k]``, denominator
    ``sum(period - k)``. None before index ``span - 1``; a None anywhere in a
    window nulls that slot (TS NaN carry-through).
    """
    n = len(values)
    out: list[float | None] = [None] * n
    span = math.ceil(period)
    if period <= 0 or n < span:
        return out
    floats = [_to_float(v) if v is not None else None for v in values]
    denom = sum(period - k for k in range(span))
    for i in range(span - 1, n):
        window = floats[i - span + 1 : i + 1]
        if any(v is None for v in window):
            continue
        acc = sum(v * (period - (span - 1 - j)) for j, v in enumerate(window))
        out[i] = acc / denom
    return out


def hma(values: list, period: int) -> list[float | None]:
    """Hull Moving Average (openalgo-charts parity).

    ``wma(2*fractionalWma(half) - wma(period), root)`` with a deliberately
    NON-floored half: ``half = max(0.5, period / 2)`` (an odd length uses a
    fractional window per the TS source) and ``root = max(1,
    floor(sqrt(period)))``. Warmup Nones propagate through each pass exactly
    as TS NaN does.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    period = max(2, int(period))
    half = max(0.5, period / 2)
    root = max(1, math.isqrt(period))
    fast = _fractional_wma(values, half)
    slow = wma(values, period)
    raw = [
        None if f is None or s is None else 2.0 * f - s
        for f, s in zip(fast, slow, strict=True)
    ]
    return wma(raw, root)


def _sma_seeded_ema(values: list[float | None], period: int) -> list[float | None]:
    """SMA-seeded EMA (openalgo-charts parity).

    Matches openalgo-charts ``smaSeededEma`` (src/indicators/calc.ts): seeded
    with the SMA of the first ``period`` values, landing at index
    ``period - 1``; everything earlier stays None.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    prev = sum(v for v in values[:period]) / period
    out[period - 1] = prev
    k = 2.0 / (period + 1)
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def _ema_of_gapped(values: list[float | None], period: int) -> list[float | None]:
    """EMA chained over a warmup-gapped series (openalgo-charts parity).

    Matches openalgo-charts ``emaOfGapped`` (src/indicators/averages.ts): the
    reference EMA re-seeds from ``sma(src, length)`` while its previous value
    is na, so an EMA over a gapped inner series starts ``length - 1`` bars
    after the inner series' first real value. Implemented by slicing off the
    leading Nones, running ``_sma_seeded_ema`` on the live tail, and
    re-padding the front.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    start = 0
    while start < n and values[start] is None:
        start += 1
    if start >= n:
        return out
    out[start:] = _sma_seeded_ema(values[start:], period)
    return out


def dema(values: list, period: int) -> list[float | None]:
    """Double EMA, ``2*e1 - e2`` with ``e2 = ema(e1)`` (openalgo-charts parity).

    Matches openalgo-charts ``DEMA`` (src/indicators/overlay.ts): both passes
    are SMA-seeded, and the second runs over e1's own warmup gap, so the
    first value lands at index ``2*period - 2``.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    floats = [_to_float(v) for v in values]
    e1 = _sma_seeded_ema(floats, period)
    e2 = _ema_of_gapped(e1, period)
    return [
        None if a is None or b is None else 2.0 * a - b
        for a, b in zip(e1, e2, strict=True)
    ]


def tema(values: list, period: int) -> list[float | None]:
    """Triple EMA, ``3*(e1 - e2) + e3`` (openalgo-charts parity).

    Matches openalgo-charts ``TEMA`` (src/indicators/averages.ts): three
    chained SMA-seeded EMAs, each over the previous pass's warmup gap, so
    the first value lands at index ``3*period - 3``.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    floats = [_to_float(v) for v in values]
    e1 = _sma_seeded_ema(floats, period)
    e2 = _ema_of_gapped(e1, period)
    e3 = _ema_of_gapped(e2, period)
    return [
        None if a is None or b is None or c is None else 3.0 * (a - b) + c
        for a, b, c in zip(e1, e2, e3, strict=True)
    ]


def _source_values(candles: list, source: str = "close") -> list[float]:
    """Extract an OHLC source series (openalgo-charts parity).

    Matches ``sourceValues`` (src/model/indicator-registry.ts): open, high,
    low, close direct; hl2, hlc3, ohlc4; volume (missing → 0); anything else
    falls back to close.
    """
    out = []
    for c in candles:
        o = _to_float(c.ohlc.open.value)
        h = _to_float(c.ohlc.high.value)
        lo = _to_float(c.ohlc.low.value)
        cl = _to_float(c.ohlc.close.value)
        if source == "open":
            out.append(o)
        elif source == "high":
            out.append(h)
        elif source == "low":
            out.append(lo)
        elif source == "hl2":
            out.append((h + lo) / 2.0)
        elif source == "hlc3":
            out.append((h + lo + cl) / 3.0)
        elif source == "ohlc4":
            out.append((o + h + lo + cl) / 4.0)
        elif source == "volume":
            out.append(float(c.volume.value))
        else:
            out.append(cl)
    return out


def smma(values: list, period: int) -> list[float | None]:
    """Smoothed MA — Wilder's RMA (openalgo-charts parity).

    Matches ``SMMA`` (src/indicators/averages.ts): alpha ``1/period`` (vs
    EMA ``2/(period+1)``). None before index ``period - 1``.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    floats = [_to_float(v) for v in values]
    return _rma(floats, int(period))


def _generalized_double(values: list[float | None], length: int, factor: float) -> list[float | None]:
    """One T3 layer: ``e1*(1+f) - e2*f`` (openalgo-charts parity).

    Matches ``generalizedDouble`` (src/indicators/averages.ts). At factor 0
    the second pass is skipped so ``NaN*0`` cannot extend the warmup.
    """
    e1 = _ema_of_gapped(values, length)
    if factor == 0:
        return e1
    e2 = _ema_of_gapped(e1, length)
    return [
        None if a is None or b is None else a * (1.0 + factor) - b * factor
        for a, b in zip(e1, e2, strict=True)
    ]


def t3(values: list, length: int, factor: float = 0.7) -> list[float | None]:
    """Tillson T3 — three generalized-double layers (openalgo-charts parity).

    Matches ``T3`` (src/indicators/averages.ts): 6 chained averages, first
    value at index ``6*(length-1)``.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    floats = [_to_float(v) for v in values]
    once = _generalized_double(floats, int(length), float(factor))
    twice = _generalized_double(once, int(length), float(factor))
    return _generalized_double(twice, int(length), float(factor))


def linreg_slope(values: list, period: int) -> list[float | None]:
    """Linear-regression slope, price per bar (openalgo-charts parity).

    Matches ``LINREG_SLOPE`` (src/indicators/adaptive.ts): least squares over
    ``x = 1..period`` with closed-form denominator ``p^2(p^2-1)/12``. None
    before index ``period - 1``; flat windows yield 0.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    period = max(2, int(period))
    floats = [_to_float(v) for v in values]
    n = len(floats)
    out: list[float | None] = [None] * n
    sum_x = (period * (period + 1)) / 2
    denom = (period * period * (period * period - 1)) / 12
    for i in range(period - 1, n):
        sum_y = 0.0
        sum_xy = 0.0
        for k in range(period):
            y = floats[i - k]
            sum_y += y
            sum_xy += y * (period - k)
        out[i] = (period * sum_xy - sum_x * sum_y) / denom
    return out


def _hull_series(values: list[float], mode: str, length: int) -> list[float | None]:
    """Hull core for one mode (openalgo-charts parity, overlay.ts)."""
    n = len(values)
    if mode == "Ehma":
        span_len = max(1, math.floor(length / 2))
        fast = _sma_seeded_ema(values, span_len)
        slow = _sma_seeded_ema(values, length)
        root = max(1, round(math.sqrt(length)))
        raw = [
            None if f is None or s is None else 2.0 * f - s
            for f, s in zip(fast, slow, strict=True)
        ]
        return _ema_of_gapped(raw, root)
    if mode == "Thma":
        # The published definition hands THMA half the length the other two
        # variations get (overlay.ts hullSeries).
        th = max(1, math.floor(length / 2))
        third = wma(values, max(1, math.floor(th / 3)))
        half = wma(values, max(1, math.floor(th / 2)))
        full = wma(values, th)
        raw = [
            None if a is None or b is None or c is None else 3.0 * a - b - c
            for a, b, c in zip(third, half, full, strict=True)
        ]
        return wma(raw, th)
    span_half = max(1, math.floor(length / 2))
    fast = wma(values, span_half)
    slow = wma(values, length)
    root = max(1, round(math.sqrt(length)))
    raw = [
        None if f is None or s is None else 2.0 * f - s
        for f, s in zip(fast, slow, strict=True)
    ]
    return wma(raw, root)


def hull_suite(
    values: list,
    mode: str = "Hma",
    length: int = 55,
    length_mult: float = 1.0,
    visual_switch: bool = True,
) -> dict[str, list]:
    """Hull Suite — Hull MA plus 2-bar displaced twin (openalgo-charts parity).

    Matches ``HULL_SUITE`` (src/indicators/overlay.ts): effective length is
    ``max(1, floor(round(length) * lengthMult))``; ``shull`` is ``mhull``
    shifted +2, or all-None when the band is switched off.
    """
    floats = [_to_float(v) for v in values]
    eff = max(1, math.floor(round(int(length)) * float(length_mult)))
    hull = _hull_series(floats, str(mode), eff)
    n = len(floats)
    if not visual_switch:
        return {"mhull": hull, "shull": [None] * n}
    displaced: list[float | None] = [None] * n
    for i in range(2, n):
        displaced[i] = hull[i - 2]
    return {"mhull": hull, "shull": displaced}


def alma(
    values: list, period: int, offset: float = 0.85, sigma: float = 6.0
) -> list[float | None]:
    """Arnaud Legoux Moving Average (openalgo-charts parity).

    Matches openalgo-charts ``alma`` (src/indicators/calc.ts): Gaussian
    weights ``exp(-((i - m)^2)/(2 s^2))`` with ``m = offset*(period-1)`` and
    ``s = period/sigma``, normalized by their sum; oldest bar in the window
    carries weight[0]. None before index ``period - 1``.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    floats = [_to_float(v) for v in values]
    n = len(floats)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    m = offset * (period - 1)
    s = period / sigma
    weights = [math.exp(-((i - m) ** 2) / (2.0 * s * s)) for i in range(period)]
    norm = sum(weights)
    for i in range(period - 1, n):
        window = floats[i - period + 1 : i + 1]
        out[i] = sum(v * w for v, w in zip(window, weights)) / norm
    return out


def macd(values: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, list]:
    """MACD: EMA(fast) − EMA(slow), plus signal EMA and histogram (parity with
    openalgo-charts ``momentum.ts``).

    Both legs are SMA-seeded (None for the first ``period - 1`` bars); the
    signal smooths the MACD line starting at its first finite value
    (``fromFirstValue``), so outputs are None-padded through the warmup.
    """
    if min(fast, slow, signal) < 1:
        raise ValueError("periods must be positive")
    floats = [_to_float(v) for v in values]
    f = _sma_seeded_ema(floats, int(fast))
    s = _sma_seeded_ema(floats, int(slow))
    line = [
        None if a is None or b is None else a - b
        for a, b in zip(f, s, strict=True)
    ]
    sig = _ema_of_gapped(line, int(signal))
    hist = [
        None if m is None or g is None else m - g
        for m, g in zip(line, sig, strict=True)
    ]
    return {"macd": line, "signal": sig, "histogram": hist}


__all__ = [
    "alma",
    "atr",
    "bollinger",
    "dema",
    "ema",
    "hma",
    "macd",
    "obv",
    "roc",
    "rsi",
    "sma",
    "stochastic",
    "supertrend",
    "tema",
    "vwap_session",
    "wma",
]


def true_ranges(candles: list) -> list[float]:
    """True range per candle: TR[0]=H-L; thereafter max(H-L, |H-prevC|, |L-prevC|).

    Matches openalgo-charts ``trueRange`` (src/indicators/atr.ts): the first
    bar has no prior close, so its true range is just high-low.
    """
    if not candles:
        return []
    first_h = _to_float(candles[0].ohlc.high.value)
    first_l = _to_float(candles[0].ohlc.low.value)
    out: list[float] = [first_h - first_l]
    for i in range(1, len(candles)):
        h = _to_float(candles[i].ohlc.high.value)
        low = _to_float(candles[i].ohlc.low.value)
        pc = _to_float(candles[i - 1].ohlc.close.value)
        out.append(max(h - low, abs(h - pc), abs(low - pc)))
    return out


def atr(candles: list, period: int = 14) -> list[float | None]:
    """Average True Range (Wilder smoothing).

    Matches openalgo-charts ``atr`` (src/indicators/atr.ts): the seed is the
    SMA of TR[0..period-1] and lands at index ``period - 1``; earlier slots
    are None-padded warmup.

    Args:
        candles: Candle objects exposing ``.ohlc.high/.low/.close`` (Decimal)
        period: Smoothing window (default 14)

    Returns:
        ATR values (None before index period-1)
    """
    if period <= 0:
        raise ValueError("period must be positive")
    trs = true_ranges(candles)
    n = len(trs)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    prev = sum(trs[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def bollinger(values: list, period: int = 20, num_std: float = 2.0) -> dict[str, list]:
    """Bollinger Bands: middle SMA with upper/lower at +-num_std deviations.

    Returns dict of parallel lists keyed 'upper'/'middle'/'lower', each
    None-padded for the warmup region.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    mid = sma(values, period)
    floats = [_to_float(v) for v in values]
    n = len(floats)
    upper: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    for i in range(period - 1, n):
        window = floats[i - period + 1 : i + 1]
        m = mid[i]
        if m is None:
            continue
        variance = sum((x - m) ** 2 for x in window) / period
        sd = variance ** 0.5
        offset = num_std * sd
        upper[i] = m + offset
        lower[i] = m - offset
    return {"upper": upper, "middle": mid, "lower": lower}


def obv(candles: list) -> list[float]:
    """On-Balance Volume: cumulative volume signed by close-to-close direction.

    Takes candles (needs close and volume); output has no warmup padding —
    the first bar contributes 0.
    """
    if not candles:
        return []
    out = [0.0]
    for i in range(1, len(candles)):
        close = _to_float(candles[i].ohlc.close.value)
        prev_close = _to_float(candles[i - 1].ohlc.close.value)
        vol = float(candles[i].volume.value)
        if close > prev_close:
            out.append(out[-1] + vol)
        elif close < prev_close:
            out.append(out[-1] - vol)
        else:
            out.append(out[-1])
    return out


def _fn_obv(
    candles: list,
    ma_type: str = "None",
    ma_length: int = 9,
    bb_mult: float = 2.0,
) -> dict[str, list]:
    """OBV plus its engine smoothing companions (volume.ts ``OBV`` calc).

    ``ma`` is the ``ma_type`` kernel over OBV (all-None when ``ma_type`` is
    ``'None'``); ``bbUpper``/``bbLower`` exist only for the
    ``'SMA + Bollinger Bands'`` kernel (plain ``stdev`` offset scaled by
    ``bb_mult``), otherwise all-None. ``value`` aliases ``obv`` for
    backward compat with the pre-companion ``compute_indicator`` shape.
    """
    base = obv(candles)
    n = len(base)
    if n == 0:
        return {"value": [], "obv": [], "ma": [], "bbUpper": [], "bbLower": []}
    vols = [float(c.volume.value) for c in candles]
    mt = str(ma_type)
    length = max(1, int(ma_length))
    if mt == "None":
        ma: list[float | None] = [None] * n
    else:
        ma = _smoothing_ma(mt, base, vols, length)
    if mt == "SMA + Bollinger Bands":
        mult = float(bb_mult)
        sd = _stdev(base, length)
        band: list[float | None] = [None if v is None else v * mult for v in sd]
    else:
        band = [None] * n
    return {
        "value": base,
        "obv": base,
        "ma": ma,
        "bbUpper": [
            None if a is None or b is None else a + b
            for a, b in zip(ma, band, strict=True)
        ],
        "bbLower": [
            None if a is None or b is None else a - b
            for a, b in zip(ma, band, strict=True)
        ],
    }


def _rolling_sma(values: list[float | None], period: int) -> list[float | None]:
    """SMA that yields None unless ALL window entries are finite (TS calc.ts semantics)."""
    n = len(values)
    out: list[float | None] = [None] * n
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window) / period
    return out


# ---------------------------------------------------------------------------
# Shared helpers — batch 4 (volume / flow indicators)
# ---------------------------------------------------------------------------


def _highest(values: list[float | None], period: int) -> list[float | None]:
    """Rolling max over *period* bars.  None for leading window."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0:
        return out
    for i in range(period - 1, n):
        best = -float("inf")
        ok = True
        for k in range(period):
            v = values[i - k]
            if v is None:
                ok = False
                break
            if v > best:
                best = v
        out[i] = best if ok else None
    return out


def _lowest(values: list[float | None], period: int) -> list[float | None]:
    """Rolling min over *period* bars; None in the window blanks it."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        out[i] = min(window)  # type: ignore[type-var]
    return out


def _shift(values: list[float | None], k: int) -> list[float | None]:
    """Displace by k bars: positive draws each value k bars later (TS shift)."""
    n = len(values)
    out: list[float | None] = [None] * n
    for i in range(n):
        j = i + k
        if 0 <= j < n:
            out[j] = values[i]
    return out


def _rma(values: list[float], period: int) -> list[float | None]:
    """Wilder RMA: SMA-seeded, then (prev*(period-1)+v)/period. None before period-1."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def _sma_skip_none(values: list[float | None], period: int) -> list[float | None]:
    """SMA that returns None for any window containing a non-finite value."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    s = 0.0
    bad = 0
    for i in range(n):
        v = values[i]
        if v is not None and _isfinite(v):
            s += v
        else:
            bad += 1
        if i >= period:
            gone = values[i - period]
            if gone is not None and _isfinite(gone):
                s -= gone
            else:
                bad -= 1
        if i >= period - 1:
            out[i] = s / period if bad == 0 else None
    return out


def _is_gap(v: float | None) -> bool:
    """True when *v* is a warmup gap (None or non-finite, i.e. engine NaN)."""
    return v is None or (isinstance(v, float) and not math.isfinite(v))


def _from_first_value(values: list, fn: Callable) -> list[float | None]:
    """Run ``fn`` over the tail from the first finite value, re-pad the front.

    Matches openalgo-charts ``fromFirstValue`` (momentum.ts): a smoother
    chained onto a gapped series starts counting at the series' first real
    value instead of treating warmup holes as bars.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    start = 0
    while start < n and _is_gap(values[start]):
        start += 1
    if start >= n:
        return out
    for j, v in enumerate(fn(list(values[start:]), start)):
        if start + j < n:
            out[start + j] = v
    return out


def _stdev(values: list, period: int) -> list[float | None]:
    """Rolling population standard deviation (openalgo-charts calc.ts parity).

    Mean is the gap-aware SMA; any window containing a gap yields None.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    clean = [None if v is None else _to_float(v) for v in values]
    means = _sma_skip_none(clean, period)
    for i in range(period - 1, n):
        m = means[i]
        window = clean[i - period + 1 : i + 1]
        if m is None or any(v is None for v in window):
            continue
        out[i] = (sum((x - m) ** 2 for x in window) / period) ** 0.5
    return out


def _sma_seeded_ema_gapped(values: list, period: int) -> list[float | None]:
    """SMA-seeded EMA with engine NaN propagation: a gap poisons it forward."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    head = values[:period]
    if any(_is_gap(v) for v in head):
        return out
    prev = sum(_to_float(v) for v in head) / period
    out[period - 1] = prev
    k = 2.0 / (period + 1)
    for i in range(period, n):
        v = values[i]
        if _is_gap(v):
            break  # rest stays None, mirroring NaN carry-through
        prev = _to_float(v) * k + prev * (1.0 - k)
        out[i] = prev
    return out


def _rma_gapped(values: list, period: int) -> list[float | None]:
    """Wilder RMA with engine NaN propagation (seed = SMA, gaps poison forward)."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    head = values[:period]
    if any(_is_gap(v) for v in head):
        return out
    prev = sum(_to_float(v) for v in head) / period
    out[period - 1] = prev
    for i in range(period, n):
        v = values[i]
        if _is_gap(v):
            break
        prev = (prev * (period - 1) + _to_float(v)) / period
        out[i] = prev
    return out


def _wma_gapped(values: list, period: int) -> list[float | None]:
    """Gap-aware WMA (openalgo-charts calc.ts parity: recent bar weighs most)."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    denom = period * (period + 1) / 2
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        if any(_is_gap(v) for v in window):
            continue
        acc = sum(_to_float(v) * (period - k) for k, v in enumerate(reversed(window)))
        out[i] = acc / denom
    return out


def _vwma_series(values: list, volumes: list, length: int) -> list[float | None]:
    """VWMA = sma(src*vol, len) / sma(vol, len) (openalgo-charts calc.ts parity).

    Zero-volume windows yield None, matching the engine's ``den == 0 -> NaN``.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if length <= 0 or n < length:
        return out
    pv = [
        None if _is_gap(v) or w is None else _to_float(v) * float(w)
        for v, w in zip(values, volumes, strict=False)
    ]
    num = _sma_skip_none(pv, length)
    den = _sma_skip_none(
        [None if w is None else float(w) for w in volumes], length
    )
    for i in range(n):
        a, d = num[i], den[i]
        if a is None or d is None or d == 0:
            continue
        out[i] = a / d
    return out


def _smoothing_ma(
    kind: str, values: list, volumes: list, length: int
) -> list[float | None]:
    """The engine "Smoothing" block kernel switch (momentum.ts / volume.ts).

    ``None`` disables smoothing (all-None); ``EMA`` / ``SMMA (RMA)`` /
    ``WMA`` / ``VWMA`` run from the series' first finite value
    (``fromFirstValue``); ``SMA``, ``SMA + Bollinger Bands`` and anything
    else fall back to SMA.
    """
    length = max(1, int(length))
    if kind == "EMA":
        return _from_first_value(values, lambda tail, _s: _sma_seeded_ema_gapped(tail, length))
    if kind == "SMMA (RMA)":
        return _from_first_value(values, lambda tail, _s: _rma_gapped(tail, length))
    if kind == "WMA":
        return _from_first_value(values, lambda tail, _s: _wma_gapped(tail, length))
    if kind == "VWMA":
        return _from_first_value(
            values, lambda tail, s: _vwma_series(tail, list(volumes[s:]), length)
        )
    return _from_first_value(
        values,
        lambda tail, _s: _sma_skip_none(
            [None if v is None else _to_float(v) for v in tail], length
        ),
    )


def _bars_since(cond: list[bool]) -> list[float | None]:
    """Bars elapsed since ``cond`` was last true; None before the first."""
    n = len(cond)
    out: list[float | None] = [None] * n
    last = -1
    for i in range(n):
        if cond[i]:
            last = i
        if last >= 0:
            out[i] = float(i - last)
    return out


def _rolling_sum(values: list[float | None], period: int) -> list[float | None]:
    """Rolling sum over *period* bars.  None for leading window."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    acc = 0.0
    for i in range(n):
        v = values[i]
        if v is None:
            return out  # None encountered → gap
        acc += v
        if i >= period:
            pv = values[i - period]
            if pv is None:
                return out
            acc -= pv
        if i >= period - 1:
            out[i] = acc
    return out


def _cumulative(values: list[float | None]) -> list[float]:
    """Running total.  Non-finite terms count as 0 (TS calc.ts semantics)."""
    out: list[float] = []
    acc = 0.0
    for v in values:
        if v is not None and _isfinite(v):
            acc += v
        out.append(acc)
    return out


def _change(values: list, n: int = 1) -> list[float | None]:
    """Δn: values[i] - values[i-n].  None for the first *n* bars or when
    either side of the pair is None (TS NaN propagation)."""
    nv = [_to_float(v) for v in values]
    m = len(nv)
    out: list[float | None] = [None] * m
    for i in range(n, m):
        a = nv[i]
        b = nv[i - n]
        if a is None or b is None:
            continue
        out[i] = a - b
    return out


def stochastic(
    candles: list, k_period: int = 14, smooth_k: int = 3, d_period: int = 3
) -> dict[str, list]:
    """Stochastic Oscillator: %K smoothed by SMA(smooth_k), %D SMA(d_period).

    Raw %K is None where high==low over the window (TS: span<=0 -> NaN -> null).
    Returns dict keyed 'k'/'d'.
    """
    if k_period <= 0 or d_period <= 0:
        raise ValueError("periods must be positive")
    if smooth_k <= 0:
        raise ValueError("smooth_k must be positive")
    n = len(candles)
    raw: list[float | None] = [None] * n
    for i in range(k_period - 1, n):
        window = candles[i - k_period + 1 : i + 1]
        hh = max(_to_float(c.ohlc.high.value) for c in window)
        ll = min(_to_float(c.ohlc.low.value) for c in window)
        close = _to_float(candles[i].ohlc.close.value)
        rng_span = hh - ll
        raw[i] = None if rng_span <= 0 else ((close - ll) / rng_span) * 100.0
    k = _rolling_sma(raw, smooth_k)
    d = _rolling_sma(k, d_period)
    return {"k": k, "d": d}


def supertrend(candles: list, period: int = 10, multiplier: float = 3.0) -> dict[str, list]:
    """Supertrend line with its direction flag (+1 up / -1 down).

    Matches openalgo-charts ``supertrend`` (src/indicators/supertrend.ts):
    bands carry forward unless price broke the previous band; which band is
    followed is tracked by comparing the previous line to the previous upper
    band (not by direction alone). TS convention is -1=uptrend/+1=downtrend;
    this backend emits the inverse (+1 up / -1 down) per its contract.

    Returns dict keyed 'line'/'direction' plus the engine's 'up'/'down'
    split (supertrend.ts ``supertrendSeries``): 'up' carries the line only
    while the backend direction is +1 (TS direction -1, uptrend), 'down'
    only while it is -1 (TS direction +1, downtrend); the inactive side is
    None so the renderer breaks across flips. None-padded during ATR warmup.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(candles)
    atrs = atr(candles, period)
    line: list[float | None] = [None] * n
    direction: list[int | None] = [None] * n
    prev_upper: float | None = None
    prev_lower: float | None = None
    prev_line: float | None = None
    started = False
    for i in range(n):
        a = atrs[i]
        if a is None:
            continue
        h = _to_float(candles[i].ohlc.high.value)
        low = _to_float(candles[i].ohlc.low.value)
        c = _to_float(candles[i].ohlc.close.value)
        mid = (h + low) / 2.0
        basic_upper = mid + multiplier * a
        basic_lower = mid - multiplier * a
        if not started:
            final_upper, final_lower = basic_upper, basic_lower
        else:
            assert prev_upper is not None and prev_lower is not None
            pc = _to_float(candles[i - 1].ohlc.close.value)
            final_upper = (
                basic_upper if (basic_upper < prev_upper or pc > prev_upper) else prev_upper
            )
            final_lower = (
                basic_lower if (basic_lower > prev_lower or pc < prev_lower) else prev_lower
            )
        # Which band was previously followed? (TS rule: prevST == prevUpper.)
        follows_upper = not started or prev_line == prev_upper
        if follows_upper:
            # was resistance above price; flip up only if close clears it
            ts_dir = 1 if c <= final_upper else -1
            st = final_upper if ts_dir == 1 else final_lower
        else:
            # was support below price; flip down only if close breaks it
            ts_dir = -1 if c >= final_lower else 1
            st = final_lower if ts_dir == -1 else final_upper
        direction[i] = -ts_dir  # backend convention: +1 up / -1 down
        line[i] = st
        prev_upper, prev_lower, prev_line = final_upper, final_lower, st
        started = True
    # Engine up/down split (supertrend.ts::supertrendSeries): the active side
    # carries the line, the inactive side carries None (backend +1 == TS -1).
    up = [v if d == 1 else None for v, d in zip(line, direction, strict=True)]
    down = [v if d == -1 else None for v, d in zip(line, direction, strict=True)]
    return {"line": line, "direction": direction, "up": up, "down": down}


def _vwap_anchor_key(ts: Any, anchor: str) -> Any:
    """Reset-group key for one VWAP anchor (openalgo-charts trend.ts parity).

    ``session`` restarts on the calendar-day boundary (the backend's
    historical behaviour); coarser anchors restart on week (Monday-based,
    ISO), month, quarter and year boundaries; ``continuous`` never resets.
    Unknown anchors fall back to ``session``.
    """
    if anchor == "continuous":
        return 0
    if anchor == "week":
        iso = ts.isocalendar()
        return (iso[0], iso[1])
    if anchor == "month":
        return (ts.year, ts.month)
    if anchor == "quarter":
        return (ts.year, (ts.month - 1) // 3)
    if anchor == "year":
        return ts.year
    return ts.date()


def _vwap_columns(
    candles: list, anchor: str = "session", source: str = "hlc3", percent_mode: bool = False
) -> tuple[list, list]:
    """VWAP accumulation plus the shared band half-width column.

    Matches trend.ts ``VWAP`` calc: volume-weighted mean with the anchor
    restarting the ``pv``/``vol``/``pv2`` accumulators, and ``basis`` as the
    volume-weighted stdev (``sqrt(max(0, pv2/vol - mean^2))``), or
    ``mean * 0.01`` in ``percent`` calcMode. None where volume is 0.
    """
    values = _source_values(candles, source)
    n = len(candles)
    vwap: list[float | None] = [None] * n
    basis: list[float | None] = [None] * n
    pv = 0.0
    vol = 0.0
    pv2 = 0.0
    current_key = object()
    for i, c in enumerate(candles):
        key = _vwap_anchor_key(c.timestamp, anchor)
        if key != current_key:
            current_key = key
            pv = 0.0
            vol = 0.0
            pv2 = 0.0
        v = float(c.volume.value)
        x = values[i]
        pv += x * v
        pv2 += x * x * v
        vol += v
        if vol <= 0:
            continue
        mean = pv / vol
        vwap[i] = mean
        basis[i] = mean * 0.01 if percent_mode else max(0.0, pv2 / vol - mean * mean) ** 0.5
    return vwap, basis


def _vwap_band(
    vwap: list, basis: list, show: bool, mult: float, sign: float
) -> list[float | None]:
    """One VWAP band edge: ``vwap + sign * basis * mult`` (all-None if hidden)."""
    n = len(vwap)
    out: list[float | None] = [None] * n
    if not show:
        return out
    m = float(mult)
    for i in range(n):
        v = vwap[i]
        b = basis[i]
        if v is not None and b is not None:
            out[i] = v + sign * b * m
    return out


def _fn_vwap(
    candles: list,
    anchor: str = "session",
    source: str = "hlc3",
    offset: int = 0,
    calcMode: str = "stdev",
    showBand1: bool = True,
    bandMult1: float = 1.0,
    showBand2: bool = False,
    bandMult2: float = 2.0,
    showBand3: bool = False,
    bandMult3: float = 3.0,
) -> dict[str, list]:
    """Session-anchored VWAP plus engine stdev band pairs (trend.ts ``VWAP``).

    Every column is displaced by ``offset`` bars (engine ``shiftColumn``).
    ``value`` aliases ``vwap`` for backward compat with the pre-band
    ``compute_indicator`` shape.
    """
    percent = str(calcMode) == "percent"
    vwap, basis = _vwap_columns(candles, str(anchor), str(source), percent)
    off = int(round(float(offset)))
    line = _shift(vwap, off)
    return {
        "value": line,
        "vwap": line,
        "upper1": _shift(_vwap_band(vwap, basis, bool(showBand1), bandMult1, 1.0), off),
        "lower1": _shift(_vwap_band(vwap, basis, bool(showBand1), bandMult1, -1.0), off),
        "upper2": _shift(_vwap_band(vwap, basis, bool(showBand2), bandMult2, 1.0), off),
        "lower2": _shift(_vwap_band(vwap, basis, bool(showBand2), bandMult2, -1.0), off),
        "upper3": _shift(_vwap_band(vwap, basis, bool(showBand3), bandMult3, 1.0), off),
        "lower3": _shift(_vwap_band(vwap, basis, bool(showBand3), bandMult3, -1.0), off),
    }


def vwap_session(candles: list) -> list[float | None]:
    """VWAP anchored to each session start (09:15 IST calendar day boundary).

    Cumulative (typical price * volume) / cumulative volume, resetting at
    each new session day. Needs candles with volume; None where volume is 0.
    """
    return _fn_vwap(candles)["value"]


# ---------------------------------------------------------------------------
# Registry — single source of truth for what the chart frontend may compute.
#
# Each spec describes one indicator the way the chart's catalogue endpoint
# serves it: id, display name, category, placement (overlay draws on the
# price pane; pane gets its own sub-pane), parameter schema with defaults,
# and the plots the computation returns. The frontend builds its whole
# indicator menu from this catalogue; adding an entry here is the entire
# act of shipping a new indicator to the UI.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """One registered indicator: metadata + callable + plot shape."""

    id: str
    name: str
    category: str
    placement: str  # 'overlay' | 'pane'
    params: tuple[tuple[str, str, Any], ...]  # (name, type, default)
    plots: tuple[tuple[str, str, str], ...]  # (plot key, kind, title)
    levels: tuple[dict[str, Any], ...] = ()  # fixed reference lines
    fn: Callable[..., Any] | None = None


# Single dispatch seam: the first-class REGISTRY is the source of truth.
# We store the full-fidelity catalog spec (trigrams/plots/levels/fn) so the
# catalogue, compute and engine all read one place — no lossy mirror.
from tradex_trading.analytics.registry import REGISTRY  # noqa: E402


def register_indicator(spec: IndicatorSpec) -> None:
    """Register an indicator spec by id. Last registration wins.

    Writes into the first-class :data:`REGISTRY` (the single source of
    truth). The whole-fidelity spec is stored as-is, so the catalogue,
    ``get_indicator_spec``, parameter resolution and ``compute_indicator``
    all read the same object the engine dispatches through.
    """
    REGISTRY.register(spec.id, spec)


def get_indicator_spec(indicator_id: str) -> IndicatorSpec | None:
    """Look up a spec; None for unknown ids (callers decide fail-loudness)."""
    try:
        return REGISTRY.get(indicator_id)
    except KeyError:
        return None


def indicator_catalogue() -> list[dict[str, Any]]:
    """Serialisable catalogue for the chart frontend's menu."""
    return [
        {
            "id": s.id,
            "name": s.name,
            "category": s.category,
            "placement": s.placement,
            "params": [{"name": p[0], "type": p[1], "default": p[2]} for p in s.params],
            "plots": [{"key": p[0], "kind": p[1], "title": p[2]} for p in s.plots],
            "levels": list(s.levels),
        }
        for s in REGISTRY.all()
    ]


def _resolve_indicator_params(
    indicator_id: str, params: dict[str, Any] | None
) -> dict[str, Any]:
    """Validate ``params`` against the spec's schema and return a
    fully-merged dict (defaults filled, explicit ``None`` falling back to the
    declared default, unknown names rejected).

    Used by :func:`compute_indicator` so frontend typos fail loudly.
    """
    try:
        spec = REGISTRY.get(indicator_id)
    except KeyError:
        raise ValueError(f"unknown indicator: {indicator_id!r}") from None
    supplied = dict(params or {})
    known = {p[0] for p in spec.params}
    unknown = set(supplied) - known
    if unknown:
        raise ValueError(f"unknown params for {indicator_id}: {sorted(unknown)}")
    merged = {}
    for name, _kind, default in spec.params:
        value = supplied.get(name, default)
        # An explicit JSON ``null`` means "not supplied": the browser serialises
        # NaN defaults (a text/select param read through a numeric input) as
        # null, and computing with e.g. ma_type=None crashes downstream.
        # Fall back to the declared default instead of trusting the null.
        if value is None:
            value = default
        merged[name] = value
    return merged


def compute_indicator(
    indicator_id: str,
    candles: list,
    params: dict[str, Any] | None = None,
) -> dict[str, list]:
    """Compute a registered indicator over candles; returns per-plot lists.

    Params are validated against the spec: unknown names are rejected rather
    than silently ignored, so a frontend typo fails loudly instead of
    computing defaults that look right.
    """
    try:
        spec = REGISTRY.get(indicator_id)
    except KeyError:
        raise ValueError(f"unknown indicator: {indicator_id!r}") from None
    if spec.fn is None:
        raise ValueError(f"unknown indicator: {indicator_id!r}")
    merged = _resolve_indicator_params(indicator_id, params)
    result = spec.fn(candles, **merged)
    if isinstance(result, dict):
        return result
    return {"value": result}


def _builtin_specs() -> list[IndicatorSpec]:
    closes_only = lambda candles: [_to_float(c.ohlc.close.value) for c in candles]  # noqa: E731

    def _fn_sma(candles, period, source="close"):
        return sma(_source_values(candles, source), int(period))

    def _fn_ema(candles, period, source="close"):
        return _sma_seeded_ema(_source_values(candles, source), int(period))

    def _fn_wma(candles, period):
        return wma(closes_only(candles), int(period))

    def _fn_hma(candles, period, source="close"):
        return hma(_source_values(candles, source), int(period))

    def _fn_dema(candles, period):
        return dema(closes_only(candles), int(period))

    def _fn_tema(candles, period):
        return tema(closes_only(candles), int(period))

    def _fn_alma(candles, period, offset, sigma):
        return alma(closes_only(candles), int(period), float(offset), float(sigma))

    def _fn_smma(candles, length, source):
        return {"smma": smma(_source_values(candles, source), int(length))}

    def _fn_t3(candles, length, factor, source, highlightMovements=True):
        return {"t3": t3(_source_values(candles, source), int(length), float(factor))}

    def _fn_linreg_slope(candles, periods):
        return {"slope": linreg_slope(closes_only(candles), int(periods))}

    def _fn_hull_suite(candles, source, mode, length, lengthMult, visualSwitch, switchColor=True, candleCol=False):
        return hull_suite(
            _source_values(candles, source), str(mode), int(length),
            float(lengthMult), bool(visualSwitch),
        )

    def _fn_rsi(candles, period, source="close"):
        return rsi(_source_values(candles, source), int(period))

    def _fn_roc(candles, period):
        return roc(closes_only(candles), int(period))

    def _fn_macd(candles, fast, slow, signal, source="close"):
        return macd(_source_values(candles, source), int(fast), int(slow), int(signal))

    return [
        IndicatorSpec(
            id="sma", name="SMA", category="Trend", placement="overlay",
            params=(("period", "int", 20), ("source", "source", "close")),
            plots=(("value", "line", "SMA"),),
            fn=_fn_sma,
        ),
        IndicatorSpec(
            id="ema", name="EMA", category="Trend", placement="overlay",
            params=(("period", "int", 20), ("source", "source", "close")),
            plots=(("value", "line", "EMA"),),
            fn=_fn_ema,
        ),
        IndicatorSpec(
            id="wma", name="WMA", category="Trend", placement="overlay",
            params=(("period", "int", 20),),
            plots=(("value", "line", "WMA"),),
            fn=_fn_wma,
        ),
        IndicatorSpec(
            id="hma", name="HMA", category="Trend", placement="overlay",
            params=(("period", "int", 9), ("source", "source", "close")),
            plots=(("value", "line", "HMA"),),
            fn=_fn_hma,
        ),
        IndicatorSpec(
            id="dema", name="DEMA", category="Trend", placement="overlay",
            params=(("period", "int", 9),),
            plots=(("value", "line", "DEMA"),),
            fn=_fn_dema,
        ),
        IndicatorSpec(
            id="tema", name="TEMA", category="Trend", placement="overlay",
            params=(("period", "int", 9),),
            plots=(("value", "line", "TEMA"),),
            fn=_fn_tema,
        ),
        IndicatorSpec(
            id="alma", name="ALMA", category="Trend", placement="overlay",
            params=(("period", "int", 9), ("offset", "float", 0.85), ("sigma", "float", 6.0)),
            plots=(("value", "line", "ALMA"),),
            fn=_fn_alma,
        ),
        IndicatorSpec(
            id="smma", name="SMMA", category="Trend", placement="overlay",
            params=(("length", "int", 7), ("source", "source", "close")),
            plots=(("smma", "line", "SMMA"),),
            fn=_fn_smma,
        ),
        IndicatorSpec(
            id="t3", name="T3", category="Trend", placement="overlay",
            params=(("length", "int", 5), ("factor", "float", 0.7), ("source", "source", "close"), ("highlightMovements", "bool", True)),
            plots=(("t3", "line", "T3"),),
            fn=_fn_t3,
        ),
        IndicatorSpec(
            id="linreg-slope", name="Linear Regression Slope", category="Trend", placement="pane",
            params=(("periods", "int", 14),),
            plots=(("slope", "line", "Slope"),),
            levels=({"value": 0},),
            fn=_fn_linreg_slope,
        ),
        IndicatorSpec(
            id="hull-suite", name="Hull Suite", category="Trend", placement="overlay",
            params=(
                ("source", "source", "close"),
                ("mode", "select", "Hma"),
                ("length", "int", 55),
                ("lengthMult", "float", 1.0),
                ("visualSwitch", "bool", True),
                ("switchColor", "bool", True),
                ("candleCol", "bool", False),
            ),
            plots=(("mhull", "line", "Hull"), ("shull", "line", "Hull Displaced")),
            fn=_fn_hull_suite,
        ),
        IndicatorSpec(
            id="rsi", name="RSI", category="Momentum", placement="pane",
            params=(("period", "int", 14), ("source", "source", "close")),
            plots=(("value", "line", "RSI"),),
            levels=({"value": 70}, {"value": 30}),
            fn=_fn_rsi,
        ),
        IndicatorSpec(
            id="roc", name="ROC", category="Momentum", placement="pane",
            params=(("period", "int", 10),),
            plots=(("value", "line", "ROC"),),
            levels=({"value": 0}),
            fn=_fn_roc,
        ),
        IndicatorSpec(
            id="macd", name="MACD", category="Momentum", placement="pane",
            params=(("fast", "int", 12), ("slow", "int", 26), ("signal", "int", 9), ("source", "source", "close")),
            plots=(
                ("macd", "line", "MACD"),
                ("signal", "line", "Signal"),
                ("histogram", "histogram", "Histogram"),
            ),
            levels=({"value": 0}),
            fn=_fn_macd,
        ),
        IndicatorSpec(
            id="bollinger", name="Bollinger Bands", category="Volatility",
            placement="overlay",
            params=(("period", "int", 20), ("num_std", "float", 2.0), ("source", "source", "close")),
            plots=(
                ("upper", "line", "Upper"),
                ("middle", "line", "Middle"),
                ("lower", "line", "Lower"),
            ),
            fn=lambda candles, period, num_std, source="close": bollinger(
                _source_values(candles, source), int(period), float(num_std)
            ),
        ),
        IndicatorSpec(
            id="atr", name="ATR", category="Volatility", placement="pane",
            params=(("period", "int", 14),),
            plots=(("value", "line", "ATR"),),
            fn=atr,
        ),
        IndicatorSpec(
            id="vwap", name="VWAP (session)", category="Volume", placement="overlay",
            params=(
                ("anchor", "select", "session"),
                ("source", "source", "hlc3"),
                ("offset", "int", 0),
                ("calcMode", "select", "stdev"),
                ("showBand1", "bool", True),
                ("bandMult1", "float", 1.0),
                ("showBand2", "bool", False),
                ("bandMult2", "float", 2.0),
                ("showBand3", "bool", False),
                ("bandMult3", "float", 3.0),
            ),
            plots=(
                ("value", "line", "VWAP"),
                ("vwap", "line", "VWAP"),
                ("upper1", "line", "Upper Band #1"),
                ("lower1", "line", "Lower Band #1"),
                ("upper2", "line", "Upper Band #2"),
                ("lower2", "line", "Lower Band #2"),
                ("upper3", "line", "Upper Band #3"),
                ("lower3", "line", "Lower Band #3"),
            ),
            fn=_fn_vwap,
        ),
        IndicatorSpec(
            id="obv", name="OBV", category="Volume", placement="pane",
            params=(
                ("ma_type", "select", "None"),
                ("ma_length", "int", 9),
                ("bb_mult", "float", 2.0),
            ),
            plots=(
                ("value", "line", "OBV"),
                ("obv", "line", "OBV"),
                ("ma", "line", "OBV-based MA"),
                ("bbUpper", "line", "Upper Bollinger Band"),
                ("bbLower", "line", "Lower Bollinger Band"),
            ),
            fn=_fn_obv,
        ),
        IndicatorSpec(
            id="stochastic", name="Stochastic", category="Momentum", placement="pane",
            params=(("k_period", "int", 14), ("smooth_k", "int", 3), ("d_period", "int", 3)),
            plots=(("k", "line", "%K"), ("d", "line", "%D"),),
            levels=({"value": 80}, {"value": 20}),
            fn=stochastic,
        ),
        IndicatorSpec(
            id="supertrend", name="Supertrend", category="Trend", placement="overlay",
            params=(("period", "int", 10), ("multiplier", "float", 3.0)),
            plots=(
                ("line", "line", "Supertrend"),
                ("up", "line", "Supertrend Up"),
                ("down", "line", "Supertrend Down"),
            ),
            fn=supertrend,
        ),
    ]


for _spec in _builtin_specs():
    register_indicator(_spec)

# Batch 1 ports — full IndicatorSpec objects (metadata + fn) defined beside
# their implementations; registering them here is the whole act of shipping.
from .band_overlays import (  # noqa: E402
    SPEC_DONCHIAN,
    SPEC_ENVELOPE,
    SPEC_KELTNER_CHANNEL,
)
from .ma_vol import (  # noqa: E402
    SPEC_LSMA,
    SPEC_MCGINLEY_DYNAMIC,
    SPEC_TWAP,
    SPEC_VWMA,
)
from .median_study import SPEC_MEDIAN  # noqa: E402

for _spec in (
    SPEC_VWMA,
    SPEC_TWAP,
    SPEC_MCGINLEY_DYNAMIC,
    SPEC_LSMA,
    SPEC_ENVELOPE,
    SPEC_DONCHIAN,
    SPEC_KELTNER_CHANNEL,
    SPEC_MEDIAN,
):
    register_indicator(_spec)

# Batch 2 ports — oscillators & trend / strength / range A/B
from .oscillators.oscillators_range_a import (  # noqa: E402
    SPEC_COPPOCK_CURVE,
    SPEC_DPO,
    SPEC_STOCHASTIC_RSI,
    SPEC_ULTIMATE_OSCILLATOR,
    SPEC_WILLIAMS_PERCENT_R,
)
from .oscillators.oscillators_range_b import (  # noqa: E402
    SPEC_BALANCE_OF_POWER,
    SPEC_CHANDE_MOMENTUM,
    SPEC_CONNORS_RSI,
    SPEC_FISHER_TRANSFORM,
)
from .oscillators.oscillators_strength import (  # noqa: E402
    SPEC_MFI,
    SPEC_PPO,
    SPEC_SMI,
    SPEC_SMI_ERGODIC_INDICATOR,
    SPEC_SMI_ERGODIC_OSCILLATOR,
    SPEC_TRIX,
    SPEC_TSI,
)
from .oscillators.oscillators_trend import (  # noqa: E402
    SPEC_ADX,
    SPEC_AROON,
    SPEC_AROON_OSCILLATOR,
    SPEC_AWESOME_OSCILLATOR,
    SPEC_CCI,
)
from .volatility.volatility_bands import (  # noqa: E402
    SPEC_BB_TREND,
    SPEC_BOLLINGER_BANDWIDTH,
    SPEC_BOLLINGER_PERCENT_B,
    SPEC_KAMA,
    SPEC_MA_CHANNEL,
    SPEC_STANDARD_ERROR_BANDS,
)
from .volatility.volatility_chop import (  # noqa: E402
    SPEC_AVERAGE_DAILY_RANGE,
    SPEC_CHAIKIN_VOLATILITY,
    SPEC_CHOP_ZONE,
    SPEC_CHOPPINESS_INDEX,
    SPEC_HISTORICAL_VOLATILITY,
    SPEC_STANDARD_DEVIATION,
    SPEC_STANDARD_ERROR,
)
from .volatility.volatility_stops import (  # noqa: E402
    SPEC_CHANDE_KROLL_STOP,
    SPEC_CHANDELIER_EXIT,
    SPEC_VOLATILITY_STOP,
)
from .volume.volume_flow import (  # noqa: E402
    SPEC_CHAIKIN_MONEY_FLOW,
    SPEC_CHAIKIN_OSCILLATOR,
    SPEC_EASE_OF_MOVEMENT,
    SPEC_ELDER_FORCE_INDEX,
    SPEC_ULCER_INDEX,
)
from .volume.volume_indices import (  # noqa: E402
    SPEC_KLINGER_OSCILLATOR,
    SPEC_KNOW_SURE_THING,
    SPEC_MASS_INDEX,
    SPEC_NVI,
    SPEC_PVI,
    SPEC_PVO,
)
from .volume.volume_simple import (  # noqa: E402
    SPEC_ADL,
    SPEC_NET_VOLUME,
    SPEC_PVT,
    SPEC_VOLUME,
)

for _spec in (
    SPEC_ADX,
    SPEC_AROON,
    SPEC_AROON_OSCILLATOR,
    SPEC_AWESOME_OSCILLATOR,
    SPEC_CCI,
    SPEC_MFI,
    SPEC_PPO,
    SPEC_TRIX,
    SPEC_TSI,
    SPEC_SMI,
    SPEC_SMI_ERGODIC_INDICATOR,
    SPEC_SMI_ERGODIC_OSCILLATOR,
    SPEC_STOCHASTIC_RSI,
    SPEC_WILLIAMS_PERCENT_R,
    SPEC_ULTIMATE_OSCILLATOR,
    SPEC_COPPOCK_CURVE,
    SPEC_DPO,
    SPEC_FISHER_TRANSFORM,
    SPEC_CHANDE_MOMENTUM,
    SPEC_CONNORS_RSI,
    SPEC_BALANCE_OF_POWER,
    SPEC_BOLLINGER_PERCENT_B,
    SPEC_BOLLINGER_BANDWIDTH,
    SPEC_BB_TREND,
    SPEC_KAMA,
    SPEC_MA_CHANNEL,
    SPEC_STANDARD_ERROR_BANDS,
    SPEC_CHAIKIN_VOLATILITY,
    SPEC_STANDARD_DEVIATION,
    SPEC_STANDARD_ERROR,
    SPEC_CHOPPINESS_INDEX,
    SPEC_HISTORICAL_VOLATILITY,
    SPEC_AVERAGE_DAILY_RANGE,
    SPEC_CHOP_ZONE,
    SPEC_VOLATILITY_STOP,
    SPEC_CHANDELIER_EXIT,
    SPEC_CHANDE_KROLL_STOP,
    SPEC_ADL,
    SPEC_NET_VOLUME,
    SPEC_VOLUME,
    SPEC_PVT,
    SPEC_CHAIKIN_MONEY_FLOW,
    SPEC_CHAIKIN_OSCILLATOR,
    SPEC_EASE_OF_MOVEMENT,
    SPEC_ELDER_FORCE_INDEX,
    SPEC_ULCER_INDEX,
    SPEC_NVI,
    SPEC_PVI,
    SPEC_PVO,
    SPEC_MASS_INDEX,
    SPEC_KNOW_SURE_THING,
    SPEC_KLINGER_OSCILLATOR,
):
    register_indicator(_spec)

# Batch 5 ports — complex studies
from .studies.studies_complex import (  # noqa: E402
    SPEC_CPR,
    SPEC_RANGE_ANALYSIS,
    SPEC_RELATIVE_VIGOR_INDEX,
    SPEC_RELATIVE_VOLATILITY_INDEX,
    SPEC_VORTEX,
)
from .studies.studies_signals import (  # noqa: E402
    SPEC_CONSOLIDATION_BREAKOUT,
    SPEC_RSI_DIVERGENCE,
    SPEC_TREND_STRENGTH_INDEX,
    SPEC_WAVETREND,
    SPEC_WILLIAMS_FRACTALS,
    SPEC_WILLIAMS_VIX_FIX,
)
from .studies.studies_simple import (  # noqa: E402
    SPEC_MA_CROSS,
    SPEC_MA_RIBBON,
    SPEC_MOMENTUM,
    SPEC_SPECIAL_K,
    SPEC_WOODIES_CCI,
)
from .studies.studies_trend import (  # noqa: E402
    SPEC_ALLIGATOR,
    SPEC_ALPHATREND,
    SPEC_HALFTREND,
    SPEC_ICHIMOKU,
    SPEC_PARABOLIC_SAR,
)
from .seasonality import SPEC_SEASONALITY  # noqa: E402

for _spec in (
    SPEC_MOMENTUM,
    SPEC_MA_CROSS,
    SPEC_MA_RIBBON,
    SPEC_WOODIES_CCI,
    SPEC_SPECIAL_K,
    SPEC_ALLIGATOR,
    SPEC_PARABOLIC_SAR,
    SPEC_ICHIMOKU,
    SPEC_HALFTREND,
    SPEC_ALPHATREND,
    SPEC_CPR,
    SPEC_RANGE_ANALYSIS,
    SPEC_VORTEX,
    SPEC_RELATIVE_VIGOR_INDEX,
    SPEC_RELATIVE_VOLATILITY_INDEX,
    SPEC_RSI_DIVERGENCE,
    SPEC_CONSOLIDATION_BREAKOUT,
    SPEC_TREND_STRENGTH_INDEX,
    SPEC_WILLIAMS_FRACTALS,
    SPEC_WILLIAMS_VIX_FIX,
    SPEC_WAVETREND,
    SPEC_SEASONALITY,
):
    register_indicator(_spec)

