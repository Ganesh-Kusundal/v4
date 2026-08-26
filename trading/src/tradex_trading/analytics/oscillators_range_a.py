"""Oscillators range A — StochRSI, Williams %R, Ultimate, Coppock, DPO.

Batch 2 parallel split: standalone module so four indicator-port tasks can run
concurrently without touching ``indicators.py``.  Helpers are imported from
``indicators.py``; all parity notes reference openalgo-charts
``src/indicators/ranges.ts`` (StochRSI/Williams/Ultimate) and
``src/indicators/oscillators.ts`` (Coppock/DPO), plus ``src/indicators/calc.ts``.

Parity notes:
- StochRSI: rsi -> stoch(rsi,rsi,rsi,lengthStoch) -> sma(smoothK) -> sma(smoothD)
  with ``fromFirstValue`` slicing semantics (implemented via ``_rolling_sma``
  strict-None propagation).  Flat RSI window gives span 0 -> None.
- Williams %R: ``100*(close - highestHigh)/(highestHigh - lowestLow)`` over
  ``length``; span 0 -> None; bounds are exactly -100..0.
- Ultimate: buying pressure ``close - min(low, prevClose)`` over true range
  ``max(high, prevClose)-min(low, prevClose)``, averaged over three lengths
  as ``100*(4*fast+2*middle+slow)/7``; first print at index ``max(lengths)``
  due to the i+1 shift (bar 0 has no prev close).
- Coppock: ``wma(roc(long)+roc(short), wmaLength)`` on close; warmup is
  ``max(long,short)+wmaLength-1``.
- DPO: ``close - sma(close, period)`` shifted by ``barsback = floor(period/2)+1``;
  centered mode draws at ``i`` what the reference computes at ``i+barsback``.
"""

from __future__ import annotations

from tradex_trading.analytics.indicators import IndicatorSpec, _rolling_sma, _to_float, roc, rsi, sma, wma

__all__ = [
    "SPEC_COPPOCK_CURVE",
    "SPEC_DPO",
    "SPEC_STOCHASTIC_RSI",
    "SPEC_ULTIMATE_OSCILLATOR",
    "SPEC_WILLIAMS_PERCENT_R",
    "coppock_curve",
    "dpo",
    "stochastic_rsi",
    "ultimate_oscillator",
    "williams_percent_r",
]


# ---------------------------------------------------------------------------
# Local helpers mirroring calc.ts strict semantics
# ---------------------------------------------------------------------------


def _rolling_sum(values: list[float], period: int) -> list[float | None]:
    """Reference ``rollingSum`` — None before index period-1, None if any None in window."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    # input lists are finite here; no bad counting needed
    acc = sum(values[:period])
    out[period - 1] = acc
    for i in range(period, n):
        acc += values[i] - values[i - period]
        out[i] = acc
    return out


def _rolling_highest(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        out[i] = max(values[i - period + 1 : i + 1])
    return out


def _rolling_lowest(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        out[i] = min(values[i - period + 1 : i + 1])
    return out


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


def stochastic_rsi(
    candles: list,
    length_rsi: int = 14,
    length_stoch: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> dict[str, list]:
    """Stochastic RSI (openalgo-charts parity).

    RSI over ``length_rsi`` -> stochastic of RSI over ``length_stoch`` ->
    SMA ``smooth_k`` = K -> SMA ``smooth_d`` = D.  Warmup is
    ``(length_rsi-1) + (length_stoch-1) + (smooth_k-1)`` to first K and two
    more for D (14/14/3/3 -> K at 29, D at 31).  Flat RSI window yields None
    (span 0 -> gap, not 50).

    Returns dict keyed ``k`` / ``d``.
    """
    if length_rsi <= 0 or length_stoch <= 0 or smooth_k <= 0 or smooth_d <= 0:
        raise ValueError("lengths must be positive")
    n = len(candles)
    if n == 0:
        return {"k": [], "d": []}
    length_rsi = int(length_rsi)
    length_stoch = int(length_stoch)
    smooth_k = int(smooth_k)
    smooth_d = int(smooth_d)
    closes = [_to_float(c.ohlc.close.value) for c in candles]
    r = rsi(closes, length_rsi)  # None-padded, first at length_rsi
    # stoch of RSI: raw = 100*(r - lowest)/(highest - lowest)
    raw: list[float | None] = [None] * n
    for i in range(length_stoch - 1, n):
        window = r[i - length_stoch + 1 : i + 1]
        if any(v is None for v in window):
            continue
        # window is all finite
        hh = max(window)  # type: ignore[arg-type]
        ll = min(window)  # type: ignore[arg-type]
        span = hh - ll  # type: ignore[operator]
        if span == 0:
            raw[i] = None
        else:
            raw[i] = (r[i] - ll) / span * 100.0  # type: ignore[operator]
    k = _rolling_sma(raw, smooth_k)
    d = _rolling_sma(k, smooth_d)
    return {"k": k, "d": d}


def williams_percent_r(candles: list, length: int = 14) -> dict[str, list]:
    """Williams %R (openalgo-charts parity).

    ``100*(close - highestHigh)/(highestHigh - lowestLow)`` over ``length``.
    0 at fresh window high, -100 at window low, None when range is 0
    (span <=0).  High/low come from bar ``high``/``low``; the numerator reads
    ``close``.

    Returns dict keyed ``percentR`` (alias ``r`` for convenience not used in
    spec).
    """
    if length <= 0:
        raise ValueError("length must be positive")
    n = len(candles)
    if n == 0:
        return {"percentR": []}
    length = int(length)
    highs = [_to_float(c.ohlc.high.value) for c in candles]
    lows = [_to_float(c.ohlc.low.value) for c in candles]
    closes = [_to_float(c.ohlc.close.value) for c in candles]
    hi = _rolling_highest(highs, length)
    lo = _rolling_lowest(lows, length)
    out: list[float | None] = [None] * n
    for i in range(n):
        h = hi[i]
        l_ = lo[i]
        if h is None or l_ is None:
            continue
        span = h - l_
        if span == 0:
            out[i] = None
        else:
            out[i] = 100.0 * (closes[i] - h) / span
    return {"percentR": out}


def ultimate_oscillator(
    candles: list,
    length1: int = 7,
    length2: int = 14,
    length3: int = 28,
) -> dict[str, list]:
    """Ultimate Oscillator (openalgo-charts parity).

    Buying pressure = ``close - min(low, prevClose)`` over true range
    ``max(high, prevClose) - min(low, prevClose)``, averaged as
    ``100*(4*fast+2*middle+slow)/7``.  Bar 0 has no prev close so it
    contributes to neither sum; the i+1 shift means first print is at
    index ``max(lengths)`` (28 on defaults, not 27).  Zero true-range sum
    yields None.

    Returns dict keyed ``uo``.
    """
    if length1 <= 0 or length2 <= 0 or length3 <= 0:
        raise ValueError("lengths must be positive")
    n = len(candles)
    if n == 0:
        return {"uo": []}
    length1 = int(length1)
    length2 = int(length2)
    length3 = int(length3)
    if n == 1:
        return {"uo": [None]}
    m = n - 1
    bp: list[float] = [0.0] * m
    tr: list[float] = [0.0] * m
    for i in range(1, n):
        prev_close = _to_float(candles[i - 1].ohlc.close.value)
        high = _to_float(candles[i].ohlc.high.value)
        low = _to_float(candles[i].ohlc.low.value)
        close = _to_float(candles[i].ohlc.close.value)
        hi = high if high > prev_close else prev_close
        lo = low if low < prev_close else prev_close
        bp[i - 1] = close - lo
        tr[i - 1] = hi - lo

    def _avg(length: int) -> list[float | None]:
        sum_bp = _rolling_sum(bp, length)
        sum_tr = _rolling_sum(tr, length)
        out_m: list[float | None] = [None] * m
        for idx in range(m):
            sb = sum_bp[idx]
            st = sum_tr[idx]
            if sb is None or st is None:
                continue
            if st == 0:
                out_m[idx] = None
            else:
                out_m[idx] = sb / st
        return out_m

    fast = _avg(length1)
    middle = _avg(length2)
    slow = _avg(length3)
    out: list[float | None] = [None] * n
    for i in range(m):
        f = fast[i]
        md = middle[i]
        s = slow[i]
        if f is None or md is None or s is None:
            continue
        out[i + 1] = 100.0 * (4.0 * f + 2.0 * md + s) / 7.0
    return {"uo": out}


def coppock_curve(
    candles: list,
    wma_length: int = 10,
    long_roc_length: int = 14,
    short_roc_length: int = 11,
) -> dict[str, list]:
    """Coppock Curve (openalgo-charts parity).

    ``wma(roc(long)+roc(short), wmaLength)`` on ``close``.  First print at
    ``max(long,short)+wmaLength-1`` (23 on defaults).  Flat base at roc
    yields 0.0 per backend ``roc`` (TS would be NaN — difference is moot
    except for zero-price feeds).

    Returns dict keyed ``curve``.
    """
    if wma_length <= 0 or long_roc_length <= 0 or short_roc_length <= 0:
        raise ValueError("lengths must be positive")
    n = len(candles)
    if n == 0:
        return {"curve": []}
    wma_length = int(wma_length)
    long_roc_length = int(long_roc_length)
    short_roc_length = int(short_roc_length)
    closes = [_to_float(c.ohlc.close.value) for c in candles]
    long_roc: list[float | None] = roc(closes, long_roc_length)  # type: ignore[assignment]
    short_roc: list[float | None] = roc(closes, short_roc_length)  # type: ignore[assignment]
    combined: list[float | None] = [None] * n
    for i in range(n):
        a = long_roc[i]
        b = short_roc[i]
        if a is None or b is None:
            continue
        combined[i] = a + b
    curve = wma(combined, wma_length)
    return {"curve": curve}


def dpo(
    candles: list,
    period: int = 21,
    is_centered: bool = False,
) -> dict[str, list]:
    """Detrended Price Oscillator (openalgo-charts parity).

    ``close - sma(close, period)`` shifted by ``barsback = floor(period/2)+1``.
    Non-centered: ``out[i] = close[i] - sma[i-barsback]`` for ``i >= barsback``.
    Centered: ``out[i] = close[i] - sma[i+barsback]`` for ``i + barsback < n``
    (the line stops ``barsback`` bars short of the right edge).

    Returns dict keyed ``dpo``.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(candles)
    if n == 0:
        return {"dpo": []}
    period = int(period)
    barsback = period // 2 + 1
    closes = [_to_float(c.ohlc.close.value) for c in candles]
    ma = sma(closes, period)
    out: list[float | None] = [None] * n
    if is_centered:
        for i in range(n):
            j = i + barsback
            if j >= n:
                continue
            m = ma[j]
            if m is None:
                continue
            out[i] = closes[i] - m
    else:
        for i in range(barsback, n):
            m = ma[i - barsback]
            if m is None:
                continue
            out[i] = closes[i] - m
    return {"dpo": out}


# ---------------------------------------------------------------------------
# Specs — ready for registry import; NOT registered here (merge task wires them).
# ---------------------------------------------------------------------------


def _fn_stochastic_rsi(candles, length_rsi=14, length_stoch=14, smooth_k=3, smooth_d=3):
    return stochastic_rsi(candles, int(length_rsi), int(length_stoch), int(smooth_k), int(smooth_d))


def _fn_williams(candles, length=14):
    return williams_percent_r(candles, int(length))


def _fn_ultimate(candles, length1=7, length2=14, length3=28):
    return ultimate_oscillator(candles, int(length1), int(length2), int(length3))


def _fn_coppock(candles, wma_length=10, long_roc_length=14, short_roc_length=11):
    return coppock_curve(candles, int(wma_length), int(long_roc_length), int(short_roc_length))


def _fn_dpo(candles, period=21, is_centered=False):
    return dpo(candles, int(period), bool(is_centered))


SPEC_STOCHASTIC_RSI = IndicatorSpec(
    id="stochastic-rsi",
    name="Stochastic RSI",
    category="Momentum",
    placement="pane",
    params=(
        ("length_rsi", "int", 14),
        ("length_stoch", "int", 14),
        ("smooth_k", "int", 3),
        ("smooth_d", "int", 3),
    ),
    plots=(
        ("k", "line", "K"),
        ("d", "line", "D"),
    ),
    levels=(
        {"value": 80},
        {"value": 50},
        {"value": 20},
    ),
    fn=_fn_stochastic_rsi,
)

SPEC_WILLIAMS_PERCENT_R = IndicatorSpec(
    id="williams-percent-r",
    name="Williams Percent Range",
    category="Momentum",
    placement="pane",
    params=(("length", "int", 14),),
    plots=(("percentR", "line", "%R"),),
    levels=(
        {"value": -20},
        {"value": -50},
        {"value": -80},
    ),
    fn=_fn_williams,
)

SPEC_ULTIMATE_OSCILLATOR = IndicatorSpec(
    id="ultimate-oscillator",
    name="Ultimate Oscillator",
    category="Momentum",
    placement="pane",
    params=(
        ("length1", "int", 7),
        ("length2", "int", 14),
        ("length3", "int", 28),
    ),
    plots=(("uo", "line", "UO"),),
    fn=_fn_ultimate,
)

SPEC_COPPOCK_CURVE = IndicatorSpec(
    id="coppock-curve",
    name="Coppock Curve",
    category="Momentum",
    placement="pane",
    params=(
        ("wma_length", "int", 10),
        ("long_roc_length", "int", 14),
        ("short_roc_length", "int", 11),
    ),
    plots=(("curve", "line", "Coppock"),),
    levels=({"value": 0},),
    fn=_fn_coppock,
)

SPEC_DPO = IndicatorSpec(
    id="dpo",
    name="Detrended Price Oscillator",
    category="Momentum",
    placement="pane",
    params=(
        ("period", "int", 21),
        ("is_centered", "bool", False),
    ),
    plots=(("dpo", "line", "DPO"),),
    levels=({"value": 0},),
    fn=_fn_dpo,
)
