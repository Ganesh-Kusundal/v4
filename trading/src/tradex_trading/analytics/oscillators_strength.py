"""Oscillators / strength — MFI, PPO, TRIX, TSI, SMI family (openalgo-charts parity).

Port of ``src/indicators/momentum.ts`` (MFI) and ``src/indicators/strength.ts``
(PPO/TRIX/TSI/SMI) with the same warmup gaps and smoothing chains. Helpers
are imported from ``indicators.py`` and never redefined here so parallel
Batch-2 tasks stay merge-clean.
"""

from __future__ import annotations

import math
from typing import Any

from tradex_trading.analytics.indicators import (
    IndicatorSpec,
    _ema_of_gapped,
)

__all__ = [
    "mfi",
    "ppo",
    "trix",
    "tsi",
    "smi",
    "smi_ergodic_indicator",
    "smi_ergodic_oscillator",
    "SPEC_MFI",
    "SPEC_PPO",
    "SPEC_TRIX",
    "SPEC_TSI",
    "SPEC_SMI",
    "SPEC_SMI_ERGODIC_INDICATOR",
    "SPEC_SMI_ERGODIC_OSCILLATOR",
]


def _f(v: Any) -> float:
    return float(v)


def _closes(candles: list) -> list[float]:
    return [_f(c.ohlc.close.value) for c in candles]


def _highs(candles: list) -> list[float]:
    return [_f(c.ohlc.high.value) for c in candles]


def _lows(candles: list) -> list[float]:
    return [_f(c.ohlc.low.value) for c in candles]


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


def _change(values: list[float | None]) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    for i in range(1, n):
        a = values[i]
        b = values[i - 1]
        if a is None or b is None:
            continue
        out[i] = a - b
    return out


def _sma_gapped(values: list[float | None], period: int) -> list[float | None]:
    """Gap-aware SMA: None if any window entry is None (TS NaN carry-through)."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        # filter None handled above
        s = sum(v for v in window)  # type: ignore[arg-type]
        out[i] = s / period
    return out


def _ppo_ma(values: list[float | None], period: int, ma_type: str) -> list[float | None]:
    if ma_type == "SMA":
        return _sma_gapped(values, period)
    return _ema_of_gapped(values, period)


def _tsi_series(values: list[float], short_length: int, long_length: int) -> list[float | None]:
    """Blau double smoothing: long EMA then short EMA (TS tsiSeries parity)."""
    pc = _change(values)
    def double_smooth(v: list[float | None]) -> list[float | None]:
        e1 = _ema_of_gapped(v, long_length)
        return _ema_of_gapped(e1, short_length)
    smoothed = double_smooth(pc)
    pc_abs: list[float | None] = [None if x is None else abs(x) for x in pc]
    smoothed_abs = double_smooth(pc_abs)
    n = len(values)
    out: list[float | None] = [None] * n
    for i in range(n):
        s = smoothed[i]
        sa = smoothed_abs[i]
        if s is None or sa is None:
            continue
        if sa == 0:
            continue
        out[i] = 100.0 * (s / sa)
    return out


# ---------------------------------------------------------------------------
# MFI
# ---------------------------------------------------------------------------

def mfi(candles: list, period: int = 14) -> list[float | None]:
    """Money Flow Index (openalgo-charts ``MFI`` parity).

    TP = (H+L+C)/3; money flow = TP*V signed by TP direction; then
    MFI = 100 - 100/(1 + pos/neg) over ``period``. First prints at
    index ``period`` (loop starts at period, not period-1).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(candles)
    out: list[float | None] = [None] * n
    if n == 0:
        return out
    tp = [(_f(c.ohlc.high.value) + _f(c.ohlc.low.value) + _f(c.ohlc.close.value)) / 3.0 for c in candles]
    vols = [_f(c.volume.value) for c in candles]
    pos = [0.0] * n
    neg = [0.0] * n
    for i in range(1, n):
        flow = tp[i] * vols[i]
        if tp[i] > tp[i - 1]:
            pos[i] = flow
        elif tp[i] < tp[i - 1]:
            neg[i] = flow
    if n < period + 1:
        return out
    for i in range(period, n):
        p = 0.0
        q = 0.0
        for j in range(period):
            p += pos[i - j]
            q += neg[i - j]
        if q == 0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + p / q)
    return out


# ---------------------------------------------------------------------------
# PPO
# ---------------------------------------------------------------------------

def ppo(
    candles: list,
    fast_length: int = 12,
    slow_length: int = 26,
    signal_length: int = 9,
    osc_type: str = "EMA",
    sig_type: str = "EMA",
) -> dict[str, list]:
    """Percentage Price Oscillator (openalgo-charts ``PPO`` parity).

    PPO = 100*(fastMA - slowMA)/slowMA; signal = MA(PPO). Both fast/slow
    and signal respect ``osc_type``/``sig_type`` (EMA via ``_ema_of_gapped``,
    SMA via gap-aware SMA). Returns dict keyed 'ppo'/'signal'/'hist'.
    """
    if min(fast_length, slow_length, signal_length) < 1:
        raise ValueError("periods must be positive")
    closes: list[float | None] = _closes(candles)  # type: ignore[assignment]
    n = len(closes)
    fast = _ppo_ma(closes, int(fast_length), osc_type)
    slow = _ppo_ma(closes, int(slow_length), osc_type)
    ppo_vals: list[float | None] = [None] * n
    for i in range(n):
        f = fast[i]
        s = slow[i]
        if f is None or s is None:
            continue
        if s == 0:
            continue
        ppo_vals[i] = 100.0 * (f - s) / s
    signal = _ppo_ma(ppo_vals, int(signal_length), sig_type)
    hist: list[float | None] = [None] * n
    for i in range(n):
        p = ppo_vals[i]
        sg = signal[i]
        if p is None or sg is None:
            continue
        hist[i] = p - sg
    return {"ppo": ppo_vals, "signal": signal, "hist": hist}


# ---------------------------------------------------------------------------
# TRIX
# ---------------------------------------------------------------------------

def trix(candles: list, period: int = 18) -> list[float | None]:
    """TRIX: 10000 * change(triple-EMA(log(close))) (openalgo-charts parity).

    Each EMA is chained via ``_ema_of_gapped``; change adds one more bar, so
    first prints at ``3*period-2``.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    closes = _closes(candles)
    n = len(closes)
    logs: list[float | None] = [None] * n
    for i, c in enumerate(closes):
        if c is None or c <= 0:  # type: ignore[operator]
            logs[i] = None
        else:
            logs[i] = math.log(c)
    e1 = _ema_of_gapped(logs, period)
    e2 = _ema_of_gapped(e1, period)
    e3 = _ema_of_gapped(e2, period)
    ch = _change(e3)
    out: list[float | None] = [None] * n
    for i, v in enumerate(ch):
        if v is None:
            continue
        out[i] = 10000.0 * v
    return out


# ---------------------------------------------------------------------------
# TSI
# ---------------------------------------------------------------------------

def tsi(
    candles: list,
    long_length: int = 25,
    short_length: int = 13,
    signal_length: int = 13,
) -> dict[str, list]:
    """True Strength Index (openalgo-charts ``TSI`` parity).

    TSI = 100*doubleSmooth(delta)/doubleSmooth(|delta|) with double smoothing
    long->short; signal = EMA(TSI). Bounded [-100,100].
    """
    if min(long_length, short_length, signal_length) < 1:
        raise ValueError("periods must be positive")
    closes = _closes(candles)
    vals = _tsi_series(closes, int(short_length), int(long_length))
    sig = _ema_of_gapped(vals, int(signal_length))
    return {"tsi": vals, "signal": sig}


# ---------------------------------------------------------------------------
# SMI
# ---------------------------------------------------------------------------

def smi(candles: list, length_k: int = 10, length_d: int = 3, length_ema: int = 3) -> dict[str, list]:
    """Stochastic Momentum Index (openalgo-charts ``SMI`` parity).

    Double-EMA of (close - median) over double-EMA of range, scaled by 200.
    """
    if min(length_k, length_d, length_ema) < 1:
        raise ValueError("periods must be positive")
    n = len(candles)
    his = _highs(candles)
    los = _lows(candles)
    cls = _closes(candles)
    hh = _highest(his, int(length_k))
    ll = _lowest(los, int(length_k))
    span: list[float | None] = [None] * n
    relative: list[float | None] = [None] * n
    for i in range(n):
        h = hh[i]
        lo = ll[i]
        if h is None or lo is None:
            continue
        span[i] = h - lo
        relative[i] = cls[i] - (h + lo) / 2.0

    def ema_ema(v: list[float | None]) -> list[float | None]:
        e1 = _ema_of_gapped(v, int(length_d))
        return _ema_of_gapped(e1, int(length_d))

    num = ema_ema(relative)
    den = ema_ema(span)
    smi_vals: list[float | None] = [None] * n
    for i in range(n):
        nv = num[i]
        dv = den[i]
        if nv is None or dv is None:
            continue
        if dv == 0:
            continue
        smi_vals[i] = 200.0 * (nv / dv)
    ema_vals = _ema_of_gapped(smi_vals, int(length_ema))
    return {"smi": smi_vals, "ema": ema_vals}


# ---------------------------------------------------------------------------
# SMI Ergodic Indicator / Oscillator
# ---------------------------------------------------------------------------

def smi_ergodic_indicator(
    candles: list,
    long_length: int = 20,
    short_length: int = 5,
    signal_length: int = 5,
) -> dict[str, list]:
    """SMI Ergodic Indicator: TSI with faster defaults, plotted vs signal."""
    if min(long_length, short_length, signal_length) < 1:
        raise ValueError("periods must be positive")
    closes = _closes(candles)
    erg = _tsi_series(closes, int(short_length), int(long_length))
    sig = _ema_of_gapped(erg, int(signal_length))
    return {"erg": erg, "sig": sig}


def smi_ergodic_oscillator(
    candles: list,
    long_length: int = 20,
    short_length: int = 5,
    signal_length: int = 5,
) -> dict[str, list]:
    """SMI Ergodic Oscillator: erg - sig histogram."""
    if min(long_length, short_length, signal_length) < 1:
        raise ValueError("periods must be positive")
    closes = _closes(candles)
    n = len(closes)
    erg = _tsi_series(closes, int(short_length), int(long_length))
    sig = _ema_of_gapped(erg, int(signal_length))
    osc: list[float | None] = [None] * n
    for i in range(n):
        e = erg[i]
        s = sig[i]
        if e is None or s is None:
            continue
        osc[i] = e - s
    return {"osc": osc}


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------

def _fn_mfi(candles, period):
    return mfi(candles, int(period))


def _fn_ppo(candles, fast_length, slow_length, signal_length, osc_type, sig_type):
    return ppo(candles, int(fast_length), int(slow_length), int(signal_length), str(osc_type), str(sig_type))


def _fn_trix(candles, period):
    return trix(candles, int(period))


def _fn_tsi(candles, long_length, short_length, signal_length):
    return tsi(candles, int(long_length), int(short_length), int(signal_length))


def _fn_smi(candles, length_k, length_d, length_ema):
    return smi(candles, int(length_k), int(length_d), int(length_ema))


def _fn_smi_ergodic_indicator(candles, long_length, short_length, signal_length):
    return smi_ergodic_indicator(candles, int(long_length), int(short_length), int(signal_length))


def _fn_smi_ergodic_oscillator(candles, long_length, short_length, signal_length):
    return smi_ergodic_oscillator(candles, int(long_length), int(short_length), int(signal_length))


SPEC_MFI = IndicatorSpec(
    id="mfi",
    name="Money Flow Index",
    category="Momentum",
    placement="pane",
    params=(("period", "int", 14),),
    plots=(("value", "line", "MFI"),),
    levels=({"value": 80}, {"value": 20}),
    fn=_fn_mfi,
)

SPEC_PPO = IndicatorSpec(
    id="ppo",
    name="Percentage Price Oscillator",
    category="Momentum",
    placement="pane",
    params=(
        ("fast_length", "int", 12),
        ("slow_length", "int", 26),
        ("signal_length", "int", 9),
        ("osc_type", "string", "EMA"),
        ("sig_type", "string", "EMA"),
    ),
    plots=(
        ("ppo", "line", "PPO"),
        ("signal", "line", "Signal"),
        ("hist", "histogram", "Histogram"),
    ),
    levels=({"value": 0},),
    fn=_fn_ppo,
)

SPEC_TRIX = IndicatorSpec(
    id="trix",
    name="TRIX",
    category="Momentum",
    placement="pane",
    params=(("period", "int", 18),),
    plots=(("value", "line", "TRIX"),),
    levels=({"value": 0},),
    fn=_fn_trix,
)

SPEC_TSI = IndicatorSpec(
    id="tsi",
    name="True Strength Index",
    category="Momentum",
    placement="pane",
    params=(
        ("long_length", "int", 25),
        ("short_length", "int", 13),
        ("signal_length", "int", 13),
    ),
    plots=(
        ("tsi", "line", "TSI"),
        ("signal", "line", "Signal"),
    ),
    levels=({"value": 0},),
    fn=_fn_tsi,
)

SPEC_SMI = IndicatorSpec(
    id="smi",
    name="Stochastic Momentum Index",
    category="Momentum",
    placement="pane",
    params=(
        ("length_k", "int", 10),
        ("length_d", "int", 3),
        ("length_ema", "int", 3),
    ),
    plots=(
        ("smi", "line", "SMI"),
        ("ema", "line", "EMA"),
    ),
    levels=({"value": 40}, {"value": 0}, {"value": -40}),
    fn=_fn_smi,
)

SPEC_SMI_ERGODIC_INDICATOR = IndicatorSpec(
    id="smi-ergodic-indicator",
    name="SMI Ergodic Indicator",
    category="Momentum",
    placement="pane",
    params=(
        ("long_length", "int", 20),
        ("short_length", "int", 5),
        ("signal_length", "int", 5),
    ),
    plots=(
        ("erg", "line", "SMI"),
        ("sig", "line", "Signal"),
    ),
    fn=_fn_smi_ergodic_indicator,
)

SPEC_SMI_ERGODIC_OSCILLATOR = IndicatorSpec(
    id="smi-ergodic-oscillator",
    name="SMI Ergodic Oscillator",
    category="Momentum",
    placement="pane",
    params=(
        ("long_length", "int", 20),
        ("short_length", "int", 5),
        ("signal_length", "int", 5),
    ),
    plots=(("osc", "histogram", "Oscillator"),),
    fn=_fn_smi_ergodic_oscillator,
)
