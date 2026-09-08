"""Simple/complex studies — openalgo-charts parity (strength/averages/ranges).

Batch 5 parallel split: five studies ported from the reference TS descriptors.
Helpers (``sma``, ``wma``, ``_change``, ``_sma_seeded_ema``, ``_sma_skip_none``)
are imported from ``indicators.py`` / ``volume_flow.py`` and never redefined.

TS sources
- ``src/indicators/strength.ts`` — MOMENTUM
- ``src/indicators/averages.ts`` — MA_CROSS, MA_RIBBON
- ``src/indicators/ranges.ts`` — WOODIES_CCI, SPECIAL_K
- ``src/indicators/calc.ts`` — ``smaSeededEma``, ``change``, ``roc``, ``cci``,
  ``dev``, ``rma``, ``vwma``, ``sma``

Parity notes
- ``_cci`` mirrors calc.ts ``cci`` over **close** (Woodies CCI uses ``close``,
  not the typical price the momentum.ts CCI uses) with the 0.015 multiplier on
  the mean absolute deviation; ``md == 0`` is a gap, not a zero.
- ``_roc_ts`` mirrors calc.ts ``roc`` exactly (``(100 * (src - src[n])) / src[n]``,
  NaN on a zero base) rather than the backend ``roc`` which emits 0.0 there.
- ``_sma_skip_none`` implements calc.ts ``sma`` NaN-counting semantics, which is
  what special-k's ``fromFirstValue(...)`` stacking reduces to.
- ``_moving_average`` mirrors averages.ts ``movingAverage``: EMA seeds via
  ``smaSeededEma``, SMMA (RMA) is Wilder's, WMA/VWMA are the calc.ts kernels,
  and anything else (including the lowercase defaults) is an SMA.
"""

from __future__ import annotations

from typing import Any

from tradex_trading.analytics.indicators import (
    IndicatorSpec,
    LEGACY_PARAM_ALIASES,
    _change,
    _rma,
    _sma_seeded_ema,
    _to_float,
    sma,
    wma,
)
from tradex_trading.analytics.volume.volume_flow import _sma_skip_none

LEGACY_PARAM_ALIASES["ma-cross"] = {"short_length": "shortLength", "long_length": "longLength"}
LEGACY_PARAM_ALIASES["ma-ribbon"] = {
    "ma1_type": "ma1Type",
    "ma1_source": "ma1Source",
    "ma1_length": "ma1Length",
    "ma2_type": "ma2Type",
    "ma2_source": "ma2Source",
    "ma2_length": "ma2Length",
    "ma3_type": "ma3Type",
    "ma3_source": "ma3Source",
    "ma3_length": "ma3Length",
    "ma4_type": "ma4Type",
    "ma4_source": "ma4Source",
    "ma4_length": "ma4Length",
}
LEGACY_PARAM_ALIASES["woodies-cci"] = {
    "cci_turbo_length": "cciTurboLength",
    "cci14_length": "cci14Length",
}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _src_val(candle: Any, key: str) -> float:
    """Open/high/low/close of a candle whether it is a dict or a Candle object."""
    if isinstance(candle, dict):
        return _to_float(candle[key])
    return _to_float(getattr(candle.ohlc, key).value)


def _source_values(candles: list, source: str) -> list[float]:
    """Reference ``sourceValues`` — the OHLCV-derived series an input selects."""
    if source == "close":
        return [_src_val(c, "close") for c in candles]
    if source == "open":
        return [_src_val(c, "open") for c in candles]
    if source == "high":
        return [_src_val(c, "high") for c in candles]
    if source == "low":
        return [_src_val(c, "low") for c in candles]
    if source == "hl2":
        return [
            (_src_val(c, "high") + _src_val(c, "low")) / 2.0 for c in candles
        ]
    if source == "hlc3":
        return [
            (_src_val(c, "high") + _src_val(c, "low") + _src_val(c, "close")) / 3.0
            for c in candles
        ]
    if source == "ohlc4":
        return [
            (
                _src_val(c, "open")
                + _src_val(c, "high")
                + _src_val(c, "low")
                + _src_val(c, "close")
            )
            / 4.0
            for c in candles
        ]
    if source == "hlcc4":
        return [
            (
                _src_val(c, "high")
                + _src_val(c, "low")
                + 2.0 * _src_val(c, "close")
            )
            / 4.0
            for c in candles
        ]
    return [_src_val(c, "close") for c in candles]


def _vol(candle: Any) -> float:
    """Reference ``nz(volume)``: a bar with no volume traded nothing."""
    if isinstance(candle, dict):
        v = candle.get("volume")
        if v is None:
            return 0.0
        fv = float(v)
        return fv if fv == fv else 0.0
    v = candle.volume.value
    if v is None:
        return 0.0
    fv = float(v)
    return fv if fv == fv else 0.0


def _roc_ts(values: list[float], n: int) -> list[float | None]:
    """calc.ts ``roc``: ``(100 * (src - src[n])) / src[n]``, None for warmup."""
    m = len(values)
    out: list[float | None] = [None] * m
    if n <= 0:
        return out
    for i in range(n, m):
        base = values[i - n]
        out[i] = None if base == 0 else (100.0 * (values[i] - base)) / base
    return out


def _vwma(
    values: list[float], vols: list[float], period: int
) -> list[float | None]:
    """calc.ts ``vwma``: ``sma(src*vol, len) / sma(vol, len)``."""
    n = len(values)
    pv = [values[i] * vols[i] for i in range(n)]
    num = sma(pv, period)
    den = sma(vols, period)
    out: list[float | None] = [None] * n
    for i in range(n):
        d = den[i]
        if d is not None and d != 0 and num[i] is not None:
            out[i] = num[i] / d
    return out


def _moving_average(
    kind: str, values: list[float], vols: list[float], length: int
) -> list[float | None]:
    """averages.ts ``movingAverage`` — the ribbon's kernel switch."""
    k = kind.upper()
    if k == "EMA":
        return _sma_seeded_ema(values, length)
    if k == "SMMA (RMA)":
        return _rma(values, length)
    if k == "WMA":
        return wma(values, length)
    if k == "VWMA":
        return _vwma(values, vols, length)
    return sma(values, length)


def _cci(values: list[float], period: int) -> list[float | None]:
    """calc.ts ``cci``: ``(src - sma) / (0.015 * meanAbsDev)``, over the source."""
    n = len(values)
    mean = sma(values, period)
    out: list[float | None] = [None] * n
    for i in range(period - 1, n):
        m = mean[i]
        if m is None:
            continue
        dev = 0.0
        for k in range(period):
            dev += abs(values[i - k] - m)
        md = dev / period
        if md == 0:
            out[i] = None
        else:
            out[i] = (values[i] - m) / (0.015 * md)
    return out


# ---------------------------------------------------------------------------
# Momentum (strength.ts MOMENTUM)
# ---------------------------------------------------------------------------


def momentum(candles: list, len: int = 10) -> dict[str, list[float | None]]:
    """Momentum — ``close[i] - close[i - len]``, no smoothing.

    Matches ``MOMENTUM.calc`` in strength.ts: ``nulls(change(source, len))``
    on ``close``, so the first value lands at index ``len``.
    """
    closes = [_src_val(c, "close") for c in candles]
    return {"mom": _change(closes, int(len))}


# ---------------------------------------------------------------------------
# MA Cross (averages.ts MA_CROSS)
# ---------------------------------------------------------------------------


def ma_cross(
    candles: list,
    short_length: int = 9,
    long_length: int = 21,
) -> dict[str, list[float | None]]:
    """MA Cross — two SMAs of ``close`` plus cross markers.

    Matches ``MA_CROSS.calc`` in averages.ts: ``cross`` is ``null`` except on
    bars where the averages actually crossed, where it carries the short MA's
    value.  A crossover/crossunder needs both sides finite on both bars.
    """
    closes = [_src_val(c, "close") for c in candles]
    short = sma(closes, int(short_length))
    long = sma(closes, int(long_length))
    n = len(closes)
    cross: list[float | None] = [None] * n
    for i in range(1, n):
        ps, pl = short[i - 1], long[i - 1]
        cs, cl = short[i], long[i]
        if ps is None or pl is None or cs is None or cl is None:
            continue
        if (cs > cl and ps <= pl) or (cs < cl and ps >= pl):
            cross[i] = cs
    return {"short": short, "long": long, "cross": cross}


# ---------------------------------------------------------------------------
# MA Ribbon (averages.ts MA_RIBBON)
# ---------------------------------------------------------------------------


def ma_ribbon(
    candles: list,
    ma1_type: str = "sma",
    ma1_source: str = "close",
    ma1_length: int = 20,
    ma2_type: str = "sma",
    ma2_source: str = "close",
    ma2_length: int = 50,
    ma3_type: str = "sma",
    ma3_source: str = "close",
    ma3_length: int = 100,
    ma4_type: str = "sma",
    ma4_source: str = "close",
    ma4_length: int = 200,
) -> dict[str, list[float | None]]:
    """Moving Average Ribbon — four independent averages on one overlay.

    Matches ``MA_RIBBON.calc`` in averages.ts: every lane picks its own kernel
    (``SMA``/``EMA``/``SMMA (RMA)``/``WMA``/``VWMA``), source, and length.
    All four lanes are always shown (the reference ``showMa`` display guard is
    not exposed here).
    """
    vols = [_vol(c) for c in candles]
    lanes = [
        ("ma1", ma1_type, ma1_source, ma1_length),
        ("ma2", ma2_type, ma2_source, ma2_length),
        ("ma3", ma3_type, ma3_source, ma3_length),
        ("ma4", ma4_type, ma4_source, ma4_length),
    ]
    out: dict[str, list[float | None]] = {}
    for key, typ, source, length in lanes:
        values = _source_values(candles, source)
        out[key] = _moving_average(typ, values, vols, int(length))
    return out


# ---------------------------------------------------------------------------
# Woodies CCI (ranges.ts WOODIES_CCI)
# ---------------------------------------------------------------------------


def woodies_cci(
    candles: list,
    cci_turbo_length: int = 6,
    cci14_length: int = 14,
) -> dict[str, list[float | None]]:
    """Woodies CCI — a 14-bar CCI over ``close`` drawn twice plus a fast turbo.

    Matches ``WOODIES_CCI.calc`` in ranges.ts: ``hist`` and ``cci14`` are the
    same slow series; ``turbo`` is the fast ``cci``.  The reference's
    colour-coded histogram states are a rendering concern and carry no data.
    """
    closes = [_src_val(c, "close") for c in candles]
    turbo = _cci(closes, int(cci_turbo_length))
    slow = _cci(closes, int(cci14_length))
    return {"hist": slow, "turbo": turbo, "cci14": slow}


# ---------------------------------------------------------------------------
# Special K (ranges.ts SPECIAL_K)
# ---------------------------------------------------------------------------

# Pring's published (weight, roc, smooth) terms, in source order.
SPECIAL_K_TERMS: tuple[tuple[int, int, int], ...] = (
    (10, 10, 10),
    (15, 15, 10),
    (20, 20, 10),
    (25, 25, 10),
    (30, 30, 15),
    (40, 50, 50),
    (50, 65, 65),
    (65, 75, 75),
    (75, 100, 100),
    (100, 195, 130),
)


def special_k(
    candles: list,
    length1: int = 100,
    length2: int = 100,
) -> dict[str, list[float | None]]:
    """Pring's Special K — ten weighted, smoothed ROC terms summed into one line.

    Matches ``SPECIAL_K.calc`` in ranges.ts: each term is
    ``weight * sma(roc(source, rocLen), smooth)`` with calc.ts NaN propagation
    (a ``None`` in any term blanks the running total and keeps it blank), then a
    twice-smoothed signal via ``sma(once, length2)``.  The slowest term needs
    ``roc(195)+smooth(130)`` bars, so on short series both plots stay all-None.
    """
    closes = [_src_val(c, "close") for c in candles]
    n = len(closes)
    out: list[float | None] = [0.0] * n
    for weight, roc_len, smooth in SPECIAL_K_TERMS:
        r = _roc_ts(closes, roc_len)
        smoothed = _sma_skip_none(r, smooth)
        for i in range(n):
            s = smoothed[i]
            if s is None or out[i] is None:
                out[i] = None
            else:
                out[i] = out[i] + weight * s
    once = _sma_skip_none(out, int(length1))
    signal = _sma_skip_none(once, int(length2))
    return {"specialK": out, "signal": signal}


# ---------------------------------------------------------------------------
# Specs — ready for registry import; NOT registered here (merge task wires).
# ---------------------------------------------------------------------------


def _fn_momentum(candles: list, len: int) -> dict[str, list]:
    return momentum(candles, int(len))


def _fn_ma_cross(candles: list, shortLength: int, longLength: int) -> dict[str, list]:
    return ma_cross(candles, int(shortLength), int(longLength))


def _fn_ma_ribbon(
    candles,
    ma1Type, ma1Source, ma1Length,
    ma2Type, ma2Source, ma2Length,
    ma3Type, ma3Source, ma3Length,
    ma4Type, ma4Source, ma4Length,
):
    return ma_ribbon(
        candles,
        ma1Type, ma1Source, int(ma1Length),
        ma2Type, ma2Source, int(ma2Length),
        ma3Type, ma3Source, int(ma3Length),
        ma4Type, ma4Source, int(ma4Length),
    )


def _fn_woodies_cci(candles: list, cciTurboLength: int, cci14Length: int) -> dict[str, list]:
    return woodies_cci(candles, int(cciTurboLength), int(cci14Length))


def _fn_special_k(candles: list, length1: int, length2: int) -> dict[str, list]:
    return special_k(candles, int(length1), int(length2))


SPEC_MOMENTUM = IndicatorSpec(
    id="momentum",
    name="Momentum",
    category="Momentum",
    placement="pane",
    params=(("len", "int", 10),),
    plots=(("mom", "line", "MOM"),),
    levels=({"value": 0},),
    fn=_fn_momentum,
)

SPEC_MA_CROSS = IndicatorSpec(
    id="ma-cross",
    name="MA Cross",
    category="Trend",
    placement="overlay",
    params=(
        ("shortLength", "int", 9),
        ("longLength", "int", 21),
    ),
    plots=(
        ("short", "line", "Short MA"),
        ("long", "line", "Long MA"),
        ("cross", "line", "Cross"),
    ),
    fn=_fn_ma_cross,
)

SPEC_MA_RIBBON = IndicatorSpec(
    id="ma-ribbon",
    name="Moving Average Ribbon",
    category="Trend",
    placement="overlay",
    params=(
        ("ma1Type", "select", "sma"),
        ("ma1Source", "source", "close"),
        ("ma1Length", "int", 20),
        ("ma2Type", "select", "sma"),
        ("ma2Source", "source", "close"),
        ("ma2Length", "int", 50),
        ("ma3Type", "select", "sma"),
        ("ma3Source", "source", "close"),
        ("ma3Length", "int", 100),
        ("ma4Type", "select", "sma"),
        ("ma4Source", "source", "close"),
        ("ma4Length", "int", 200),
    ),
    plots=(
        ("ma1", "line", "MA #1"),
        ("ma2", "line", "MA #2"),
        ("ma3", "line", "MA #3"),
        ("ma4", "line", "MA #4"),
    ),
    fn=_fn_ma_ribbon,
)

SPEC_WOODIES_CCI = IndicatorSpec(
    id="woodies-cci",
    name="Woodies CCI",
    category="Momentum",
    placement="pane",
    params=(
        ("cciTurboLength", "int", 6),
        ("cci14Length", "int", 14),
    ),
    plots=(
        ("hist", "histogram", "CCI Turbo Histogram"),
        ("turbo", "line", "CCI Turbo"),
        ("cci14", "line", "CCI 14"),
    ),
    levels=({"value": 100}, {"value": 0}, {"value": -100}),
    fn=_fn_woodies_cci,
)

SPEC_SPECIAL_K = IndicatorSpec(
    id="special-k",
    name="Pring's Special K",
    category="Momentum",
    placement="pane",
    params=(
        ("length1", "int", 100),
        ("length2", "int", 100),
    ),
    plots=(
        ("specialK", "line", "Special K"),
        ("signal", "line", "Signal"),
    ),
    levels=({"value": 0},),
    fn=_fn_special_k,
)

SPECS = [
    SPEC_MOMENTUM,
    SPEC_MA_CROSS,
    SPEC_MA_RIBBON,
    SPEC_WOODIES_CCI,
    SPEC_SPECIAL_K,
]

__all__ = [
    "momentum",
    "ma_cross",
    "ma_ribbon",
    "woodies_cci",
    "special_k",
    "SPEC_MOMENTUM",
    "SPEC_MA_CROSS",
    "SPEC_MA_RIBBON",
    "SPEC_WOODIES_CCI",
    "SPEC_SPECIAL_K",
    "SPECS",
]

