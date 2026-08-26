"""Oscillators range B — Fisher Transform, Chande Momentum, Connors RSI, Balance of Power.

Batch 2 parallel split: standalone module so four indicator-port tasks can run
concurrently without touching ``indicators.py``.  Helpers are imported from
``indicators.py``; all parity notes reference openalgo-charts
``src/indicators/oscillators.ts`` and ``src/indicators/calc.ts``.
"""

from __future__ import annotations

import math

from tradex_trading.analytics.indicators import IndicatorSpec, _to_float, roc, rsi

__all__ = [
    "SPEC_BALANCE_OF_POWER",
    "SPEC_CHANDE_MOMENTUM",
    "SPEC_CONNORS_RSI",
    "SPEC_FISHER_TRANSFORM",
    "balance_of_power",
    "chande_momentum",
    "connors_rsi",
    "connors_streak",
    "fisher_transform",
]


# ---------------------------------------------------------------------------
# Local calc helpers (mirrors src/indicators/calc.ts)
# ---------------------------------------------------------------------------


def _highest(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        out[i] = max(values[i - period + 1 : i + 1])
    return out


def _lowest(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        out[i] = min(values[i - period + 1 : i + 1])
    return out


def _change(values: list[float]) -> list[float | None]:
    """Reference ``change(src, 1)``: src - src[1], None at bar 0."""
    n = len(values)
    out: list[float | None] = [None] * n
    for i in range(1, n):
        out[i] = values[i] - values[i - 1]
    return out


def _rolling_sum(values: list[float], period: int) -> list[float | None]:
    """Reference ``rollingSum`` — NaN/None before index period-1."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    acc = sum(values[:period])
    out[period - 1] = acc
    for i in range(period, n):
        acc += values[i] - values[i - period]
        out[i] = acc
    return out


def _percent_rank(values: list[float | None], period: int) -> list[float | None]:
    """Reference ``percentRank`` — previous period values <= current, as 0..100."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0:
        return out
    for i in range(period, n):
        cur = values[i]
        if cur is None:
            continue
        window = values[i - period : i]  # previous period, not including i
        if any(v is None for v in window):
            continue
        cnt = sum(1 for v in window if v <= cur)  # type: ignore[operator]
        out[i] = (cnt * 100.0) / period
    return out


def connors_streak(values: list[float]) -> list[float]:
    """Port of ``connorsStreak`` in oscillators.ts.

    Signed run length of consecutive up/down closes.  Bar 0 is -1 (reference
    ``nz(ud[1])`` reads 0, so the first bar falls into the down branch).
    """
    n = len(values)
    out: list[float] = [0.0] * n
    for i in range(n):
        prev = out[i - 1] if i > 0 else 0.0
        if i > 0 and values[i] == values[i - 1]:
            out[i] = 0.0
        elif i > 0 and values[i] > values[i - 1]:
            out[i] = 1.0 if prev <= 0 else prev + 1.0
        else:
            # i == 0 or values[i] < values[i-1]  (first bar falls here -> -1)
            out[i] = -1.0 if prev >= 0 else prev - 1.0
    return out


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


def fisher_transform(candles: list, length: int = 9) -> dict[str, list]:
    """Fisher Transform (openalgo-charts parity).

    Mid = hl2, clamped recursive normalized position then inverse hyperbolic
    tangent.  Flat or warming-up windows emit None and reset the recursion
    (``nz`` reads 0).  Trigger is ``fisher[1]`` (lag 1).

    Returns dict keyed ``fisher`` / ``trigger``.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    n = len(candles)
    if n == 0:
        return {"fisher": [], "trigger": []}
    length = int(length)
    mid = [(_to_float(c.ohlc.high.value) + _to_float(c.ohlc.low.value)) / 2.0 for c in candles]
    hi = _highest(mid, length)
    lo = _lowest(mid, length)
    fisher: list[float | None] = [None] * n
    prev_value = 0.0
    prev_fish = 0.0
    for i in range(n):
        h = hi[i]
        lo_v = lo[i]
        if h is None or lo_v is None:
            prev_value = 0.0
            prev_fish = 0.0
            continue
        span = h - lo_v
        if span == 0:
            prev_value = 0.0
            prev_fish = 0.0
            continue
        raw = 0.66 * ((mid[i] - lo_v) / span - 0.5) + 0.67 * prev_value
        if raw > 0.99:
            value = 0.999
        elif raw < -0.99:
            value = -0.999
        else:
            value = raw
        # 0.5*ln((1+v)/(1-v)) + 0.5*prevFish
        fish = 0.5 * math.log((1 + value) / (1 - value)) + 0.5 * prev_fish
        fisher[i] = fish
        prev_value = value
        prev_fish = fish
    trigger: list[float | None] = [None] * n
    for i in range(1, n):
        trigger[i] = fisher[i - 1]
    return {"fisher": fisher, "trigger": trigger}


def chande_momentum(candles: list, length: int = 9) -> dict[str, list]:
    """Chande Momentum Oscillator (openalgo-charts parity).

    ``100*(sumUp - sumDown)/(sumUp + sumDown)`` where sums are rolling sums of
    gains/losses over ``length``.  First value at index ``length`` (needs
    ``length`` real changes); flat windows emit None.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    n = len(candles)
    if n == 0:
        return {"cmo": []}
    length = int(length)
    closes = [_to_float(c.ohlc.close.value) for c in candles]
    momm = _change(closes)
    # gains/losses over n-1 changes (bar 1..n-1)
    gains = [0.0] * max(0, n - 1)
    losses = [0.0] * max(0, n - 1)
    for i in range(1, n):
        m = momm[i]
        assert m is not None
        gains[i - 1] = m if m >= 0 else 0.0
        losses[i - 1] = -m if m < 0 else 0.0
    sum_up = _rolling_sum(gains, length)
    sum_down = _rolling_sum(losses, length)
    out: list[float | None] = [None] * n
    for i in range(len(sum_up)):
        su = sum_up[i]
        sd = sum_down[i]
        if su is None or sd is None:
            continue
        total = su + sd
        out[i + 1] = None if total == 0 else (100.0 * (su - sd)) / total
    return {"cmo": out}


def connors_rsi(
    candles: list,
    lenrsi: int = 3,
    lenupdown: int = 2,
    lenroc: int = 100,
) -> dict[str, list]:
    """Connors RSI (openalgo-charts parity).

    Mean of RSI(close, lenrsi), RSI(streak, lenupdown), and
    percentRank(ROC1, lenroc).  First print at index ``lenroc + 1`` on
    defaults (101).  Returns dict keyed ``crsi`` plus constant bands
    ``bandHigh``/``bandLow`` (70/30).
    """
    if lenrsi <= 0 or lenupdown <= 0 or lenroc <= 0:
        raise ValueError("lengths must be positive")
    n = len(candles)
    if n == 0:
        return {"crsi": [], "bandHigh": [], "bandLow": []}
    lenrsi = int(lenrsi)
    lenupdown = int(lenupdown)
    lenroc = int(lenroc)
    closes = [_to_float(c.ohlc.close.value) for c in candles]
    price_rsi = rsi(closes, lenrsi)
    streak = connors_streak(closes)
    streak_rsi = rsi(streak, lenupdown)
    # ROC(1) from indicators.py (None at 0), then percentRank over slice(1)
    returns_full: list[float | None] = roc(closes, 1)  # type: ignore[assignment]
    # roc returns list[float|None]; keep as is
    tail: list[float | None] = returns_full[1:] if n > 1 else []
    ranked_tail = _percent_rank(tail, lenroc)
    rank: list[float | None] = [None] * n
    for i, v in enumerate(ranked_tail):
        rank[i + 1] = v
    out: list[float | None] = [None] * n
    for i in range(n):
        a = price_rsi[i]
        b = streak_rsi[i]
        c_ = rank[i]
        if a is None or b is None or c_ is None:
            continue
        out[i] = (a + b + c_) / 3.0
    band_high = [70.0] * n
    band_low = [30.0] * n
    return {"crsi": out, "bandHigh": band_high, "bandLow": band_low}


def balance_of_power(candles: list) -> dict[str, list]:
    """Balance of Power (openalgo-charts parity).

    ``(close - open) / (high - low)``; zero-range bars emit None.
    """
    n = len(candles)
    if n == 0:
        return {"bop": []}
    out: list[float | None] = []
    for c in candles:
        h = _to_float(c.ohlc.high.value)
        lo = _to_float(c.ohlc.low.value)
        rng = h - lo
        if rng == 0:
            out.append(None)
        else:
            o = _to_float(c.ohlc.open.value)
            cl = _to_float(c.ohlc.close.value)
            out.append((cl - o) / rng)
    return {"bop": out}


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------

SPEC_FISHER_TRANSFORM = IndicatorSpec(
    id="fisher-transform",
    name="Fisher Transform",
    category="Momentum",
    placement="pane",
    params=(("length", "int", 9),),
    plots=(
        ("fisher", "line", "Fisher"),
        ("trigger", "line", "Trigger"),
    ),
    levels=(
        {"value": 1.5},
        {"value": 0.75},
        {"value": 0},
        {"value": -0.75},
        {"value": -1.5},
    ),
    fn=fisher_transform,
)

SPEC_CHANDE_MOMENTUM = IndicatorSpec(
    id="chande-momentum",
    name="Chande Momentum Oscillator",
    category="Momentum",
    placement="pane",
    params=(("length", "int", 9),),
    plots=(("cmo", "line", "Chande MO"),),
    levels=({"value": 0},),
    fn=chande_momentum,
)

SPEC_CONNORS_RSI = IndicatorSpec(
    id="connors-rsi",
    name="Connors RSI",
    category="Momentum",
    placement="pane",
    params=(
        ("lenrsi", "int", 3),
        ("lenupdown", "int", 2),
        ("lenroc", "int", 100),
    ),
    plots=(("crsi", "line", "CRSI"),),
    levels=(
        {"value": 70},
        {"value": 50},
        {"value": 30},
    ),
    fn=connors_rsi,
)

SPEC_BALANCE_OF_POWER = IndicatorSpec(
    id="balance-of-power",
    name="Balance of Power",
    category="Momentum",
    placement="pane",
    params=(),
    plots=(("bop", "line", "BOP"),),
    levels=({"value": 0},),
    fn=balance_of_power,
)
