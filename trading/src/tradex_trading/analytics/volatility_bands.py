"""Bollinger family + KAMA — volatility bands (openalgo-charts parity).

Batch 3 parallel split: stands alone beside ``indicators.py`` so it can be
developed without merge conflicts. Helpers (``sma``) are imported from
``indicators.py`` and never redefined.

TS sources
- ``src/indicators/volatility.ts`` — BOLLINGER_PERCENT_B, BOLLINGER_BANDWIDTH,
  BB_TREND (``bands()`` helper: ``sma`` + population ``stdev`` ± ``mult``)
- ``src/indicators/adaptive.ts`` — KAMA (Kaufman's Adaptive MA)
- ``src/indicators/calc.ts`` — ``sma``, ``stdev``, ``highest``, ``lowest``,
  ``change``, ``rollingSum``, ``nulls``

Parity notes
- Bands are identical to the reference ``bands()``: population stdev
  (``sqrt(sum((x - sma)^2)/len)``) over the same ``sma`` window.
- ``bollinger_percent_b``: ``(src - lower)/(upper - lower)`` when the span
  is > 0; a collapsed window (flat series, stdev 0) returns ``None``
  matching the TS ``NaN``→``null``. Warmup stays ``None``.
- ``bollinger_bandwidth``: ``((upper - lower)/middle)*100`` per band; the
  two companion rails are rolling extremes *of the bandwidth itself* using a
  NaN-skipped ``highest``/``lowest`` (any ``NaN`` in the window loses the
  comparison, so extremes print as soon as any finite sample enters the
  window, matching the reference comment in volatility.ts). ``middle == 0``
  is treated as ``None`` to avoid division by zero.
- ``bb_trend``: two Bollinger sets (short/long) over the same closes; spread
  is ``abs(short.lower - long.lower) - abs(short.upper - long.upper)``
  normalised by ``short.middle`` as a percentage. ``short.middle == 0``
  yields ``None``.
- ``kama``: direct port of ``KAMA.calc`` in adaptive.ts. Efficiency ratio
  is ``abs(change(erLength))/rollingSum(abs(change(1)), erLength)``;
  ``walked == 0`` maps to ``er = 0`` (maximally inefficient). Smoothing
  constant is ``er*(fastAlpha - slowAlpha)+slowAlpha`` squared. Seeded at
  ``values[erLength]``; first prints at index ``erLength``; ``n <= erLength``
  returns all ``None``.

NOT registered here — the merge task imports the ``SPEC_*`` objects into
``indicators.py``. Specs are ``overlay`` for KAMA and ``pane`` for the
Bollinger readings, matching the TS descriptors.
"""

from __future__ import annotations

import math

from tradex_trading.analytics.indicators import (
    IndicatorSpec,
    _change,
    _rolling_sum,
    _to_float,
    sma,
)


def _extract(candles_or_values: list) -> list[float]:
    """Normalise ``candles`` (objects with ``.ohlc.close.value``) or a plain
    ``list[float|Decimal]`` of closes into ``list[float]``."""
    if not candles_or_values:
        return []
    first = candles_or_values[0]
    # duck-type candle detection
    if hasattr(first, "ohlc"):
        return [_to_float(c.ohlc.close.value) for c in candles_or_values]  # type: ignore[union-attr]
    # plain numeric list (Decimal/float/int)
    return [_to_float(v) for v in candles_or_values]  # type: ignore[arg-type]


def _bands(values: list[float], length: int, mult: float):
    """Shared Bollinger construction: ``sma`` middle and population-stdev rails."""
    middle = sma(values, length)
    n = len(values)
    upper: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    for i in range(length - 1, n):
        m = middle[i]
        if m is None:
            continue
        window = values[i - length + 1 : i + 1]
        # population variance
        var = sum((x - m) ** 2 for x in window) / length
        sd = math.sqrt(var)
        off = mult * sd
        upper[i] = m + off
        lower[i] = m - off
    return middle, upper, lower


def _highest_skip(values: list[float | None], period: int) -> list[float | None]:
    """Rolling maximum that skips ``None`` (TS ``highest`` over a series holding
    ``NaN`` — a ``NaN`` loses every comparison). Returns ``None`` where no
    finite value is in the window."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < 1:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        finite = [v for v in window if v is not None and math.isfinite(v)]  # type: ignore[arg-type]
        if finite:
            out[i] = max(finite)  # type: ignore[type-var]
        # else stays None (TS +Infinity -> nulls -> None)
    return out


def _lowest_skip(values: list[float | None], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < 1:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        finite = [v for v in window if v is not None and math.isfinite(v)]  # type: ignore[arg-type]
        if finite:
            out[i] = min(finite)  # type: ignore[type-var]
    return out


# ---------------------------------------------------------------------------
# Bollinger %b
# ---------------------------------------------------------------------------


def bollinger_percent_b(
    candles: list, length: int = 20, mult: float = 2.0
) -> list[float | None]:
    """Bollinger Bands %b — position of price inside its own bands.

    Matches ``BOLLINGER_PERCENT_B.calc`` in ``src/indicators/volatility.ts``:
    ``(src - lower)/(upper - lower)`` when ``span > 0``. A flat window
    (``span == 0``) yields ``None`` (TS ``NaN``→``null``). ``None`` during
    ``length - 1`` warmup.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    values = _extract(candles)
    n = len(values)
    out: list[float | None] = [None] * n
    if n < length:
        return out
    mult = float(mult)
    _middle, upper, lower = _bands(values, length, mult)
    for i in range(n):
        lo = lower[i]
        up = upper[i]
        if lo is None or up is None:
            continue
        span = up - lo
        if span > 0:
            out[i] = (values[i] - lo) / span
        # else span == 0 (TS: NaN → null via nulls())
        # else span is nan/inf should not happen; stays None
    return out


# ---------------------------------------------------------------------------
# Bollinger Bandwidth
# ---------------------------------------------------------------------------


def bollinger_bandwidth(
    candles: list,
    length: int = 20,
    mult: float = 2.0,
    expansion_length: int = 125,
    contraction_length: int = 125,
) -> dict[str, list]:
    """Bollinger BandWidth — band spread as percent of basis plus rails.

    Matches ``BOLLINGER_BANDWIDTH.calc`` in ``src/indicators/volatility.ts``:
    ``bandwidth = ((upper - lower)/middle)*100`` (``None`` when
    ``middle == 0`` or warmup); ``expansion`` is the rolling maximum of
    ``bandwidth`` over ``expansion_length`` and ``contraction`` the rolling
    minimum over ``contraction_length``, both skipping ``None`` (TS
    ``highest``/``lowest`` over ``NaN``).
    """
    if length <= 0 or expansion_length <= 0 or contraction_length <= 0:
        raise ValueError("length must be positive")
    values = _extract(candles)
    n = len(values)
    if n == 0:
        return {"bandwidth": [], "expansion": [], "contraction": []}
    mult = float(mult)
    middle, upper, lower = _bands(values, length, mult)
    bbw: list[float | None] = [None] * n
    for i in range(n):
        m = middle[i]
        up = upper[i]
        lo = lower[i]
        if m is None or up is None or lo is None:
            continue
        if m == 0:
            continue
        bbw[i] = ((up - lo) / m) * 100.0
    expansion = _highest_skip(bbw, int(expansion_length))
    contraction = _lowest_skip(bbw, int(contraction_length))
    return {"bandwidth": bbw, "expansion": expansion, "contraction": contraction}


# ---------------------------------------------------------------------------
# BBTrend
# ---------------------------------------------------------------------------


def bb_trend(
    candles: list,
    short_length: int = 20,
    long_length: int = 50,
    std_dev_mult: float = 2.0,
) -> list[float | None]:
    """BBTrend — short vs long Bollinger spread normalised by short basis.

    Matches ``BB_TREND.calc`` in ``src/indicators/volatility.ts``:
    ``spread = abs(shortLower - longLower) - abs(shortUpper - longUpper)``
    ``bbtrend = (spread / shortMiddle)*100``; ``None`` when ``shortMiddle``
    is ``None`` or zero. First prints when *both* band legs are live
    (``max(short, long) - 1``).
    """
    if short_length <= 0 or long_length <= 0:
        raise ValueError("length must be positive")
    values = _extract(candles)
    n = len(values)
    out: list[float | None] = [None] * n
    if n == 0:
        return out
    mult = float(std_dev_mult)
    s_mid, s_up, s_lo = _bands(values, int(short_length), mult)
    l_mid, l_up, l_lo = _bands(values, int(long_length), mult)
    for i in range(n):
        sm = s_mid[i]
        su = s_up[i]
        sl = s_lo[i]
        lu = l_up[i]
        ll = l_lo[i]
        if sm is None or su is None or sl is None or lu is None or ll is None:
            continue
        if sm == 0:
            continue
        spread = abs(sl - ll) - abs(su - lu)
        out[i] = (spread / sm) * 100.0
    return out


# ---------------------------------------------------------------------------
# KAMA — Kaufman's Adaptive Moving Average
# ---------------------------------------------------------------------------


def kama(
    candles: list,
    er_length: int = 10,
    fast_length: int = 2,
    slow_length: int = 30,
) -> list[float | None]:
    """Kaufman's Adaptive Moving Average.

    Direct port of ``KAMA.calc`` in ``src/indicators/adaptive.ts``: ER is
    ``abs(change(erLength))/rollingSum(abs(change(1)), erLength)`` with
    ``walked == 0`` -> ``er = 0``; alphas are ``2/(len+1)``; smoothing is
    ``prev += alpha^2 * (src - prev)`` seeded at ``values[erLength]``.
    Returns ``None`` before ``erLength`` and all ``None`` when
    ``n <= erLength``.
    """
    if er_length <= 0 or fast_length <= 0 or slow_length <= 0:
        raise ValueError("length must be positive")
    values = _extract(candles)
    n = len(values)
    out: list[float | None] = [None] * n
    if n <= er_length:
        return out
    # steps = abs(change(values, 1)) with NaN -> 0 for bar 0
    ch1 = _change(values, 1)
    steps: list[float] = [0.0] * n
    for i in range(n):
        v = ch1[i]
        steps[i] = abs(v) if v is not None else 0.0
    path = _rolling_sum(steps, int(er_length))
    travel = _change(values, int(er_length))
    fast_alpha = 2.0 / (int(fast_length) + 1)
    slow_alpha = 2.0 / (int(slow_length) + 1)
    prev = values[int(er_length)]
    out[int(er_length)] = prev
    for i in range(int(er_length) + 1, n):
        walked = path[i]
        tv = travel[i]
        if walked is None or walked == 0 or tv is None:
            er = 0.0
        else:
            er = abs(tv) / walked if math.isfinite(walked) and walked != 0 else 0.0
        # clamp er into [0,1] defensively (TS divides and may exceed due to fp)
        if er < 0:
            er = 0.0
        elif er > 1:
            er = 1.0
        alpha = er * (fast_alpha - slow_alpha) + slow_alpha
        prev = prev + alpha * alpha * (values[i] - prev)
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# Specs — ready for registry import; NOT registered here (merge task wires).
# ---------------------------------------------------------------------------


def _fn_bollinger_percent_b(candles, length, mult):
    return bollinger_percent_b(candles, int(length), float(mult))


def _fn_bollinger_bandwidth(candles, length, mult, expansion_length, contraction_length):
    return bollinger_bandwidth(
        candles, int(length), float(mult), int(expansion_length), int(contraction_length)
    )


def _fn_bb_trend(candles, short_length, long_length, std_dev_mult):
    return bb_trend(candles, int(short_length), int(long_length), float(std_dev_mult))


def _fn_kama(candles, er_length, fast_length, slow_length):
    return kama(candles, int(er_length), int(fast_length), int(slow_length))


SPEC_BOLLINGER_PERCENT_B = IndicatorSpec(
    id="bollinger-percent-b",
    name="Bollinger Bands %b",
    category="Volatility",
    placement="pane",
    params=(
        ("length", "int", 20),
        ("mult", "float", 2.0),
    ),
    plots=(("value", "line", "Bollinger Bands %b"),),
    levels=({"value": 1}, {"value": 0.5}, {"value": 0}),
    fn=_fn_bollinger_percent_b,
)

SPEC_BOLLINGER_BANDWIDTH = IndicatorSpec(
    id="bollinger-bandwidth",
    name="Bollinger BandWidth",
    category="Volatility",
    placement="pane",
    params=(
        ("length", "int", 20),
        ("mult", "float", 2.0),
        ("expansion_length", "int", 125),
        ("contraction_length", "int", 125),
    ),
    plots=(
        ("bandwidth", "line", "Bollinger BandWidth"),
        ("expansion", "line", "Highest Expansion"),
        ("contraction", "line", "Lowest Contraction"),
    ),
    fn=_fn_bollinger_bandwidth,
)

SPEC_BB_TREND = IndicatorSpec(
    id="bb-trend",
    name="BBTrend",
    category="Volatility",
    placement="pane",
    params=(
        ("short_length", "int", 20),
        ("long_length", "int", 50),
        ("std_dev_mult", "float", 2.0),
    ),
    plots=(("value", "line", "BBTrend"),),
    levels=({"value": 0},),
    fn=_fn_bb_trend,
)

SPEC_KAMA = IndicatorSpec(
    id="kama",
    name="Kaufman's Adaptive Moving Average",
    category="Trend",
    placement="overlay",
    params=(
        ("er_length", "int", 10),
        ("fast_length", "int", 2),
        ("slow_length", "int", 30),
    ),
    plots=(("value", "line", "KAMA"),),
    fn=_fn_kama,
)

__all__ = [
    "bb_trend",
    "bollinger_bandwidth",
    "bollinger_percent_b",
    "kama",
    "SPEC_BB_TREND",
    "SPEC_BOLLINGER_BANDWIDTH",
    "SPEC_BOLLINGER_PERCENT_B",
    "SPEC_KAMA",
]
