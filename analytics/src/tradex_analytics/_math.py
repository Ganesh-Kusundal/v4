"""Shared math helpers for the analytics indicator modules."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any, cast

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


def _closes(candles: list) -> list[float]:
    return [_to_float(c.ohlc.close.value) for c in candles]


def _highs(candles: list) -> list[float]:
    return [_to_float(c.ohlc.high.value) for c in candles]


def _lows(candles: list) -> list[float]:
    return [_to_float(c.ohlc.low.value) for c in candles]


def _f(candle: Any, key: str) -> float:
    """Bar OHLC field as a float, for dict candles or ``.ohlc.*.value`` objects."""
    if isinstance(candle, dict):
        return _to_float(candle[key])
    return _to_float(getattr(candle.ohlc, key).value)


def _vol(candle: Any) -> float:
    """Reference ``nz(volume)``: a bar with no volume traded nothing."""
    if isinstance(candle, dict):
        v = candle.get("volume")
    else:
        v = candle.volume.value
    if v is None:
        return 0.0
    fv = float(v)
    return fv if _isfinite(fv) else 0.0


def _extract(
    candles: list,
) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    """(highs, lows, closes, opens, volumes) as float arrays."""
    highs = [_f(c, "high") for c in candles]
    lows = [_f(c, "low") for c in candles]
    closes = [_f(c, "close") for c in candles]
    opens = [_f(c, "open") for c in candles]
    vols = [_vol(c) for c in candles]
    return highs, lows, closes, opens, vols


def _source_values(candles: list, source: str) -> list[float]:
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


def _highest(values: Sequence[float | None], period: int) -> list[float | None]:
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


def _lowest(values: Sequence[float | None], period: int) -> list[float | None]:
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


def _shift(values: Sequence[float | None], k: int) -> list[float | None]:
    """Displace by k bars: positive draws each value k bars later (TS shift)."""
    n = len(values)
    out: list[float | None] = [None] * n
    for i in range(n):
        j = i + k
        if 0 <= j < n:
            out[j] = values[i]
    return out


def _rma(values: Sequence[float | None], period: int) -> list[float | None]:
    """Wilder RMA: SMA-seeded, then (prev*(period-1)+v)/period. None before period-1."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    prev = sum(cast(float, v) for v in values[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        v = values[i]
        if v is None:
            continue
        prev = (prev * (period - 1) + v) / period
        out[i] = prev
    return out


def _sma_skip_none(values: Sequence[float | None], period: int) -> list[float | None]:
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
        out[i] = (sum((cast(float, x) - m) ** 2 for x in window) / period) ** 0.5
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


def _seeded_ema(values: list[float | None], period: int) -> list[float | None]:
    """ranges.ts ``seededEma``: the reference EMA with a NaN-strict SMA seed."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0:
        return out
    seed = _sma_skip_none(values, period)
    k = 2.0 / (period + 1)
    prev: float | None = None
    for i in range(n):
        if prev is None:
            prev = seed[i]
        else:
            v = values[i]
            prev = v * k + prev * (1.0 - k) if v is not None else None
        out[i] = prev
    return out


def _rolling_sum(values: Sequence[float | None], period: int) -> list[float | None]:
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


def _vwma(
    values: Sequence[float | None], vols: Sequence[float | None], period: int
) -> list[float | None]:
    """calc.ts ``vwma``: ``sma(src*vol, len) / sma(vol, len)``."""
    n = len(values)
    pv = [cast(float, values[i]) * cast(float, vols[i]) for i in range(n)]
    num = sma(list(pv), period)
    den = sma(list(vols), period)
    out: list[float | None] = [None] * n
    for i in range(n):
        d = den[i]
        if d is not None and d != 0 and num[i] is not None:
            out[i] = num[i] / d
    return out
