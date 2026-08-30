"""Volume indices — NVI, PVI, PVO, Mass Index, KST, Klinger Oscillator.

Ported from openalgo-charts TS sources for parity:
- ``src/indicators/indices.ts``  — NVI, PVI, PVO, MASS_INDEX
- ``src/indicators/adaptive.ts`` — KLINGER_OSCILLATOR, KNOW_SURE_THING
- ``src/indicators/calc.ts``     — smaSeededEma, roc, change, rollingSum, sma

Helpers (``_sma_seeded_ema``, ``_ema_of_gapped``, ``_change``, ``roc``,
``sma``, ``IndicatorSpec``, etc.) are imported from
``tradex_trading.analytics.indicators``.
"""

from __future__ import annotations

from typing import Any

from tradex_trading.analytics.indicators import (
    IndicatorSpec,
    _change,
    _ema_of_gapped,
    _isfinite,
    _rolling_sum,
    _sma_seeded_ema,
    _to_float,
    roc,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_float_safe(v: Any) -> float:
    """Like ``_to_float`` but returns 0.0 for None (``nz(volume)`` semantics)."""
    if v is None:
        return 0.0
    return _to_float(v)


def _sma_skip_nan(values: list[float], period: int) -> list[float | None]:
    """SMA that skips NaN/None entries — matches TS ``sma`` (calc.ts:13).

    The Python ``sma`` in indicators.py does not tolerate None inputs.  This
    helper reproduces the TS behaviour: NaN slots are excluded from both the
    sum and the count, and the output is None whenever the window holds any
    non-finite entry.
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


def _smooth_runs(
    values: list[float],
    period: int,
    smoother: Any,
) -> list[float | None]:
    """Process gapless runs of finite values independently through *smoother*.

    Matches TS ``smoothRuns`` (indices.ts:56).  The smoother receives a
    clean ``list[float]`` slice (no leading Nones) and must return a
    same-length list.  Only the leading gap is relevant for every indicator
    in this module, so ``smoother`` is always ``_sma_seeded_ema`` or
    ``_rolling_sum``.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    i = 0
    while i < n:
        if not _isfinite(values[i]):
            i += 1
            continue
        end = i
        while end < n and _isfinite(values[end]):
            end += 1
        run = smoother(values[i:end], period)
        for k in range(len(run)):
            out[i + k] = run[k]
        i = end
    return out


# ---------------------------------------------------------------------------
# NVI / PVI — shared volume-index body
# ---------------------------------------------------------------------------


def _volume_index(
    candles: list,
    on: str,
) -> list[float]:
    """Ratcheting price-compounding index (``on='falling'`` → NVI, ``'rising'`` → PVI).

    Matches TS ``volumeIndex`` (indices.ts:88).  Bar 0 is the base at 1.0;
    the result is scaled ×1000.
    """
    n = len(candles)
    out: list[float] = [0.0] * n
    index = 1.0
    for i in range(n):
        if i > 0:
            prev_close = _to_float(candles[i - 1].ohlc.close.value)
            vol_now = _to_float_safe(candles[i].volume.value)
            vol_prev = _to_float_safe(candles[i - 1].volume.value)
            moved = (vol_now < vol_prev) if on == "falling" else (vol_now > vol_prev)
            if moved and prev_close != 0 and _isfinite(prev_close):
                cur_close = _to_float(candles[i].ohlc.close.value)
                if _isfinite(cur_close):
                    index *= cur_close / prev_close
        out[i] = index * 1000.0
    return out


def nvi(
    candles: list,
    ma_length: int = 255,
) -> dict[str, list]:
    """Negative Volume Index.

    Compounds price changes on bars where volume fell; smoothed by an
    SMA-seeded EMA over ``ma_length`` bars.

    Returns ``{'nvi': [...], 'ema': [...]}``.
    """
    idx = _volume_index(candles, "falling")
    ema_line = _smooth_runs(idx, int(ma_length), _sma_seeded_ema)
    return {"nvi": idx, "ema": ema_line}


def pvi(
    candles: list,
    ma_length: int = 255,
) -> dict[str, list]:
    """Positive Volume Index.

    Compounds price changes on bars where volume rose; smoothed by an
    SMA-seeded EMA over ``ma_length`` bars.

    Returns ``{'pvi': [...], 'ema': [...]}``.
    """
    idx = _volume_index(candles, "rising")
    ema_line = _smooth_runs(idx, int(ma_length), _sma_seeded_ema)
    return {"pvi": idx, "ema": ema_line}


# ---------------------------------------------------------------------------
# PVO — Percentage Volume Oscillator (MACD on volume)
# ---------------------------------------------------------------------------


def pvo(
    candles: list,
    fast_length: int = 12,
    slow_length: int = 26,
    signal_length: int = 9,
) -> dict[str, list]:
    """Percentage Volume Oscillator.

    MACD construction on volume, expressed as a percentage of the slow
    average.  Both oscillator legs default to SMA-seeded EMA (matching
    ``oscType = 'EMA'`` in the reference).

    Returns ``{'hist': [...], 'pvo': [...], 'signal': [...]}``.
    """
    volumes = [_to_float_safe(c.volume.value) for c in candles]
    n = len(volumes)
    fast = _sma_seeded_ema(volumes, int(fast_length))
    slow = _sma_seeded_ema(volumes, int(slow_length))

    pvo_line: list[float | None] = [None] * n
    for i in range(n):
        sv = slow[i]
        if sv is not None and sv != 0 and fast[i] is not None:
            pvo_line[i] = 100.0 * (fast[i] - sv) / sv

    signal_line = _smooth_runs(
        [v if v is not None else float("nan") for v in pvo_line],
        int(signal_length),
        _sma_seeded_ema,
    )
    hist: list[float | None] = [None] * n
    for i in range(n):
        pv = pvo_line[i]
        sg = signal_line[i]
        if pv is not None and sg is not None:
            hist[i] = pv - sg
    return {"hist": hist, "pvo": pvo_line, "signal": signal_line}


# ---------------------------------------------------------------------------
# Mass Index
# ---------------------------------------------------------------------------


def mass_index(
    candles: list,
    length: int = 10,
) -> dict[str, list]:
    """Mass Index — volatility expansion ratio over a rolling window.

    Matches TS ``MASS_INDEX.calc`` (indices.ts:293).  The two 9-bar EMA
    periods are hard-coded; only the sum window is exposed as ``length``.

    Returns ``{'mi': [...]}``.
    """
    span = [
        _to_float(c.ohlc.high.value) - _to_float(c.ohlc.low.value)
        for c in candles
    ]
    single = _sma_seeded_ema(span, 9)
    double = _smooth_runs(
        [v if v is not None else float("nan") for v in single],
        9,
        _sma_seeded_ema,
    )
    ratio: list[float] = [float("nan")] * len(candles)
    for i in range(len(candles)):
        s = single[i]
        d = double[i]
        if s is not None and d is not None and d != 0:
            ratio[i] = s / d

    finite_runs = [v for v in ratio if _isfinite(v)]
    if finite_runs:
        rolling = _rolling_sum(finite_runs, int(length))
        mi: list[float | None] = [None] * len(candles)
        idx = 0
        for i in range(len(candles)):
            if _isfinite(ratio[i]):
                mi[i] = rolling[idx] if idx < len(rolling) else None
                idx += 1
        return {"mi": mi}
    return {"mi": [None] * len(candles)}


# ---------------------------------------------------------------------------
# Know Sure Thing
# ---------------------------------------------------------------------------


def know_sure_thing(
    candles: list,
    roclen1: int = 10,
    roclen2: int = 15,
    roclen3: int = 20,
    roclen4: int = 30,
    smalen1: int = 10,
    smalen2: int = 10,
    smalen3: int = 10,
    smalen4: int = 15,
    siglen: int = 9,
) -> dict[str, list]:
    """Know Sure Thing — weighted sum of four smoothed ROC readings.

    Matches ``KNOW_SURE_THING.calc`` (adaptive.ts:340).  Each term is
    ``sma(roc(closes, rocLen), smaLen)``; weights are 1/2/3/4.

    Returns ``{'kst': [...], 'signal': [...]}``.
    """
    closes = [_to_float(c.ohlc.close.value) for c in candles]

    def term(rl: int, sl: int) -> list[float | None]:
        roc_vals = roc(closes, int(rl))
        roc_nan = [float("nan") if v is None else v for v in roc_vals]
        return _sma_skip_nan(roc_nan, int(sl))

    first = term(roclen1, smalen1)
    second = term(roclen2, smalen2)
    third = term(roclen3, smalen3)
    fourth = term(roclen4, smalen4)

    n = len(candles)
    kst: list[float | None] = [None] * n
    for i in range(n):
        a, b, c, d = first[i], second[i], third[i], fourth[i]
        if a is not None and b is not None and c is not None and d is not None:
            kst[i] = a + 2.0 * b + 3.0 * c + 4.0 * d

    signal = _sma_skip_nan(
        [float("nan") if v is None else v for v in kst],
        int(siglen),
    )
    return {"kst": kst, "signal": signal}


# ---------------------------------------------------------------------------
# Klinger Oscillator
# ---------------------------------------------------------------------------

KLINGER_FAST = 34
KLINGER_SLOW = 55
KLINGER_SIGNAL = 13


def klinger_oscillator(
    candles: list,
) -> dict[str, list]:
    """Klinger Oscillator — volume signed by typical-price direction.

    Matches ``KLINGER_OSCILLATOR.calc`` (adaptive.ts:280).  No user-tunable
    parameters; the three periods (34, 55, 13) are hard-coded in the
    reference definition.

    Returns ``{'kvo': [...], 'signal': [...]}``.
    """
    n = len(candles)
    hlc3 = [
        (
            _to_float(c.ohlc.high.value)
            + _to_float(c.ohlc.low.value)
            + _to_float(c.ohlc.close.value)
        )
        / 3.0
        for c in candles
    ]
    step = _change(hlc3, 1)

    signed: list[float] = [0.0] * n
    for i in range(n):
        volume = _to_float_safe(candles[i].volume.value)
        sv = step[i]
        # TS: NaN >= 0 → false → -volume for bar 0
        if sv is not None and sv >= 0:
            signed[i] = volume
        else:
            signed[i] = -volume

    fast = _sma_seeded_ema(signed, KLINGER_FAST)
    slow = _sma_seeded_ema(signed, KLINGER_SLOW)

    kvo: list[float | None] = [None] * n
    for i in range(n):
        f, s = fast[i], slow[i]
        if f is not None and s is not None:
            kvo[i] = f - s

    signal = _ema_of_gapped(kvo, KLINGER_SIGNAL)
    return {"kvo": kvo, "signal": signal}


# ---------------------------------------------------------------------------
# IndicatorSpec instances — wired into the registry by indicators.py
# ---------------------------------------------------------------------------


def _fn_nvi(candles: list, ma_length: int) -> dict[str, list]:
    return nvi(candles, int(ma_length))


def _fn_pvi(candles: list, ma_length: int) -> dict[str, list]:
    return pvi(candles, int(ma_length))


def _fn_pvo(
    candles: list,
    fast_length: int,
    slow_length: int,
    signal_length: int,
) -> dict[str, list]:
    return pvo(candles, int(fast_length), int(slow_length), int(signal_length))


def _fn_mass_index(candles: list, length: int) -> dict[str, list]:
    return mass_index(candles, int(length))


def _fn_know_sure_thing(
    candles: list,
    roclen1: int,
    roclen2: int,
    roclen3: int,
    roclen4: int,
    smalen1: int,
    smalen2: int,
    smalen3: int,
    smalen4: int,
    siglen: int,
) -> dict[str, list]:
    return know_sure_thing(
        candles,
        int(roclen1), int(roclen2), int(roclen3), int(roclen4),
        int(smalen1), int(smalen2), int(smalen3), int(smalen4),
        int(siglen),
    )


def _fn_klinger_oscillator(candles: list) -> dict[str, list]:
    return klinger_oscillator(candles)


SPEC_NVI = IndicatorSpec(
    id="nvi",
    name="Negative Volume Index",
    category="Volume",
    placement="pane",
    params=(("ma_length", "int", 255),),
    plots=(
        ("nvi", "line", "NVI"),
        ("ema", "line", "NVI EMA"),
    ),
    levels=({"value": 1000},),
    fn=_fn_nvi,
)

SPEC_PVI = IndicatorSpec(
    id="pvi",
    name="Positive Volume Index",
    category="Volume",
    placement="pane",
    params=(("ma_length", "int", 255),),
    plots=(
        ("pvi", "line", "PVI"),
        ("ema", "line", "PVI EMA"),
    ),
    levels=({"value": 1000},),
    fn=_fn_pvi,
)

SPEC_PVO = IndicatorSpec(
    id="pvo",
    name="Percentage Volume Oscillator",
    category="Volume",
    placement="pane",
    params=(
        ("fast_length", "int", 12),
        ("slow_length", "int", 26),
        ("signal_length", "int", 9),
    ),
    plots=(
        ("hist", "histogram", "Histogram"),
        ("pvo", "line", "PVO"),
        ("signal", "line", "Signal"),
    ),
    levels=({"value": 0},),
    fn=_fn_pvo,
)

SPEC_MASS_INDEX = IndicatorSpec(
    id="mass-index",
    name="Mass Index",
    category="Volatility",
    placement="pane",
    params=(("length", "int", 10),),
    plots=(("mi", "line", "Mass Index"),),
    fn=_fn_mass_index,
)

SPEC_KNOW_SURE_THING = IndicatorSpec(
    id="know-sure-thing",
    name="Know Sure Thing",
    category="Momentum",
    placement="pane",
    params=(
        ("roclen1", "int", 10),
        ("roclen2", "int", 15),
        ("roclen3", "int", 20),
        ("roclen4", "int", 30),
        ("smalen1", "int", 10),
        ("smalen2", "int", 10),
        ("smalen3", "int", 10),
        ("smalen4", "int", 15),
        ("siglen", "int", 9),
    ),
    plots=(
        ("kst", "line", "KST"),
        ("signal", "line", "Signal"),
    ),
    levels=({"value": 0},),
    fn=_fn_know_sure_thing,
)

SPEC_KLINGER_OSCILLATOR = IndicatorSpec(
    id="klinger-oscillator",
    name="Klinger Oscillator",
    category="Volume",
    placement="pane",
    params=(),
    plots=(
        ("kvo", "line", "KVO"),
        ("signal", "line", "Signal"),
    ),
    levels=({"value": 0},),
    fn=_fn_klinger_oscillator,
)

__all__ = [
    "mass_index",
    "know_sure_thing",
    "klinger_oscillator",
    "nvi",
    "pvi",
    "pvo",
    "SPEC_MASS_INDEX",
    "SPEC_KLINGER_OSCILLATOR",
    "SPEC_KNOW_SURE_THING",
    "SPEC_NVI",
    "SPEC_PVI",
    "SPEC_PVO",
]
