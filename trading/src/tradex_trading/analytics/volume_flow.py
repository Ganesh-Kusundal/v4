"""Volume flow indicators — openalgo-charts parity (flow.ts / indices.ts).

Batch 4 parallel split: volume/flow studies ported from the reference TS
descriptors. Helpers (``_sma_seeded_ema``, ``_rolling_sum``, etc.) are
imported from ``indicators.py`` and never redefined.

TS sources
- ``src/indicators/flow.ts`` — CHAIKIN_MONEY_FLOW, CHAIKIN_OSCILLATOR,
  EASE_OF_MOVEMENT, ELDER_FORCE_INDEX
- ``src/indicators/indices.ts`` — ULCER_INDEX
- ``src/indicators/calc.ts`` — ``smaSeededEma``, ``highest``, ``cumulative``,
  ``change``, ``rollingSum``, ``sma``

Parity notes
- ``_sma_skip_none`` mirrors the TS ``sma`` NaN-counting semantics: any
  ``None`` in the window blanks the output for that position.  The imported
  ``sma`` from ``indicators.py`` does not handle ``None`` and would crash,
  so Ease of Movement and Ulcer Index use the local helper.
- ``_ema_of_gapped`` matches ``emaFromFirstFinite`` in flow.ts: slices off
  leading Nones, runs ``_sma_seeded_ema`` on the live tail, re-pads.
- ``_money_flow`` matches the TS ``moneyFlow`` guard: degenerate bars
  (``close == high == low`` or ``high == low``) contribute 0, not NaN,
  preventing a running sum from being poisoned.
"""

from __future__ import annotations

import math
from typing import Any

from tradex_trading.analytics.indicators import (
    IndicatorSpec,
    _change,
    _cumulative,
    _ema_of_gapped,
    _highest,
    _isfinite,
    _rolling_sum,
    _to_float,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _vol(candle: Any) -> float:
    """Reference ``nz(volume)``: a bar with no volume traded nothing."""
    v = candle.volume.value
    if v is None:
        return 0.0
    fv = float(v)
    return fv if math.isfinite(fv) else 0.0


def _money_flow(candles: list) -> list[float]:
    """Accumulation/Distribution money-flow term.

    Matches ``moneyFlow`` in flow.ts: degenerate bars (no range) contribute 0
    rather than NaN so a running sum is never poisoned.
    """
    n = len(candles)
    out = [0.0] * n
    for i in range(n):
        c = candles[i]
        h = _to_float(c.ohlc.high.value)
        lo = _to_float(c.ohlc.low.value)
        cl = _to_float(c.ohlc.close.value)
        degenerate = (cl == h and cl == lo) or h == lo
        if not degenerate:
            out[i] = ((2.0 * cl - lo - h) / (h - lo)) * _vol(c)
    return out


def _sma_skip_none(values: list, period: int) -> list[float | None]:
    """SMA that returns ``None`` for any window containing ``None``.

    Mirrors the TS ``sma`` in calc.ts which counts NaN values and blanks any
    window that holds one.  The imported ``sma`` from ``indicators.py`` does
    not handle ``None`` and would crash, so this module uses this helper for
    Ease of Movement and Ulcer Index where intermediate ``None`` values occur.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    s = 0.0
    bad = 0
    for i in range(n):
        v = values[i]
        if _isfinite(v):
            s += v
        else:
            bad += 1
        if i >= period:
            gone = values[i - period]
            if _isfinite(gone):
                s -= gone
            else:
                bad -= 1
        if i >= period - 1:
            out[i] = s / period if bad == 0 else None
    return out


# ---------------------------------------------------------------------------
# Chaikin Money Flow
# ---------------------------------------------------------------------------


def chaikin_money_flow(candles: list, length: int = 20) -> dict[str, list]:
    """Chaikin Money Flow — bounded -1..+1 share of participation.

    Matches ``CHAIKIN_MONEY_FLOW.calc`` in flow.ts: money-flow term summed
    over the window and normalised by the volume traded in that window.
    """
    n = len(candles)
    mf = _money_flow(candles)
    flow = _rolling_sum(mf, length)
    vols = [_vol(c) for c in candles]
    traded = _rolling_sum(vols, length)
    cmf: list[float | None] = [None] * n
    for i in range(n):
        t = traded[i]
        f = flow[i]
        if t is not None and f is not None and t > 0:
            cmf[i] = f / t
    return {"cmf": cmf}


# ---------------------------------------------------------------------------
# Chaikin Oscillator
# ---------------------------------------------------------------------------


def chaikin_oscillator(
    candles: list, short: int = 3, long: int = 10
) -> dict[str, list]:
    """Chaikin Oscillator — MACD of the A/D line.

    Matches ``CHAIKIN_OSCILLATOR.calc`` in flow.ts: two EMAs (fast/slow) over
    the cumulative money-flow term, subtracted.
    """
    mf = _money_flow(candles)
    accdist = _cumulative(mf)
    fast = _ema_of_gapped(accdist, short)
    slow = _ema_of_gapped(accdist, long)
    n = len(candles)
    osc: list[float | None] = [None] * n
    for i in range(n):
        f = fast[i]
        s = slow[i]
        if f is not None and s is not None:
            osc[i] = f - s
    return {"osc": osc}


# ---------------------------------------------------------------------------
# Ease of Movement
# ---------------------------------------------------------------------------


def ease_of_movement(
    candles: list, length: int = 14, divisor: int = 10000
) -> dict[str, list]:
    """Ease of Movement — midpoint travel per unit volume, SMA-smoothed.

    Matches ``EASE_OF_MOVEMENT.calc`` in flow.ts: ``change(midpoint, 1)``
    scaled by the bar's range and divisor, averaged over ``length``.
    Zero-volume bars produce ``None`` which blanks the SMA window.
    """
    n = len(candles)
    mid = [
        (_to_float(c.ohlc.high.value) + _to_float(c.ohlc.low.value)) / 2.0
        for c in candles
    ]
    move = _change(mid, 1)
    term: list[float | None] = [None] * n
    for i in range(n):
        m = move[i]
        v = _vol(candles[i])
        if m is None or v == 0:
            continue
        h = _to_float(candles[i].ohlc.high.value)
        lo = _to_float(candles[i].ohlc.low.value)
        term[i] = (divisor * m * (h - lo)) / v
    return {"eom": _sma_skip_none(term, length)}


# ---------------------------------------------------------------------------
# Elder Force Index
# ---------------------------------------------------------------------------


def elder_force_index(candles: list, length: int = 13) -> dict[str, list]:
    """Elder Force Index — price change weighted by volume, EMA-smoothed.

    Matches ``ELDER_FORCE_INDEX.calc`` in flow.ts: ``change(close, 1) * vol``
    smoothed via ``emaFromFirstFinite`` (``_ema_of_gapped``).
    """
    closes = [_to_float(c.ohlc.close.value) for c in candles]
    moved = _change(closes, 1)
    n = len(candles)
    force: list[float | None] = [None] * n
    for i in range(n):
        m = moved[i]
        if m is not None:
            force[i] = m * _vol(candles[i])
    return {"efi": _ema_of_gapped(force, length)}


# ---------------------------------------------------------------------------
# Ulcer Index
# ---------------------------------------------------------------------------


def ulcer_index(candles: list, length: int = 14) -> dict[str, list]:
    """Ulcer Index — root-mean-square percentage drawdown from rolling peak.

    Matches ``ULCER_INDEX.calc`` in indices.ts: ``(100*(close-peak)/peak)^2``
    averaged over ``length``, then square-rooted.
    """
    closes = [_to_float(c.ohlc.close.value) for c in candles]
    peak = _highest(closes, length)
    n = len(candles)
    squared: list[float | None] = [None] * n
    for i in range(n):
        hi = peak[i]
        if hi is not None and _isfinite(hi) and hi != 0:
            dd = (100.0 * (closes[i] - hi)) / hi
            squared[i] = dd * dd
    mean = _sma_skip_none(squared, length)
    ui: list[float | None] = [None] * n
    for i in range(n):
        m = mean[i]
        if m is not None and m >= 0:
            ui[i] = math.sqrt(m)
    return {"ui": ui}


# ---------------------------------------------------------------------------
# Specs — ready for registry import; NOT registered here (merge task wires).
# ---------------------------------------------------------------------------


def _fn_chaikin_money_flow(candles: list, length: int) -> dict[str, list]:
    return chaikin_money_flow(candles, int(length))


def _fn_chaikin_oscillator(
    candles: list, short: int, long: int
) -> dict[str, list]:
    return chaikin_oscillator(candles, int(short), int(long))


def _fn_ease_of_movement(
    candles: list, length: int, divisor: int
) -> dict[str, list]:
    return ease_of_movement(candles, int(length), int(divisor))


def _fn_elder_force_index(candles: list, length: int) -> dict[str, list]:
    return elder_force_index(candles, int(length))


def _fn_ulcer_index(candles: list, length: int) -> dict[str, list]:
    return ulcer_index(candles, int(length))


SPEC_CHAIKIN_MONEY_FLOW = IndicatorSpec(
    id="chaikin-money-flow",
    name="Chaikin Money Flow",
    category="Volume",
    placement="pane",
    params=(("length", "int", 20),),
    plots=(("cmf", "line", "CMF"),),
    levels=({"value": 0},),
    fn=_fn_chaikin_money_flow,
)

SPEC_CHAIKIN_OSCILLATOR = IndicatorSpec(
    id="chaikin-oscillator",
    name="Chaikin Oscillator",
    category="Volume",
    placement="pane",
    params=(
        ("short", "int", 3),
        ("long", "int", 10),
    ),
    plots=(("osc", "line", "Chaikin Oscillator"),),
    levels=({"value": 0},),
    fn=_fn_chaikin_oscillator,
)

SPEC_EASE_OF_MOVEMENT = IndicatorSpec(
    id="ease-of-movement",
    name="Ease of Movement",
    category="Volume",
    placement="pane",
    params=(
        ("length", "int", 14),
        ("divisor", "int", 10000),
    ),
    plots=(("eom", "line", "EOM"),),
    fn=_fn_ease_of_movement,
)

SPEC_ELDER_FORCE_INDEX = IndicatorSpec(
    id="elder-force-index",
    name="Elder Force Index",
    category="Volume",
    placement="pane",
    params=(("length", "int", 13),),
    plots=(("efi", "line", "Elder Force Index"),),
    levels=({"value": 0},),
    fn=_fn_elder_force_index,
)

SPEC_ULCER_INDEX = IndicatorSpec(
    id="ulcer-index",
    name="Ulcer Index",
    category="Volatility",
    placement="pane",
    params=(("length", "int", 14),),
    plots=(("ui", "line", "Ulcer Index"),),
    levels=({"value": 0},),
    fn=_fn_ulcer_index,
)

__all__ = [
    "chaikin_money_flow",
    "chaikin_oscillator",
    "ease_of_movement",
    "elder_force_index",
    "ulcer_index",
    "SPEC_CHAIKIN_MONEY_FLOW",
    "SPEC_CHAIKIN_OSCILLATOR",
    "SPEC_EASE_OF_MOVEMENT",
    "SPEC_ELDER_FORCE_INDEX",
    "SPEC_ULCER_INDEX",
]
