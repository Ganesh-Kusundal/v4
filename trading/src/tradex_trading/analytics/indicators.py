"""Technical indicators — stdlib implementation with optional numpy acceleration."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

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


def hma(values: list, period: int) -> list[float | None]:
    """Hull Moving Average (openalgo-charts parity).

    ``wma(2*wma(half) - wma(period), root)`` where both sub-periods are
    FLOORED per the TS source (integer division truncates, never rounds):
    half = max(1, floor(period/2)) — so 9 smooths over 4 bars — and
    root = max(1, floor(sqrt(period))). Warmup Nones propagate through
    each pass exactly as TS NaN does.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    half = max(1, period // 2)
    root = max(1, math.isqrt(period))
    fast = wma(values, half)
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
    openalgo-charts). Full-length lists, no warmup padding."""
    if min(fast, slow, signal) < 1:
        raise ValueError("periods must be positive")
    f = ema(values, fast)
    s = ema(values, slow)
    line = [a - b for a, b in zip(f, s, strict=True)]
    sig = ema(line, signal)
    hist = [m - g for m, g in zip(line, sig, strict=True)]
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

    Returns dict keyed 'line'/'direction'. None-padded during ATR warmup.
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
    return {"line": line, "direction": direction}


def vwap_session(candles: list) -> list[float | None]:
    """VWAP anchored to each session start (09:15 IST calendar day boundary).

    Cumulative (typical price * volume) / cumulative volume, resetting at
    each new session day. Needs candles with volume; None where volume is 0.
    """
    out: list[float | None] = []
    cum_pv = 0.0
    cum_v = 0.0
    current_day = None
    for c in candles:
        ts = c.timestamp
        day = ts.date()
        if day != current_day:
            current_day = day
            cum_pv = 0.0
            cum_v = 0.0
        typical = (
            _to_float(c.ohlc.high.value)
            + _to_float(c.ohlc.low.value)
            + _to_float(c.ohlc.close.value)
        ) / 3.0
        v = float(c.volume.value)
        cum_pv += typical * v
        cum_v += v
        out.append(cum_pv / cum_v if cum_v > 0 else None)
    return out


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


@dataclass(slots=True)
class IndicatorResult:
    """Aligned computation output: one value-list per plot key."""

    values: dict[str, list] = field(default_factory=dict)


_REGISTRY: dict[str, IndicatorSpec] = {}


def register_indicator(spec: IndicatorSpec) -> None:
    """Register an indicator spec by id. Last registration wins."""
    _REGISTRY[spec.id] = spec


def get_indicator_spec(indicator_id: str) -> IndicatorSpec | None:
    """Look up a spec; None for unknown ids (callers decide fail-loudness)."""
    return _REGISTRY.get(indicator_id)


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
        for s in _REGISTRY.values()
    ]


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
    spec = _REGISTRY.get(indicator_id)
    if spec is None or spec.fn is None:
        raise ValueError(f"unknown indicator: {indicator_id!r}")
    supplied = dict(params or {})
    known = {p[0] for p in spec.params}
    unknown = set(supplied) - known
    if unknown:
        raise ValueError(f"unknown params for {indicator_id}: {sorted(unknown)}")
    merged = {p[0]: supplied.get(p[0], p[2]) for p in spec.params}
    result = spec.fn(candles, **merged)
    if isinstance(result, dict):
        return result
    return {"value": result}


def _builtin_specs() -> list[IndicatorSpec]:
    closes_only = lambda candles: [_to_float(c.ohlc.close.value) for c in candles]  # noqa: E731

    def _fn_sma(candles, period):
        return sma(closes_only(candles), int(period))

    def _fn_ema(candles, period):
        return ema(closes_only(candles), int(period))

    def _fn_wma(candles, period):
        return wma(closes_only(candles), int(period))

    def _fn_hma(candles, period):
        return hma(closes_only(candles), int(period))

    def _fn_dema(candles, period):
        return dema(closes_only(candles), int(period))

    def _fn_tema(candles, period):
        return tema(closes_only(candles), int(period))

    def _fn_alma(candles, period, offset, sigma):
        return alma(closes_only(candles), int(period), float(offset), float(sigma))

    def _fn_rsi(candles, period):
        return rsi(closes_only(candles), int(period))

    def _fn_roc(candles, period):
        return roc(closes_only(candles), int(period))

    def _fn_macd(candles, fast, slow, signal):
        return macd(closes_only(candles), int(fast), int(slow), int(signal))

    return [
        IndicatorSpec(
            id="sma", name="SMA", category="Trend", placement="overlay",
            params=(("period", "int", 20),),
            plots=(("value", "line", "SMA"),),
            fn=_fn_sma,
        ),
        IndicatorSpec(
            id="ema", name="EMA", category="Trend", placement="overlay",
            params=(("period", "int", 20),),
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
            params=(("period", "int", 9),),
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
            id="rsi", name="RSI", category="Momentum", placement="pane",
            params=(("period", "int", 14),),
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
            params=(("fast", "int", 12), ("slow", "int", 26), ("signal", "int", 9)),
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
            params=(("period", "int", 20), ("num_std", "float", 2.0)),
            plots=(
                ("upper", "line", "Upper"),
                ("middle", "line", "Middle"),
                ("lower", "line", "Lower"),
            ),
            fn=lambda candles, period, num_std: bollinger(
                closes_only(candles), int(period), float(num_std)
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
            params=(),
            plots=(("value", "line", "VWAP"),),
            fn=vwap_session,
        ),
        IndicatorSpec(
            id="obv", name="OBV", category="Volume", placement="pane",
            params=(),
            plots=(("value", "line", "OBV"),),
            fn=obv,
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
            plots=(("line", "line", "Supertrend"),),
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
from .oscillators_range_a import (  # noqa: E402
    SPEC_COPPOCK_CURVE,
    SPEC_DPO,
    SPEC_STOCHASTIC_RSI,
    SPEC_ULTIMATE_OSCILLATOR,
    SPEC_WILLIAMS_PERCENT_R,
)
from .oscillators_range_b import (  # noqa: E402
    SPEC_BALANCE_OF_POWER,
    SPEC_CHANDE_MOMENTUM,
    SPEC_CONNORS_RSI,
    SPEC_FISHER_TRANSFORM,
)
from .oscillators_strength import (  # noqa: E402
    SPEC_MFI,
    SPEC_PPO,
    SPEC_SMI,
    SPEC_SMI_ERGODIC_INDICATOR,
    SPEC_SMI_ERGODIC_OSCILLATOR,
    SPEC_TRIX,
    SPEC_TSI,
)
from .oscillators_trend import (  # noqa: E402
    SPEC_ADX,
    SPEC_AROON,
    SPEC_AROON_OSCILLATOR,
    SPEC_AWESOME_OSCILLATOR,
    SPEC_CCI,
)

from .volatility_bands import (  # noqa: E402
    SPEC_BOLLINGER_BANDWIDTH,
    SPEC_BOLLINGER_PERCENT_B,
    SPEC_BB_TREND,
    SPEC_KAMA,
)

from .volatility_chop import (  # noqa: E402
    SPEC_AVERAGE_DAILY_RANGE,
    SPEC_CHOP_ZONE,
    SPEC_CHOPPINESS_INDEX,
    SPEC_HISTORICAL_VOLATILITY,
)

from .volatility_stops import (  # noqa: E402
    SPEC_CHANDE_KROLL_STOP,
    SPEC_CHANDELIER_EXIT,
    SPEC_VOLATILITY_STOP,
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
    SPEC_CHOPPINESS_INDEX,
    SPEC_HISTORICAL_VOLATILITY,
    SPEC_AVERAGE_DAILY_RANGE,
    SPEC_CHOP_ZONE,
    SPEC_VOLATILITY_STOP,
    SPEC_CHANDELIER_EXIT,
    SPEC_CHANDE_KROLL_STOP,
):
    register_indicator(_spec)
