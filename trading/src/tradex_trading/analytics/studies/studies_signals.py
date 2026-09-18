"""Complex study indicators — openalgo-charts parity (signals.ts /
momentum.ts / wavetrend.ts).

Batch 5 task 4: the signal-bearing / complex studies:
- RSI Divergence (``RSI_DIVERGENCE`` in signals.ts)
- Trend Strength Index (``TREND_STRENGTH_INDEX`` in signals.ts)
- Williams Fractals (``WILLIAMS_FRACTALS`` in signals.ts)
- Williams Vix Fix (``WILLIAMS_VIX_FIX`` in momentum.ts)
- WaveTrend Pro (``WAVETREND`` in wavetrend.ts)

Shared helpers (``sma``, ``ema``, ``rsi``, ``_sma_seeded_ema``,
``_ema_of_gapped``, ``_to_float``, ...) come from ``indicators.py`` and are
never redefined.  The study-specific primitives below (pivot detection,
``barsSince`` / ``valueWhen`` semantics, NaN-counting ``sma``, population
``stdev``, rolling max/min) mirror the reference ``calc.ts`` exactly and are
kept private to this module, as in the sibling study modules.

Parity notes
- ``_sma_skip_none`` mirrors the TS ``sma`` in calc.ts: a running sum that
  never absorbs a non-finite value, blanking any window that holds one.
  ``_sma_of_gapped`` is ``sma`` applied to a warmup-gapped series through the
  reference ``fromFirstValue`` (slice the live tail, smooth, re-pad).
- ``_stdev`` is the reference population standard deviation over the SMA mean.
- ``_pivot_high`` / ``_pivot_low`` confirm a pivot ``right`` bars after it
  happens and place the answer on the confirming bar, strict on both sides.
- ``_bars_since`` / ``_value_when`` follow calc.ts: bars since the last true
  flag (None before the first), and the ``occurrence``-th most recent source
  reading at a true flag.
- The divergence studies' signal columns (``bull``/``bear``/``hidden*``,
  ``upFractal``/``downFractal``) are marker payloads, not plots, so the goldens
  carry only the plotted columns; the extra columns are still returned for
  faithfulness to the TS descriptors.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import cast

from tradex_trading.analytics.indicators import (
    IndicatorSpec,
    LEGACY_PARAM_ALIASES,
    _bars_since,
    _ema_of_gapped,
    _shift,
    _sma_skip_none,
    _to_float,
    rsi,
)

LEGACY_PARAM_ALIASES["rsi-divergence"] = {"lb_r": "lbR", "lb_l": "lbL"}
LEGACY_PARAM_ALIASES["wavetrend"] = {"sig_len": "sigLen"}

# ---------------------------------------------------------------------------
# Reference calc.ts primitives (never exported; module-private by convention)
# ---------------------------------------------------------------------------


def _sma_of_gapped(values: list[float | None], period: int) -> list[float | None]:
    """``fromFirstValue`` + ``sma``: smooth the tail that begins at the first
    real value, then pad the answer back to full length (calc.ts / wavetrend.ts
    ``fromFirstValue`` with the ``sma`` kernel)."""
    n = len(values)
    out: list[float | None] = [None] * n
    start = 0
    while start < n and values[start] is None:
        start += 1
    if start >= n:
        return out
    out[start:] = _sma_skip_none(values[start:], period)
    return out


def _stdev(values: list[float | None], period: int) -> list[float | None]:
    """Rolling population standard deviation (calc.ts ``stdev``).

    The mean comes from the NaN-counting ``sma``; any window holding a
    non-finite value blanks the slot (``NaN - mean`` poisons the sum).
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    means = _sma_skip_none(values, period)
    for i in range(period - 1, n):
        m = means[i]
        if m is None:
            continue
        acc = 0.0
        for k in range(period):
            d = cast(float, values[i - k]) - m
            acc += d * d
        out[i] = math.sqrt(acc / period)
    return out


def _highest_skip(values: Sequence[float | None], period: int) -> list[float | None]:
    """Rolling maximum (calc.ts ``highest``).

    Non-finite entries lose every comparison, so they are simply skipped; an
    all-blank window yields ``None`` (the reference leaves ``-Infinity`` which
    ``nulls`` then converts to null).
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0:
        return out
    for i in range(period - 1, n):
        best: float | None = None
        for k in range(period):
            v = values[i - k]
            if v is not None and (best is None or v > best):
                best = v
        out[i] = best
    return out


def _lowest_skip(values: list[float | None], period: int) -> list[float | None]:
    """Rolling minimum (calc.ts ``lowest``); non-finite entries are skipped."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0:
        return out
    for i in range(period - 1, n):
        best: float | None = None
        for k in range(period):
            v = values[i - k]
            if v is not None and (best is None or v < best):
                best = v
        out[i] = best
    return out


def _shift_flags(flags: list[bool], k: int) -> list[bool]:
    """``shift`` for a condition series; out-of-range flags read as False."""
    n = len(flags)
    out = [False] * n
    for i in range(k, n):
        out[i] = flags[i - k]
    return out


def _pivot_high(values: list[float | None], left: int, right: int) -> list[float | None]:
    return _pivot(values, left, right, True)


def _pivot_low(values: list[float | None], left: int, right: int) -> list[float | None]:
    return _pivot(values, left, right, False)


def _pivot(
    values: list[float | None], left: int, right: int, want_high: bool
) -> list[float | None]:
    """Pivot extreme confirmed ``right`` bars after it happens (calc.ts).

    The answer lands on the confirming bar and holds the value ``right`` bars
    back.  Comparisons are strict on both sides, so a tie is not a pivot; a
    missing neighbour fails the pivot rather than being skipped.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    for i in range(left + right, n):
        at = i - right
        v = values[at]
        if v is None:
            continue
        ok = True
        for k in range(1, left + 1):
            if not ok:
                break
            o = values[at - k]
            if o is None or (o >= v if want_high else o <= v):
                ok = False
        for k in range(1, right + 1):
            if not ok:
                break
            o = values[at + k]
            if o is None or (o >= v if want_high else o <= v):
                ok = False
        if ok:
            out[i] = v
    return out


def _value_when(
    cond: list[bool], source: list[float | None], occurrence: int
) -> list[float | None]:
    """The ``occurrence``-th most recent source reading at a true flag (calc.ts).

    Occurrence 0 is the latest (counting the current bar).
    """
    n = len(cond)
    out: list[float | None] = [None] * n
    hits: list[int] = []
    for i in range(n):
        if cond[i]:
            hits.append(i)
        at = len(hits) - 1 - occurrence
        if at >= 0:
            out[i] = source[hits[at]]
    return out


def _correlation(a: list[float], b: list[float], period: int) -> list[float | None]:
    """Pearson correlation of two series over ``period`` (calc.ts).

    The covariance is ``period*sab - sa*sb`` over a zero-degenerate
    denominator; when either variance term is negative (only possible through
    float rounding) the reference reads NaN and so do we.
    """
    n = len(a)
    out: list[float | None] = [None] * n
    if period <= 1 or n < period:
        return out
    for i in range(period - 1, n):
        sa = 0.0
        sb = 0.0
        saa = 0.0
        sbb = 0.0
        sab = 0.0
        for k in range(period):
            x = a[i - k]
            y = b[i - k]
            sa += x
            sb += y
            saa += x * x
            sbb += y * y
            sab += x * y
        cov = period * sab - sa * sb
        r1 = period * saa - sa * sa
        r2 = period * sbb - sb * sb
        if r1 < 0 or r2 < 0:
            continue
        den = math.sqrt(r1) * math.sqrt(r2)
        if den == 0:
            continue
        out[i] = cov / den
    return out


def _is_fractal(values: list[float], at: int, n: int, want_high: bool) -> bool:
    """The five-variant fractal test from signals.ts.

    The newer side is strict: all ``n`` bars after the candidate must be past
    it.  The older side ORs five flags whose only difference is how many bars
    immediately before the candidate may merely *equal* it (zero through four)
    before a run of ``n`` strictly lower bars is demanded.  A bar off either
    end of the series has no value, so a missing neighbour fails the variant.
    """
    v = values[at]
    if v is None:
        return False

    def beyond(j: int) -> bool:
        if 0 <= j < len(values) and values[j] is not None:
            return values[j] < v if want_high else values[j] > v
        return False

    def level(j: int) -> bool:
        if 0 <= j < len(values) and values[j] is not None:
            return values[j] <= v if want_high else values[j] >= v
        return False

    for k in range(1, n + 1):
        if not beyond(at + k):
            return False
    for plateau in range(5):
        ok = True
        for k in range(1, plateau + 1):
            if not ok:
                break
            if not level(at - k):
                ok = False
        for k in range(1, n + 1):
            if not ok:
                break
            if not beyond(at - plateau - k):
                ok = False
        if ok:
            return True
    return False


def _hlc3(candles: list) -> list[float]:
    """Typical-ish source used by WaveTrend: ``(high + low + close) / 3``."""
    return [
        (
            _to_float(c.ohlc.high.value)
            + _to_float(c.ohlc.low.value)
            + _to_float(c.ohlc.close.value)
        )
        / 3.0
        for c in candles
    ]


# ---------------------------------------------------------------------------
# RSI Divergence
# ---------------------------------------------------------------------------


def rsi_divergence(
    candles: list, length: int = 14, lb_r: int = 5, lb_l: int = 5
) -> dict[str, list[float | None]]:
    """RSI, plus a labelled plate wherever a fresh RSI pivot disagrees with the
    price pivot beside it.

    Matches ``RSI_DIVERGENCE.calc`` in signals.ts: the RSI is the base-bundle
    Wilder RSI; each signal class compares the newest oscillator pivot with the
    one before it (``valueWhen(..., 1)``) and the price at both, gated by a
    range of bars since the previous pivot.  The signal columns are marker
    payloads in the reference and are returned for parity; the plotted column
    is ``rsi``.
    """
    n = len(candles)
    lb_r = int(lb_r)
    lb_l = int(lb_l)
    lower = 5.0
    upper = 60.0
    want_bull = True
    want_hidden_bull = False
    want_bear = True
    want_hidden_bear = False

    closes = [_to_float(c.ohlc.close.value) for c in candles]
    lows = [_to_float(c.ohlc.low.value) for c in candles]
    highs = [_to_float(c.ohlc.high.value) for c in candles]
    osc = rsi(closes, int(length))

    bull: list[float | None] = [None] * n
    hidden_bull: list[float | None] = [None] * n
    bear: list[float | None] = [None] * n
    hidden_bear: list[float | None] = [None] * n

    pl_found = [v is not None for v in _pivot_low(osc, lb_l, lb_r)]
    ph_found = [v is not None for v in _pivot_high(osc, lb_l, lb_r)]
    osc_at = _shift(osc, lb_r)
    low_at = _shift(lows, lb_r)
    high_at = _shift(highs, lb_r)
    since_pl = _bars_since(_shift_flags(pl_found, 1))
    since_ph = _bars_since(_shift_flags(ph_found, 1))
    prev_osc_low = _value_when(pl_found, osc_at, 1)
    prev_price_low = _value_when(pl_found, low_at, 1)
    prev_osc_high = _value_when(ph_found, osc_at, 1)
    prev_price_high = _value_when(ph_found, high_at, 1)

    for i in range(n):
        # The signal belongs to the pivot bar, `lb_r` back from the confirmation.
        at = i - lb_r
        if at < 0:
            continue
        oa = osc_at[i]

        if pl_found[i]:
            sp = since_pl[i]
            in_range = sp is not None and lower <= sp <= upper
            pol = prev_osc_low[i]
            pa = low_at[i]
            ppl = prev_price_low[i]
            if (
                in_range
                and oa is not None
                and pol is not None
                and pa is not None
                and ppl is not None
            ):
                if want_bull and oa > pol and pa < ppl:
                    bull[at] = oa
                if want_hidden_bull and oa < pol and pa > ppl:
                    hidden_bull[at] = oa

        if ph_found[i]:
            sp = since_ph[i]
            in_range = sp is not None and lower <= sp <= upper
            poh = prev_osc_high[i]
            pa = high_at[i]
            pph = prev_price_high[i]
            if (
                in_range
                and oa is not None
                and poh is not None
                and pa is not None
                and pph is not None
            ):
                if want_bear and oa < poh and pa > pph:
                    bear[at] = oa
                if want_hidden_bear and oa > poh and pa < pph:
                    hidden_bear[at] = oa

    return {
        "rsi": osc,
        "bull": bull,
        "hiddenBull": hidden_bull,
        "bear": bear,
        "hiddenBear": hidden_bear,
    }


# ---------------------------------------------------------------------------
# Trend Strength Index
# ---------------------------------------------------------------------------


def trend_strength_index(candles: list, length: int = 14) -> dict[str, list[float | None]]:
    """Pearson correlation between price and the passage of time.

    Matches ``TREND_STRENGTH_INDEX.calc`` in signals.ts: ``correlation(close,
    index, length)`` where ``index`` is the bar position within the supplied
    bars.  A flat window (zero variance) yields None, exactly as the reference
    NaN.
    """
    n = len(candles)
    closes = [_to_float(c.ohlc.close.value) for c in candles]
    index = [float(i) for i in range(n)]
    return {"tsi": _correlation(closes, index, int(length))}


# ---------------------------------------------------------------------------
# Williams Fractals
# ---------------------------------------------------------------------------


def williams_fractals(candles: list, periods: int = 2) -> dict[str, list[float | None]]:
    """Pivot highs and lows by the five-variant fractal test.

    Matches ``WILLIAMS_FRACTALS.calc`` in signals.ts: the ``fractals`` column
    is all-null (it exists only to own the marker layer); the up/down signal
    columns carry the extreme price each shape is anchored to, placed on the
    candidate bar.  A fractal is only known ``periods`` bars after it happens,
    so the last ``periods`` bars can never carry one.
    """
    n = len(candles)
    periods = max(2, int(periods))
    fractals: list[float | None] = [None] * n
    up_fractal: list[float | None] = [None] * n
    down_fractal: list[float | None] = [None] * n

    highs = [_to_float(c.ohlc.high.value) for c in candles]
    lows = [_to_float(c.ohlc.low.value) for c in candles]
    for i in range(n):
        if _is_fractal(highs, i, periods, True):
            up_fractal[i] = highs[i]
        if _is_fractal(lows, i, periods, False):
            down_fractal[i] = lows[i]
    return {"fractals": fractals, "upFractal": up_fractal, "downFractal": down_fractal}


# ---------------------------------------------------------------------------
# Williams Vix Fix
# ---------------------------------------------------------------------------


def williams_vix_fix(
    candles: list,
    pd: int = 22,
    bbl: int = 20,
    mult: float = 2.0,
    lb: int = 50,
    ph: float = 0.85,
    pl: float = 1.01,
) -> dict[str, list[float | None]]:
    """A synthetic VIX from price alone: how far the low sits below the highest
    close of the lookback, as a percentage.

    Matches ``WILLIAMS_VIX_FIX.calc`` in momentum.ts: ``wvf = (highest(close,
    pd) - low) / highest(close, pd) * 100``; the band/range columns are its
    Bollinger upper band (``sma`` + ``mult`` population ``stdev`` over ``bbl``)
    and the ``ph`` / ``pl`` percentiles of the ``lb``-bar range.  The reference
    only plots those columns while its ``hp`` / ``sd`` show toggles are on,
    which are not backend params, so the range/band columns return null here
    exactly as the golden (toggles off) expects.
    """
    n = len(candles)
    pd = int(pd)
    bbl = int(bbl)
    lb = int(lb)
    closes = [_to_float(c.ohlc.close.value) for c in candles]
    lows = [_to_float(c.ohlc.low.value) for c in candles]

    highest_close = _highest_skip(closes, pd)
    wvf: list[float | None] = [None] * n
    for i in range(n):
        hc = highest_close[i]
        if hc is not None and hc != 0:
            wvf[i] = ((hc - lows[i]) / hc) * 100.0

    dev = _stdev(wvf, bbl)
    mid = _sma_skip_none(wvf, bbl)
    highest_wvf = _highest_skip(wvf, lb)
    lowest_wvf = _lowest_skip(wvf, lb)

    show_range = False
    show_band = False
    range_high: list[float | None] = [None] * n
    range_low: list[float | None] = [None] * n
    upper_band: list[float | None] = [None] * n
    for i in range(n):
        m = mid[i]
        d = dev[i]
        up = m + mult * d if m is not None and d is not None else None
        hw = highest_wvf[i]
        lw = lowest_wvf[i]
        rh = hw * ph if hw is not None else None
        rl = lw * pl if lw is not None else None
        if show_band and up is not None:
            upper_band[i] = up
        if show_range and rh is not None:
            range_high[i] = rh
        if show_range and rl is not None:
            range_low[i] = rl

    return {
        "wvf": wvf,
        "range_high": range_high,
        "range_low": range_low,
        "upper_band": upper_band,
    }


# ---------------------------------------------------------------------------
# WaveTrend Pro
# ---------------------------------------------------------------------------


def wavetrend(
    candles: list, n1: int = 10, n2: int = 21, sig_len: int = 4
) -> dict[str, list[float | None]]:
    """WaveTrend oscillator, signal line and momentum histogram.

    Matches ``WAVETREND.calc`` in wavetrend.ts: the channel is the source's
    distance from its own SMA-seeded EMA, scaled by ``0.015`` times the
    smoothed mean absolute deviation; ``wt1`` re-smooths it (``n2``), ``wt2``
    SMA-smoothes ``wt1`` (``sig_len``) and ``mom = wt1 - wt2``.  Every chained
    smoother goes through ``fromFirstValue`` so a leading warmup gap never
    poisons a recursion; with the defaults wt1 prints from bar 38, wt2/mom
    from bar 41.
    """
    n = len(candles)
    n1 = int(n1)
    n2 = int(n2)
    sig_len = int(sig_len)

    ap = _hlc3(candles)
    esa = _ema_of_gapped(ap, n1)
    absdev_in: list[float | None] = [
        None if esa[i] is None else abs(cast(float, ap[i]) - cast(float, esa[i])) for i in range(n)
    ]
    abs_dev = _ema_of_gapped(absdev_in, n1)

    ci: list[float | None] = [None] * n
    for i in range(n):
        dv = abs_dev[i]
        if dv is None:
            continue
        ci[i] = 0.0 if dv == 0 else (cast(float, ap[i]) - cast(float, esa[i])) / (0.015 * dv)

    wt1 = _ema_of_gapped(ci, n2)
    wt2 = _sma_of_gapped(wt1, sig_len)
    mom: list[float | None] = [
        None if wt1[i] is None or wt2[i] is None else cast(float, wt1[i]) - cast(float, wt2[i]) for i in range(n)
    ]
    return {"mom": mom, "wt1": wt1, "wt2": wt2}


# ---------------------------------------------------------------------------
# Specs — ready for registry import; NOT registered here (merge task wires).
# ---------------------------------------------------------------------------


def _fn_rsi_divergence(candles: list, length: int, lbR: int, lbL: int) -> dict[str, list]:
    return rsi_divergence(candles, int(length), int(lbR), int(lbL))


def _fn_trend_strength_index(candles: list, length: int) -> dict[str, list]:
    return trend_strength_index(candles, int(length))


def _fn_williams_fractals(candles: list, periods: int) -> dict[str, list]:
    return williams_fractals(candles, int(periods))


def _fn_williams_vix_fix(
    candles: list, pd: int, bbl: int, mult: float, lb: int, ph: float, pl: float
) -> dict[str, list]:
    return williams_vix_fix(candles, int(pd), int(bbl), float(mult), int(lb), float(ph), float(pl))


def _fn_wavetrend(candles: list, n1: int, n2: int, sigLen: int) -> dict[str, list]:
    return wavetrend(candles, int(n1), int(n2), int(sigLen))


# ---------------------------------------------------------------------------
# Consolidation Breakout
# ---------------------------------------------------------------------------


def consolidation_breakout(candles: list) -> dict[str, list]:
    """Inside-range rails with body-breakout signals (openalgo-charts parity).

    Matches ``CONSOLIDATION_BREAKOUT`` (src/indicators/signals.ts): the range
    mother is the most recent non-inside bar; a bar is inside when its whole
    body sits inside the mother's high/low; breakouts need age > 1 and <=
    250 and trigger on body extremes beyond the rail. Rails print on held
    bars only (None on breakout bars so the renderer breaks the line); bar 0
    seeds its own range. Hidden ``insideAge`` supports the tint hook.
    """
    from tradex_trading.analytics.indicators import _to_float

    n = len(candles)
    opens = [_to_float(c.ohlc.open.value) for c in candles]
    highs = [_to_float(c.ohlc.high.value) for c in candles]
    lows = [_to_float(c.ohlc.low.value) for c in candles]
    closes = [_to_float(c.ohlc.close.value) for c in candles]
    range_high: list[float | None] = [None] * n
    range_low: list[float | None] = [None] * n
    break_up: list[float | None] = [None] * n
    break_down: list[float | None] = [None] * n
    inside_age: list[float | None] = [None] * n
    main = 0
    for i in range(n):
        body_top = max(opens[i], closes[i])
        body_bottom = min(opens[i], closes[i])
        mother_high = highs[main]
        mother_low = lows[main]
        age = i - main
        if age > 1 and age <= 250:
            if body_top > mother_high:
                break_up[i] = mother_high
            if body_bottom < mother_low:
                break_down[i] = mother_low
        inside = (
            i > 1
            and mother_low <= body_bottom <= mother_high
            and mother_low <= body_top <= mother_high
        )
        prev = main
        main = main if inside else i
        if main == prev:
            range_high[i] = highs[main]
            range_low[i] = lows[main]
        held = i - main
        if held > 0:
            inside_age[i] = float(held)
    return {
        "rangeHigh": range_high,
        "rangeLow": range_low,
        "breakUp": break_up,
        "breakDown": break_down,
        "insideAge": inside_age,
    }


def _fn_consolidation_breakout(candles: list, markbreakout: bool = True, colorinside: bool = True) -> dict[str, list]:
    # markbreakout/colorinside drive markers/barColors hooks in the engine;
    # the backend always computes the full signal set (presentation-only).
    return consolidation_breakout(candles)


SPEC_CONSOLIDATION_BREAKOUT = IndicatorSpec(
    id="consolidation-breakout",
    name="Consolidation Breakout",
    category="Trend",
    placement="overlay",
    params=(("markbreakout", "bool", True), ("colorinside", "bool", True)),
    plots=(
        ("rangeHigh", "line", "Range High"),
        ("rangeLow", "line", "Range Low"),
        ("breakUp", "line", "Break Up"),
        ("breakDown", "line", "Break Down"),
        ("insideAge", "line", "Inside Age"),
    ),
    fn=_fn_consolidation_breakout,
)


SPEC_RSI_DIVERGENCE = IndicatorSpec(
    id="rsi-divergence",
    name="RSI Divergence Indicator",
    category="Momentum",
    placement="pane",
    params=(
        ("length", "int", 14),
        ("lbR", "int", 5),
        ("lbL", "int", 5),
    ),
    plots=(("rsi", "line", "RSI"),),
    levels=({"value": 70}, {"value": 50}, {"value": 30}),
    fn=_fn_rsi_divergence,
)

SPEC_TREND_STRENGTH_INDEX = IndicatorSpec(
    id="trend-strength-index",
    name="Trend Strength Index",
    category="Trend",
    placement="pane",
    params=(("length", "int", 14),),
    plots=(("tsi", "line", "Trend Strength Index"),),
    levels=({"value": 1}, {"value": 0}, {"value": -1}),
    fn=_fn_trend_strength_index,
)

SPEC_WILLIAMS_FRACTALS = IndicatorSpec(
    id="williams-fractals",
    name="Williams Fractals",
    category="Trend",
    placement="overlay",
    params=(("periods", "int", 2),),
    plots=(("fractals", "line", "Fractals"),),
    fn=_fn_williams_fractals,
)

SPEC_WILLIAMS_VIX_FIX = IndicatorSpec(
    id="williams-vix-fix",
    name="William VIX FIX",
    category="Volatility",
    placement="pane",
    params=(
        ("pd", "int", 22),
        ("bbl", "int", 20),
        ("mult", "float", 2.0),
        ("lb", "int", 50),
        ("ph", "float", 0.85),
        ("pl", "float", 1.01),
    ),
    plots=(
        ("wvf", "histogram", "Williams Vix Fix"),
        ("range_high", "line", "Range High Percentile"),
        ("range_low", "line", "Range Low Percentile"),
        ("upper_band", "line", "Upper Band"),
    ),
    fn=_fn_williams_vix_fix,
)

SPEC_WAVETREND = IndicatorSpec(
    id="wavetrend",
    name="WaveTrend Pro",
    category="Momentum",
    placement="pane",
    params=(
        ("n1", "int", 10),
        ("n2", "int", 21),
        ("sigLen", "int", 4),
    ),
    plots=(
        ("mom", "histogram", "Momentum"),
        ("wt1", "line", "WT1"),
        ("wt2", "line", "WT2"),
    ),
    levels=({"value": 60}, {"value": 53}, {"value": 0}, {"value": -53}, {"value": -60}),
    fn=_fn_wavetrend,
)

SPECS = [
    SPEC_RSI_DIVERGENCE,
    SPEC_TREND_STRENGTH_INDEX,
    SPEC_WILLIAMS_FRACTALS,
    SPEC_WILLIAMS_VIX_FIX,
    SPEC_WAVETREND,
    SPEC_CONSOLIDATION_BREAKOUT,
]

__all__ = [
    "consolidation_breakout",
    "rsi_divergence",
    "trend_strength_index",
    "williams_fractals",
    "williams_vix_fix",
    "wavetrend",
    "SPEC_CONSOLIDATION_BREAKOUT",
    "SPEC_RSI_DIVERGENCE",
    "SPEC_TREND_STRENGTH_INDEX",
    "SPEC_WILLIAMS_FRACTALS",
    "SPEC_WILLIAMS_VIX_FIX",
    "SPEC_WAVETREND",
    "SPECS",
]
