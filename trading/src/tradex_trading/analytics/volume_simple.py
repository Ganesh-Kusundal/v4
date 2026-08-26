"""Simple volume indicators — ADL, raw volume, PVT (openalgo-charts parity).

TS sources
- ``src/indicators/volume.ts`` — ADL, VOLUME
- ``src/indicators/indices.ts`` — PVT

Parity notes
- ADL: ``money_flow_mult = ((close - low) - (high - close)) / (high - low)``;
  doji bars (high == low) contribute nothing. Running total from bar 0.
- Volume: pass-through of ``candle.volume.value``.
- PVT: ``((close - prev_close) / prev_close) * volume`` per bar, cumulative
  from bar 0. Bar 0 has no previous close so contributes 0.
"""

from __future__ import annotations

from tradex_trading.analytics.indicators import (
    IndicatorSpec,
    _cumulative,
    _isfinite,
    _to_float,
)


def adl(candles: list) -> dict[str, list]:
    """Accumulation/Distribution Line.

    Running total of ``money_flow_multiplier * volume``. Doji bars (high == low)
    contribute nothing. Matches openalgo-charts ``ADL.calc``.
    """
    n = len(candles)
    out: list[float] = [0.0] * n
    acc = 0.0
    for i in range(n):
        c = candles[i]
        h = _to_float(c.ohlc.high.value)
        lo = _to_float(c.ohlc.low.value)
        cl = _to_float(c.ohlc.close.value)
        v = float(c.volume.value)
        span = h - lo
        if span > 0:
            acc += (((cl - lo) - (h - cl)) / span) * v
        out[i] = acc
    return {"adl": out}


def volume_raw(candles: list) -> dict[str, list]:
    """Raw volume per bar.

    Matches openalgo-charts ``VOLUME.calc``: ``bars.map(b => b.volume ?? 0)``.
    """
    return {"volume": [float(c.volume.value) for c in candles]}


def pvt(candles: list) -> dict[str, list]:
    """Price Volume Trend.

    Running total of ``((close - prev_close) / prev_close) * volume``.
    Bar 0 contributes 0 (no previous close). Matches openalgo-charts ``PVT.calc``.
    """
    n = len(candles)
    terms: list[float | None] = [None] * n
    for i in range(1, n):
        prev_cl = _to_float(candles[i - 1].ohlc.close.value)
        if prev_cl == 0 or not _isfinite(prev_cl):
            continue
        cl = _to_float(candles[i].ohlc.close.value)
        v = float(candles[i].volume.value)
        terms[i] = ((cl - prev_cl) / prev_cl) * v
    return {"pvt": _cumulative(terms)}


SPEC_ADL = IndicatorSpec(
    id="adl",
    name="Accumulation/Distribution",
    category="Volume",
    placement="pane",
    params=(),
    plots=(("adl", "line", "A/D Line"),),
    fn=adl,
)

SPEC_VOLUME = IndicatorSpec(
    id="volume",
    name="Volume",
    category="Volume",
    placement="pane",
    params=(),
    plots=(("volume", "histogram", "Volume"),),
    fn=volume_raw,
)

SPEC_PVT = IndicatorSpec(
    id="pvt",
    name="Price Volume Trend",
    category="Volume",
    placement="pane",
    params=(),
    plots=(("pvt", "line", "PVT"),),
    fn=pvt,
)

__all__ = [
    "adl",
    "pvt",
    "volume_raw",
    "SPEC_ADL",
    "SPEC_PVT",
    "SPEC_VOLUME",
]
