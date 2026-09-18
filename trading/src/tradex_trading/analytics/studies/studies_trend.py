"""Complex trend studies — openalgo-charts parity (averages.ts / trend.ts / studies.ts).

Batch 5 parallel split: the five heavy trend studies ported from the reference
TS descriptors. Helpers (``sma``, ``_highest``, ``_rolling_sum``, ``_isfinite``,
``_to_float``) are imported from ``indicators.py`` and never redefined here.

TS sources
- ``src/indicators/averages.ts`` — ALLIGATOR (``rma`` + ``shift``)
- ``src/indicators/trend.ts`` — PARABOLIC_SAR, ICHIMOKU, HALFTREND
- ``src/indicators/studies.ts`` — ALPHATREND (``moneyFlowIndex``)

Parity notes
- ``_rma`` matches calc.ts ``rma`` (Wilder): seed with the SMA of the first
  ``period`` values at index ``period - 1``, then ``(prev*(period-1)+v)/period``.
- ``_shift`` matches the per-plot offset semantics: a positive ``k`` draws a
  value computed on bar ``i`` in slot ``i + k`` (``out[i] = values[i - k]``),
  leaving ``k`` leading None slots and dropping the shifted tail.
- ``_atr_arrays`` matches the base bundle ``atr`` (atr.ts): Wilder ATR seeded
  with the SMA of TR[0..period-1]; TR[0] = high-low.
- ``_money_flow_index`` matches ``moneyFlowIndex`` in studies.ts: first value
  lands at index ``period``; a window with no down-flow pins at 100.
- ``_bars_since`` matches calc.ts ``barsSince``: bars since the condition was
  last true (0 on the true bar), None before the first true.
"""

from __future__ import annotations

from typing import cast, Any

from tradex_trading.analytics.indicators import (
    IndicatorSpec,
    LEGACY_PARAM_ALIASES,
    _bars_since,
    _highest,
    _isfinite,
    _lowest,
    _rma,
    _rolling_sum,
    _shift,
    _to_float,
    sma,
)

LEGACY_PARAM_ALIASES["alligator"] = {
    "jaw_length": "jawLength",
    "teeth_length": "teethLength",
    "lips_length": "lipsLength",
    "jaw_offset": "jawOffset",
    "teeth_offset": "teethOffset",
    "lips_offset": "lipsOffset",
}
LEGACY_PARAM_ALIASES["ichimoku"] = {
    "conversion": "conversionPeriod",
    "base": "basePeriod",
    "lagging": "laggingSpanPeriod",
}
LEGACY_PARAM_ALIASES["halftrend"] = {
    "channel_deviation": "channelDeviation",
    "atr_period": "atrPeriod",
}
LEGACY_PARAM_ALIASES["alphatrend"] = {"ap": "AP"}

# ---------------------------------------------------------------------------
# Candle access — the golden harness feeds candle objects (``.ohlc.*.value``);
# the documented backend contract feeds dicts (``{"open", "high", ...}``).
# Both are accepted so the functions are drop-in under either convention.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """True range per bar; TR[0] = high-low (atr.ts)."""
    n = len(highs)
    tr: list[float] = [0.0] * n
    if n == 0:
        return tr
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr[i] = max(hl, hc, lc)
    return tr


def _atr_arrays(
    highs: list[float], lows: list[float], closes: list[float], period: int
) -> list[float | None]:
    """Wilder ATR over arrays; first value at ``period - 1`` (atr.ts)."""
    n = len(highs)
    tr = _true_ranges(highs, lows, closes)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def _money_flow_index(typical: list[float], vols: list[float], period: int) -> list[float | None]:
    """Money Flow Index over an explicit typical-price series (studies.ts).

    First value lands at index ``period``; a window with no down-flow pins at 100.
    """
    n = len(typical)
    out: list[float | None] = [None] * n
    if period <= 0 or n == 0:
        return out
    positive = [0.0] * n
    negative = [0.0] * n
    for i in range(1, n):
        flow = typical[i] * vols[i]
        if typical[i] > typical[i - 1]:
            positive[i] = flow
        elif typical[i] < typical[i - 1]:
            negative[i] = flow
    up = _rolling_sum(positive, period)
    down = _rolling_sum(negative, period)
    for i in range(period, n):
        d = down[i]
        u = up[i]
        if d is None or u is None:
            continue
        out[i] = 100.0 if d == 0 else 100.0 - 100.0 / (1.0 + u / d)
    return out


# ---------------------------------------------------------------------------
# Alligator
# ---------------------------------------------------------------------------


def alligator(
    candles: list,
    jaw_length: int = 13,
    jaw_offset: int = 8,
    teeth_length: int = 8,
    teeth_offset: int = 5,
    lips_length: int = 5,
    lips_offset: int = 3,
) -> dict[str, list]:
    """Williams Alligator — three Wilder-smoothed medians of ``hl2``, each
    displaced forward by its offset.

    Matches ``ALLIGATOR.calc`` in averages.ts: ``rma(hl2, n)`` shifted by the
    plot offset (value on bar ``i`` lands in slot ``i + offset``). First prints
    at ``length - 1 + offset``.
    """
    highs, lows, _c, _o, _v = _extract(candles)
    hl2 = [(h + lo) / 2.0 for h, lo in zip(highs, lows, strict=True)]
    return {
        "jaw": _shift(_rma(hl2, int(jaw_length)), int(jaw_offset)),
        "teeth": _shift(_rma(hl2, int(teeth_length)), int(teeth_offset)),
        "lips": _shift(_rma(hl2, int(lips_length)), int(lips_offset)),
    }


# ---------------------------------------------------------------------------
# Parabolic SAR
# ---------------------------------------------------------------------------


def parabolic_sar(
    candles: list,
    start: float = 0.02,
    increment: float = 0.02,
    maximum: float = 0.2,
) -> dict[str, list]:
    """Parabolic SAR — the classic EP/AF state machine.

    Matches ``PARABOLIC_SAR.calc`` in trend.ts: trend seeded from closes
    0/1, SAR/EP seeded from bars 0/1 extremes with the seed bar carrying the
    seed itself (``out[1]`` unaccelerated, ``out[0]`` gapped). Per bar:
    propagate first, test reversal on the unclamped stop, update EP/AF, then
    clamp against the prior two bars' range (clamp also applies to reversal
    stops via ``max(ep, high)`` / ``min(ep, low)`` with the current bar).
    """
    n = len(candles)
    out: list[float | None] = [None] * n
    if n < 2:
        return {"sar": out}
    highs, lows, closes, _o, _v = _extract(candles)
    step = float(start)
    inc = float(increment)
    max_ = float(maximum)

    rising = closes[1] >= closes[0]
    sar = lows[0] if rising else highs[0]
    ep = highs[1] if rising else lows[1]
    af = step
    out[1] = sar

    for i in range(2, n):
        sar += af * (ep - sar)
        if rising and lows[i] < sar:
            rising = False
            sar = max(ep, highs[i])
            ep = lows[i]
            af = step
        elif not rising and highs[i] > sar:
            rising = True
            sar = min(ep, lows[i])
            ep = highs[i]
            af = step
        elif rising and highs[i] > ep:
            ep = highs[i]
            af = min(max_, af + inc)
        elif not rising and lows[i] < ep:
            ep = lows[i]
            af = min(max_, af + inc)
        if rising:
            sar = min(sar, lows[i - 1], lows[i - 2])
        else:
            sar = max(sar, highs[i - 1], highs[i - 2])
        out[i] = sar
    return {"sar": out}


# ---------------------------------------------------------------------------
# Ichimoku Cloud
# ---------------------------------------------------------------------------


def ichimoku(
    candles: list,
    conversion: int = 9,
    base: int = 26,
    lagging: int = 52,
    displacement: int = 26,
) -> dict[str, list]:
    """Ichimoku Cloud — Donchian midpoints plus the displaced spans.

    Matches ``ICHIMOKU.calc`` in trend.ts: conversion/base are ``(highest high
    + lowest low)/2`` over their periods; spanA is the mean of the two (None
    until both are live) and spanB the ``lagging`` midpoint, both displaced
    forward by ``displacement``; the lagging plot is ``close`` shifted back by
    ``displacement`` (trailing ``displacement`` slots are None).
    """
    n = len(candles)
    highs, lows, closes, _o, _v = _extract(candles)

    def mid(p: int) -> list[float | None]:
        out: list[float | None] = [None] * n
        for i in range(p - 1, n):
            hi = max(highs[i - p + 1 : i + 1])
            lo = min(lows[i - p + 1 : i + 1])
            out[i] = (hi + lo) / 2.0
        return out

    conv = int(conversion)
    base_p = int(base)
    lag = int(lagging)
    disp = int(displacement)

    conversion_series = mid(conv)
    base_series = mid(base_p)
    span_a = [
        None if a is None or b is None else (a + b) / 2.0
        for a, b in zip(conversion_series, base_series, strict=True)
    ]
    span_b = mid(lag)
    return {
        "conversion": conversion_series,
        "base": base_series,
        "spanA": _shift(span_a, disp),
        "spanB": _shift(span_b, disp),
        "lagging": _shift(closes, -disp),
    }


# ---------------------------------------------------------------------------
# HalfTrend
# ---------------------------------------------------------------------------


def halftrend(
    candles: list,
    amplitude: int = 2,
    channel_deviation: float = 2,
    atr_period: int = 100,
) -> dict[str, list]:
    """HalfTrend — a trend level that only moves once the opposing range gives
    way, riding half-ATR channels with flip markers.

    Matches ``HALFTREND.calc`` in trend.ts: two state machines track ``trend``
    and the armed ``nextTrend``; ``meanHigh``/``meanLow`` cross a running
    ``maxLow``/``minHigh`` extreme and a close beyond the prior bar's extreme to
    flip. On a flip the new level steps from where the other side ended, and the
    flip bar is marked half an ATR inside the channel. Channels (deviation) and
    signals are shown (showChannels/showSignals default true).

    Plots: ``up``/``down`` carry the level while their side is live (null on
    the other); ``atr_high``/``atr_low`` the channel edges (None until ATR
    warmup); ``buy_signal``/``sell_signal`` the flip markers.
    """
    n = len(candles)
    up: list[float | None] = [None] * n
    down: list[float | None] = [None] * n
    ch_high: list[float | None] = [None] * n
    ch_low: list[float | None] = [None] * n
    buy: list[float | None] = [None] * n
    sell: list[float | None] = [None] * n
    if n == 0:
        return {
            "up": up,
            "down": down,
            "atr_high": ch_high,
            "atr_low": ch_low,
            "buy_signal": buy,
            "sell_signal": sell,
        }

    amp = max(1, round(float(amplitude)))
    ch_dev = float(channel_deviation)

    highs, lows, closes, _o, _v = _extract(candles)
    half_atr = _atr_arrays(highs, lows, closes, max(1, round(float(atr_period))))
    mean_high = sma(highs, amp)
    mean_low = sma(lows, amp)
    roll_high = _highest(highs, amp)
    roll_low = _lowest(lows, amp)

    trend = 0
    armed = 0
    max_low = lows[0]
    min_high = highs[0]
    up_level = 0.0
    down_level = 0.0
    seeded = False

    for i in range(n):
        half = cast(float, half_atr[i]) / 2.0 if half_atr[i] is not None else None
        dev = ch_dev * half if half is not None else None
        bar_high = roll_high[i]
        bar_low = roll_low[i]
        prev_high = highs[i - 1] if i > 0 else highs[0]
        prev_low = lows[i - 1] if i > 0 else lows[0]
        was_trend = trend if seeded else -1

        if armed == 1:
            if bar_low is not None:
                max_low = max(bar_low, max_low)
            if (
                mean_high[i] is not None
                and mean_high[i] < max_low
                and closes[i] < prev_low
            ):
                trend = 1
                armed = 0
                min_high = bar_high  # type: ignore[assignment]
        else:
            if bar_high is not None:
                min_high = min(bar_high, min_high)
            if (
                mean_low[i] is not None
                and mean_low[i] > min_high
                and closes[i] > prev_high
            ):
                trend = 0
                armed = 1
                max_low = bar_low  # type: ignore[assignment]

        if trend == 0:
            if was_trend == 1:
                up_level = down_level
                if half is not None:
                    buy[i] = up_level - half
            else:
                up_level = max_low if was_trend == -1 else max(max_low, up_level)
            level = up_level
        else:
            if was_trend == 0:
                down_level = up_level
                if half is not None:
                    sell[i] = down_level + half
            else:
                down_level = min_high if was_trend == -1 else min(min_high, down_level)
            level = down_level
        seeded = True

        if not _isfinite(level):
            continue
        if trend == 0:
            up[i] = level
        else:
            down[i] = level
        if dev is not None:
            ch_high[i] = level + dev
            ch_low[i] = level - dev

    return {
        "up": up,
        "down": down,
        "atr_high": ch_high,
        "atr_low": ch_low,
        "buy_signal": buy,
        "sell_signal": sell,
    }


# ---------------------------------------------------------------------------
# AlphaTrend
# ---------------------------------------------------------------------------


def alphatrend(candles: list, coeff: float = 1, ap: int = 14) -> dict[str, list]:
    """AlphaTrend — MFI-gated trailing band, clamped against its own prior level.

    Matches ``ALPHATREND.calc`` in studies.ts: the gauge is Money Flow Index
    over typical price; the band is a plain SMA of true range over ``ap``. The
    level recurses as ``prev`` (0 until the first resolved bar) and clamps
    ``low - band*coeff`` when the gauge is >= 50, else ``high + band*coeff``.
    ``lagged`` is the level two bars back. ``buy_signal``/``sell_signal`` fire
    on level/lagged crossovers gated by the TS ``barsSince`` counters, matching
    the reference suppression of the very first signal.
    """
    n = len(candles)
    level: list[float | None] = [None] * n
    lagged: list[float | None] = [None] * n
    buy: list[float | None] = [None] * n
    sell: list[float | None] = [None] * n
    if n == 0:
        return {
            "alphatrend": level,
            "lagged": lagged,
            "buy_signal": buy,
            "sell_signal": sell,
        }

    period = max(1, round(float(ap)))
    coeff_f = float(coeff)

    highs, lows, closes, _o, vols = _extract(candles)
    typical = [(h + lo + c) / 3.0 for h, lo, c in zip(highs, lows, closes, strict=True)]
    band = sma(_true_ranges(highs, lows, closes), period)
    gauge = _money_flow_index(typical, vols, period)

    for i in range(n):
        prev = level[i - 1] if (i > 0 and level[i - 1] is not None) else 0.0
        b = band[i]
        g = gauge[i]
        if b is None or g is None:
            continue
        offset = b * coeff_f
        if g >= 50:
            level[i] = max(lows[i] - offset, prev)
        else:
            level[i] = min(highs[i] + offset, prev)
    for i in range(2, n):
        lagged[i] = level[i - 2]

    cross_up = [False] * n
    cross_down = [False] * n
    for i in range(1, n):
        a = level[i]
        b = lagged[i]
        pa = level[i - 1]
        pb = lagged[i - 1]
        if a is None or b is None or pa is None or pb is None:
            continue
        if a > b and pa <= pb:
            cross_up[i] = True
        elif a < b and pa >= pb:
            cross_down[i] = True

    shifted_up = [False] + cross_up[:-1]
    shifted_down = [False] + cross_down[:-1]
    since_up = _bars_since(cross_up)
    since_down = _bars_since(cross_down)
    since_shifted_up = _bars_since(shifted_up)
    since_shifted_down = _bars_since(shifted_down)
    for i in range(n):
        ssu = since_shifted_up[i]
        sd = since_down[i]
        ssd = since_shifted_down[i]
        su = since_up[i]
        if cross_up[i] and ssu is not None and sd is not None and ssu > sd:
            buy[i] = lagged[i] * 0.9999  # type: ignore[operator]
        elif cross_down[i] and ssd is not None and su is not None and ssd > su:
            sell[i] = lagged[i] * 1.0001  # type: ignore[operator]

    return {
        "alphatrend": level,
        "lagged": lagged,
        "buy_signal": buy,
        "sell_signal": sell,
    }


# ---------------------------------------------------------------------------
# Specs — ready for registry import; NOT registered here (merge task wires).
# ---------------------------------------------------------------------------


def _fn_alligator(
    candles: list,
    jawLength: int,
    jawOffset: int,
    teethLength: int,
    teethOffset: int,
    lipsLength: int,
    lipsOffset: int,
) -> dict[str, list]:
    return alligator(
        candles,
        int(jawLength),
        int(jawOffset),
        int(teethLength),
        int(teethOffset),
        int(lipsLength),
        int(lipsOffset),
    )


def _fn_parabolic_sar(
    candles: list, start: float, increment: float, maximum: float
) -> dict[str, list]:
    return parabolic_sar(candles, float(start), float(increment), float(maximum))


def _fn_ichimoku(
    candles: list,
    conversionPeriod: int,
    basePeriod: int,
    laggingSpanPeriod: int,
    displacement: int,
) -> dict[str, list]:
    return ichimoku(
        candles,
        int(conversionPeriod),
        int(basePeriod),
        int(laggingSpanPeriod),
        int(displacement),
    )


def _fn_halftrend(
    candles: list,
    amplitude: int,
    channelDeviation: float,
    atrPeriod: int,
) -> dict[str, list]:
    return halftrend(
        candles,
        int(amplitude),
        float(channelDeviation),
        int(atrPeriod),
    )


def _fn_alphatrend(candles: list, coeff: float, AP: int) -> dict[str, list]:
    return alphatrend(candles, float(coeff), int(AP))


SPEC_ALLIGATOR = IndicatorSpec(
    id="alligator",
    name="Williams Alligator",
    category="Trend",
    placement="overlay",
    params=(
        ("jawLength", "int", 13),
        ("jawOffset", "int", 8),
        ("teethLength", "int", 8),
        ("teethOffset", "int", 5),
        ("lipsLength", "int", 5),
        ("lipsOffset", "int", 3),
    ),
    plots=(
        ("jaw", "line", "Jaw"),
        ("teeth", "line", "Teeth"),
        ("lips", "line", "Lips"),
    ),
    fn=_fn_alligator,
)

SPEC_PARABOLIC_SAR = IndicatorSpec(
    id="parabolic-sar",
    name="Parabolic SAR",
    category="Trend",
    placement="overlay",
    params=(
        ("start", "float", 0.02),
        ("increment", "float", 0.02),
        ("maximum", "float", 0.2),
    ),
    plots=(("sar", "line", "SAR"),),
    fn=_fn_parabolic_sar,
)

SPEC_ICHIMOKU = IndicatorSpec(
    id="ichimoku",
    name="Ichimoku Cloud",
    category="Trend",
    placement="overlay",
    params=(
        ("conversionPeriod", "int", 9),
        ("basePeriod", "int", 26),
        ("laggingSpanPeriod", "int", 52),
        ("displacement", "int", 26),
    ),
    plots=(
        ("conversion", "line", "Conversion"),
        ("base", "line", "Base"),
        ("spanA", "line", "Span A"),
        ("spanB", "line", "Span B"),
        ("lagging", "line", "Lagging"),
    ),
    fn=_fn_ichimoku,
)

SPEC_HALFTREND = IndicatorSpec(
    id="halftrend",
    name="HalfTrend",
    category="Trend",
    placement="overlay",
    params=(
        ("amplitude", "int", 2),
        ("channelDeviation", "float", 2),
        ("atrPeriod", "int", 100),
    ),
    plots=(
        ("up", "line", "HalfTrend Up"),
        ("down", "line", "HalfTrend Down"),
        ("atr_high", "line", "Channel High"),
        ("atr_low", "line", "Channel Low"),
        ("buy_signal", "line", "Buy"),
        ("sell_signal", "line", "Sell"),
    ),
    fn=_fn_halftrend,
)

SPEC_ALPHATREND = IndicatorSpec(
    id="alphatrend",
    name="AlphaTrend",
    category="Trend",
    placement="overlay",
    params=(
        ("coeff", "float", 1),
        ("AP", "int", 14),
    ),
    plots=(
        ("alphatrend", "line", "AlphaTrend"),
        ("lagged", "line", "AlphaTrend Lag"),
    ),
    fn=_fn_alphatrend,
)

SPECS = [
    SPEC_ALLIGATOR,
    SPEC_PARABOLIC_SAR,
    SPEC_ICHIMOKU,
    SPEC_HALFTREND,
    SPEC_ALPHATREND,
]

__all__ = [
    "alligator",
    "parabolic_sar",
    "ichimoku",
    "halftrend",
    "alphatrend",
    "SPEC_ALLIGATOR",
    "SPEC_PARABOLIC_SAR",
    "SPEC_ICHIMOKU",
    "SPEC_HALFTREND",
    "SPEC_ALPHATREND",
    "SPECS",
]
