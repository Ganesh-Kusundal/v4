"""Band overlays — Envelope, Donchian, Keltner Channels (openalgo-charts parity).

Batch 1 parallel split: this module is intentionally separate from
``indicators.py`` so three indicator-port tasks can proceed concurrently.
A later merge task registers ``SPEC_ENVELOPE`` / ``SPEC_DONCHIAN`` /
``SPEC_KELTNER_CHANNEL`` into the shared registry and adds the PLOT_MAP
entry mapping the backend plot key ``"middle"`` to the chart's ``"basis"``
(bollinger precedent).

Parity notes from the TS sources:
- Envelope (src/indicators/overlay.ts): basis is an SMA over ``length``
  (the ``exponential`` flag defaults to false), bands are basis scaled by
  ``1 +- percent/100``.
- Donchian (src/indicators/overlay.ts): rolling highest high / lowest low
  over ``length``, mid is their average; the whole set is displaced by
  ``offset`` bars where a positive offset draws values LATER
  (reference ``plot(..., offset=n)``).
- Keltner (src/indicators/adaptive.ts): basis is an SMA-seeded EMA
  (``exp`` flag defaults to true) landing at index ``length - 1``; the rail
  is the base bundle's ATR — Wilder smoothing seeded with the SMA of
  TR[0..n-1], TR[0] = high - low — which is exactly the Batch 0 backend
  :func:`atr` semantics, reused here unmodified.
"""

from __future__ import annotations

from typing import Any

from tradex_analytics.indicators import (
    IndicatorSpec,
    _highest,
    _lowest,
    _shift,
    _sma_seeded_ema,
    _to_float,
    atr,
    sma,
)


def _closes(candles: list) -> list[float]:
    return [_to_float(c.ohlc.close.value) for c in candles]


def envelope(
    values: list, period: int = 20, percent: float = 10.0
) -> dict[str, list]:
    """Envelope: SMA basis with bands at basis * (1 +- percent/100).

    Matches openalgo-charts ``ENVELOPE`` (src/indicators/overlay.ts,
    exponential flag default false). Returns dict keyed 'upper'/'middle'/
    'lower', each None-padded before index ``period - 1``.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    basis = sma(values, period)
    k = percent / 100.0
    upper = [None if b is None else b * (1 + k) for b in basis]
    lower = [None if b is None else b * (1 - k) for b in basis]
    return {"upper": upper, "middle": basis, "lower": lower}


def donchian(
    candles: list, period: int = 20, offset: int = 0
) -> dict[str, list]:
    """Donchian Channels: highest-high / lowest-low band, displaced by offset.

    Matches openalgo-charts ``DONCHIAN`` (src/indicators/overlay.ts):
    upper/lower roll over ``period`` bars of highs/lows, mid is their
    average, and the whole set shifts by ``offset`` (may be negative;
    positive draws later). Returns dict keyed 'upper'/'middle'/'lower'.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    highs = [_to_float(c.ohlc.high.value) for c in candles]
    lows = [_to_float(c.ohlc.low.value) for c in candles]
    upper = _highest(highs, period)
    lower = _lowest(lows, period)
    mid = [
        None if u is None or l is None else (u + l) / 2.0
        for u, l in zip(upper, lower, strict=True)
    ]
    k = round(offset)
    return {
        "upper": _shift(upper, k),
        "middle": _shift(mid, k),
        "lower": _shift(lower, k),
    }


def keltner_channel(
    candles: list, period: int = 20, mult: float = 2.0, atr_length: int = 10
) -> dict[str, list]:
    """Keltner Channels: SMA-seeded-EMA basis with ATR rails.

    Matches openalgo-charts ``KELTNER_CHANNEL`` (src/indicators/adaptive.ts,
    exp=true / Average True Range defaults): bands = basis +- mult *
    ATR(atr_length). The rail reuses the Batch 0 backend :func:`atr`
    (TR[0]=H-L, Wilder seed at ``atr_length - 1``). Returns dict keyed
    'upper'/'middle'/'lower'; the band starts at whichever warmup is slower.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if atr_length <= 0:
        raise ValueError("period must be positive")
    closes = _closes(candles)
    basis = _sma_seeded_ema(closes, period)
    rail = atr(candles, atr_length)
    n = len(closes)
    upper: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    for i in range(n):
        b = basis[i]
        r = rail[i]
        if b is None or r is None:
            continue
        off = r * mult
        upper[i] = b + off
        lower[i] = b - off
    return {"upper": upper, "middle": basis, "lower": lower}


def _make_spec(
    id_: str, name_: str, params: tuple, fn: Any
) -> IndicatorSpec:
    return IndicatorSpec(
        id=id_,
        name=name_,
        category="Volatility",
        placement="overlay",
        params=params,
        plots=(
            ("upper", "line", "Upper"),
            ("middle", "line", "Middle"),
            ("lower", "line", "Lower"),
        ),
        fn=fn,
    )


SPEC_ENVELOPE = _make_spec(
    "envelope",
    "Envelope",
    (("period", "int", 20), ("percent", "float", 10.0)),
    lambda candles, period, percent: envelope(_closes(candles), int(period), float(percent)),
)

SPEC_DONCHIAN = _make_spec(
    "donchian",
    "Donchian Channels",
    (("period", "int", 20), ("offset", "int", 0)),
    lambda candles, period, offset: donchian(candles, int(period), int(offset)),
)

SPEC_KELTNER_CHANNEL = _make_spec(
    "keltner-channel",
    "Keltner Channels",
    (("period", "int", 20), ("mult", "float", 2.0), ("atrlength", "int", 10)),
    lambda candles, period, mult, atrlength: keltner_channel(
        candles, int(period), float(mult), int(atrlength)
    ),
)

