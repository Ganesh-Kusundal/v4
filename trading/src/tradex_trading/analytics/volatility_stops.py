"""ATR trailing stops — Volatility Stop, Chandelier Exit, Chande-Kroll Stop.

Batch 3 parallel split: standalone module so indicator-port tasks can run
concurrently without touching ``indicators.py``. Helpers are imported from
``indicators.py`` and never redefined here.

Parity notes:
- Volatility Stop (src/indicators/signals.ts): trailing stop an ATR multiple
  away from the running extreme (max/min of source), ratcheted toward price
  (max(stop, max-atr) / min(stop, min+atr)) and flipping when close crosses
  it. While ATR is warming the band falls back to the bar's own true range,
  unmultiplied, so the stop exists from bar 0. Flip resets extremes.
- Chandelier Exit (src/indicators/overlay.ts): highest high - mult*ATR and
  lowest low + mult*ATR. No ratchet beyond the rolling extreme.
- Chande Kroll Stop (src/indicators/overlay.ts): two stacked extremes:
  firstHighStop = highest(high,p)-x*ATR(p), firstLowStop = lowest(low,p)+x*ATR(p),
  then stopShort = highest(firstHighStop,q) and stopLong = lowest(firstLowStop,q)
  via NaN-strict extremes (any None in window -> None), first prints at p+q-2.
"""

from __future__ import annotations

import math
from typing import Any

from tradex_trading.analytics.indicators import IndicatorSpec, _to_float, atr, true_ranges

__all__ = [
    "SPEC_CHANDE_KROLL_STOP",
    "SPEC_CHANDELIER_EXIT",
    "SPEC_VOLATILITY_STOP",
    "chande_kroll_stop",
    "chandelier_exit",
    "volatility_stop",
]


def _source_values(candles: list, source: str) -> list[float]:
    src = (source or "close").lower()
    out: list[float] = []
    for c in candles:
        o = _to_float(c.ohlc.open.value)
        h = _to_float(c.ohlc.high.value)
        lo = _to_float(c.ohlc.low.value)
        cl = _to_float(c.ohlc.close.value)
        if src == "open":
            out.append(o)
        elif src == "high":
            out.append(h)
        elif src == "low":
            out.append(lo)
        elif src == "hl2":
            out.append((h + lo) / 2.0)
        elif src == "hlc3":
            out.append((h + lo + cl) / 3.0)
        elif src == "ohlc4":
            out.append((o + h + lo + cl) / 4.0)
        else:
            out.append(cl)
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


def _extreme_strict(values: list[float | None], period: int, want_high: bool) -> list[float | None]:
    """NaN-strict rolling extreme (openalgo-charts extremeStrict parity).

    Any None/NaN in the window -> None at that index. Otherwise max/min.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        live = True
        best = -math.inf if want_high else math.inf
        for v in window:
            if v is None or not math.isfinite(v):
                live = False
                break
            if want_high:
                if v > best:
                    best = v
            else:
                if v < best:
                    best = v
        if live:
            out[i] = best
    return out


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


def volatility_stop(
    candles: list,
    length: int = 20,
    factor: float = 2.0,
    source: str = "close",
) -> dict[str, list]:
    """Volatility Stop (openalgo-charts parity).

    ATR-trailing stop with ratcheting and flip-reset. While ATR is warming
    the offset falls back to the bar's own true range (unmultiplied).
    Returns dict keyed 'up' / 'down' (only one non-None per bar).

    Args:
        candles: domain candles
        length: ATR period (default 20, min 2 per TS)
        factor: ATR multiplier (default 2)
        source: price source for the running extreme (default close)
    """
    if length <= 0:
        raise ValueError("length must be positive")
    n = len(candles)
    up: list[float | None] = [None] * n
    down: list[float | None] = [None] * n
    if n == 0:
        return {"up": up, "down": down}
    values = _source_values(candles, str(source))
    highs = [_to_float(c.ohlc.high.value) for c in candles]
    lows = [_to_float(c.ohlc.low.value) for c in candles]
    # atr uses candles directly; true_ranges for fallback
    band = atr(candles, int(length))
    tr = true_ranges(candles)

    max_v: float = values[0]
    min_v: float = values[0]
    uptrend: bool = True
    stop: float = float("nan")

    for i in range(n):
        v = values[i]
        atr_val = band[i]
        atr_m = (atr_val * float(factor)) if atr_val is not None else float(tr[i]) if i < len(tr) else float("nan")
        # ratchet toward price
        max_v = max(max_v, v)
        min_v = min(min_v, v)
        if math.isfinite(stop):
            level = max(stop, max_v - atr_m) if uptrend else min(stop, min_v + atr_m)
        else:
            level = float("nan")
        if not math.isfinite(level):
            level = v
        now_up = (v - level) >= 0
        if i > 0 and now_up != uptrend:
            max_v = v
            min_v = v
            level = (max_v - atr_m) if now_up else (min_v + atr_m)
        uptrend = now_up
        stop = level
        if not math.isfinite(level):
            continue
        if now_up:
            up[i] = level
        else:
            down[i] = level
    return {"up": up, "down": down}


def chandelier_exit(
    candles: list,
    length: int = 22,
    atr_length: int = 22,
    atr_multiplier: float = 3.0,
) -> dict[str, list]:
    """Chandelier Exit (openalgo-charts parity).

    long = highest(high,length) - mult*ATR(atr_length)
    short = lowest(low,length) + mult*ATR(atr_length)

    Returns dict keyed 'long' / 'short' (also aliased as longExit/shortExit
    for TS key parity).
    """
    if length <= 0 or atr_length <= 0:
        raise ValueError("length must be positive")
    n = len(candles)
    if n == 0:
        return {"long": [], "short": [], "longExit": [], "shortExit": []}
    length = int(length)
    atr_length = int(atr_length)
    mult = float(atr_multiplier)
    highs = [_to_float(c.ohlc.high.value) for c in candles]
    lows = [_to_float(c.ohlc.low.value) for c in candles]
    atr_vals = atr(candles, atr_length)
    hh = _rolling_highest(highs, length)
    ll = _rolling_lowest(lows, length)
    long_exit: list[float | None] = [None] * n
    short_exit: list[float | None] = [None] * n
    for i in range(n):
        h = hh[i]
        lo = ll[i]
        r = atr_vals[i]
        if h is not None and r is not None:
            long_exit[i] = h - mult * r
        if lo is not None and r is not None:
            short_exit[i] = lo + mult * r
    return {"long": long_exit, "short": short_exit, "longExit": long_exit, "shortExit": short_exit}


def chande_kroll_stop(
    candles: list,
    p: int = 10,
    x: int = 1,
    q: int = 9,
) -> dict[str, list]:
    """Chande Kroll Stop (openalgo-charts parity).

    firstHigh = highest(high,p) - x*ATR(p)
    firstLow  = lowest(low,p)  + x*ATR(p)
    stopShort = highest(firstHigh,q) strict
    stopLong  = lowest(firstLow,q)  strict

    Returns dict keyed 'stopLong' / 'stopShort' (also short aliases 'long'/'short').
    """
    if p <= 0 or q <= 0:
        raise ValueError("lengths must be positive")
    n = len(candles)
    if n == 0:
        return {"stopLong": [], "stopShort": [], "long": [], "short": []}
    p = int(p)
    q = int(q)
    xv = int(x)
    highs = [_to_float(c.ohlc.high.value) for c in candles]
    lows = [_to_float(c.ohlc.low.value) for c in candles]
    atr_vals = atr(candles, p)
    hh_p = _rolling_highest(highs, p)
    ll_p = _rolling_lowest(lows, p)
    first_high: list[float | None] = [None] * n
    first_low: list[float | None] = [None] * n
    for i in range(n):
        h = hh_p[i]
        lo = ll_p[i]
        r = atr_vals[i]
        if h is not None and r is not None:
            first_high[i] = h - xv * r
        if lo is not None and r is not None:
            first_low[i] = lo + xv * r
    stop_long = _extreme_strict(first_low, q, False)
    stop_short = _extreme_strict(first_high, q, True)
    return {"stopLong": stop_long, "stopShort": stop_short, "long": stop_long, "short": stop_short}


# ---------------------------------------------------------------------------
# Specs — ready for registry import; NOT registered here (merge task wires them).
# ---------------------------------------------------------------------------


def _fn_volatility_stop(candles, length=20, factor=2, source="close"):
    return volatility_stop(candles, int(length), float(factor), str(source))


def _fn_chandelier(candles, length=22, atr_length=22, atr_multiplier=3):
    return chandelier_exit(candles, int(length), int(atr_length), float(atr_multiplier))


def _fn_chande_kroll(candles, p=10, x=1, q=9):
    return chande_kroll_stop(candles, int(p), int(x), int(q))


SPEC_VOLATILITY_STOP = IndicatorSpec(
    id="volatility-stop",
    name="Volatility Stop",
    category="Trend",
    placement="overlay",
    params=(
        ("length", "int", 20),
        ("factor", "float", 2.0),
        ("source", "string", "close"),
    ),
    plots=(
        ("up", "line", "Up Trend"),
        ("down", "line", "Down Trend"),
    ),
    fn=_fn_volatility_stop,
)

SPEC_CHANDELIER_EXIT = IndicatorSpec(
    id="chandelier-exit",
    name="Chandelier Exit",
    category="Trend",
    placement="overlay",
    params=(
        ("length", "int", 22),
        ("atr_length", "int", 22),
        ("atr_multiplier", "float", 3.0),
    ),
    plots=(
        ("long", "line", "Long"),
        ("short", "line", "Short"),
        ("longExit", "line", "Long Exit"),
        ("shortExit", "line", "Short Exit"),
    ),
    fn=_fn_chandelier,
)

SPEC_CHANDE_KROLL_STOP = IndicatorSpec(
    id="chande-kroll-stop",
    name="Chande Kroll Stop",
    category="Trend",
    placement="overlay",
    params=(
        ("p", "int", 10),
        ("x", "int", 1),
        ("q", "int", 9),
    ),
    plots=(
        ("stopLong", "line", "Stop Long"),
        ("stopShort", "line", "Stop Short"),
    ),
    fn=_fn_chande_kroll,
)
